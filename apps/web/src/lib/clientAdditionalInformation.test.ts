import assert from "node:assert/strict";
import { readFileSync as readFileFromDisk } from "node:fs";
import { test } from "node:test";

import { ApiRequestError, saveAdditionalInformation } from "./api";

// Sources are compared as text below. A Windows checkout may hold them with CRLF line ends.
const readFileSync = (path: URL, encoding: "utf8"): string => readFileFromDisk(path, encoding).replace(/\r\n/g, "\n");

const opportunityId = "11111111-1111-4111-8111-111111111111";

function mockFetch(respond: (url: string, init: RequestInit) => Response) {
  const calls: { url: string; init: RequestInit }[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init: init ?? {} });
    return respond(String(input), init ?? {});
  }) as typeof fetch;
  return { calls, restore: () => { globalThis.fetch = original; } };
}

// --------------------------------------------------------------- API (BAP-10)

test("only Additional information is sent, to this client's endpoint, and the stored value comes back", async () => {
  const fetchMock = mockFetch(() => Response.json({ id: opportunityId, stage1_intake: { about_company: "Distributor of fasteners." } }));
  try {
    const stored = await saveAdditionalInformation("token", opportunityId, "  Distributor of fasteners.  ");
    assert.equal(stored, "Distributor of fasteners.");
  } finally { fetchMock.restore(); }
  const [call] = fetchMock.calls;
  assert.match(call.url, new RegExp(`/opportunities/${opportunityId}/additional-information$`));
  assert.equal(call.init.method, "PUT");
  assert.deepEqual(JSON.parse(String(call.init.body)), { additional_information: "Distributor of fasteners." }, "nothing but the one field");
  assert.equal(new Headers(call.init.headers).get("Authorization"), "Bearer token");
});

test("blank text clears the field; a refusal is reported, not swallowed", async () => {
  const cleared = mockFetch(() => Response.json({ id: opportunityId, stage1_intake: { about_company: null } }));
  try {
    assert.equal(await saveAdditionalInformation("token", opportunityId, "   "), "");
    assert.deepEqual(JSON.parse(String(cleared.calls[0].init.body)), { additional_information: null });
  } finally { cleared.restore(); }
  const refused = mockFetch(() => Response.json({ error: { code: "OPPORTUNITY_NOT_FOUND", message: "not found" } }, { status: 404 }));
  try {
    await assert.rejects(saveAdditionalInformation("token", opportunityId, "Text"), (error: unknown) => error instanceof ApiRequestError && error.status === 404);
  } finally { refused.restore(); }
});

// --------------------------------------------------------------- editor (BAP-10)

test("in a live session only Additional information is editable and saved", () => {
  const editor = readFileSync(new URL("../components/ClientInformationEditor.tsx", import.meta.url), "utf8");
  assert.ok(editor.includes('const LIVE_EDITABLE_FIELD: ClientInformationField = "additional_information";'));
  assert.ok(editor.includes("disabled: busy || locked || (liveRecord && key !== LIVE_EDITABLE_FIELD),"), "the other client fields stay read-only live");
  assert.ok(editor.includes("await saveAdditionalInformation(accessToken!, record.opportunity_id, draft.additional_information)"));
  assert.ok(editor.includes("...currentRecord.values,"), "the other values are kept as the API holds them");
  assert.ok(editor.includes("The next Discovery generation uses it; the other client fields were not changed."));
  // The local preview adapter still saves only in preview mode.
  assert.ok(editor.includes(": await adapterRef.current.save({"));
  // Refusals are explained; the text stays in the form.
  assert.ok(editor.includes('if (error.status === 403 || error.status === 404) return "You do not have permission to update this client information.";'));
});
