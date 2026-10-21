import assert from "node:assert/strict";
import { readFileSync as readFileFromDisk } from "node:fs";
import { afterEach, test } from "node:test";

import {
  POST_MEETING_LABEL, PRE_MEETING_LABEL, adaptDiscoveryDocuments, downloadPostMeetingDocx, downloadPreMeetingDocx,
  loadDiscoveryDocuments, loadPresentationVersions, printPreMeetingDiscovery, type DiscoveryDocuments,
} from "./postMeetingDocuments";

// Sources are compared as text below. A Windows checkout may hold them with CRLF line ends.
const readFileSync = (path: URL, encoding: "utf8"): string => readFileFromDisk(path, encoding).replace(/\r\n/g, "\n");

const opportunity = "11111111-1111-4111-8111-111111111111";
const version = "22222222-2222-4222-8222-222222222222";
const discovery2 = "33333333-3333-4333-8333-333333333333";
const presentation = "44444444-4444-4444-8444-444444444444";
const v1 = "55555555-5555-4555-8555-555555555555";
const v2 = "66666666-6666-4666-8666-666666666666";
const base = `/opportunities/${opportunity}`;

function payload(overrides: Partial<DiscoveryDocuments["pre_meeting"]> = {}): DiscoveryDocuments {
  return {
    opportunity_id: opportunity,
    pre_meeting: {
      state: "available", label: "x", resolution: "presentation_v2_base", version_id: version, version_number: 1,
      document_id: "doc", approved_at: "2026-10-15T09:00:00Z", format: "analysis", docx_available: true,
      version_path: `${base}/discovery-paper/versions/${version}`,
      docx_download_path: `${base}/discovery-paper/versions/${version}/download/docx`, presentation_version_id: v1,
      ...overrides,
    } as DiscoveryDocuments["pre_meeting"],
    post_meeting: {
      state: "available", label: "y", version_id: discovery2, version_number: 2, approved_at: "2026-10-20T09:00:00Z",
      docx_download_path: `${base}/post-meeting-discovery/versions/${discovery2}/download/docx`,
    },
  };
}

const calls: { url: string; method: string }[] = [];
const originalFetch = globalThis.fetch;
function mockFetch(respond: (url: string) => Response) {
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), method: init?.method ?? "GET" });
    return respond(String(input));
  }) as typeof fetch;
}
afterEach(() => {
  globalThis.fetch = originalFetch;
  calls.length = 0;
});
function noGeneration() {
  assert.ok(calls.every((call) => call.method === "GET"), "only reads");
  assert.ok(calls.every((call) => !/generate|approve|regenerate/.test(call.url)), "no generation, approval or regeneration");
}

// --------------------------------------------------------------- the list (BAP-11)

test("the documents carry the exact labels and come from this client's endpoint", async () => {
  mockFetch(() => Response.json(payload()));
  const documents = await loadDiscoveryDocuments("token", opportunity);
  assert.match(calls[0].url, new RegExp(`${base}/pre-meeting-discovery$`));
  assert.equal(documents.pre_meeting.label, "Pre-Meeting Discovery Document");
  assert.equal(documents.post_meeting.label, "Post-Meeting Discovery Document");
  assert.equal(PRE_MEETING_LABEL, "Pre-Meeting Discovery Document");
  assert.equal(POST_MEETING_LABEL, "Post-Meeting Discovery Document");
  noGeneration();
});

test("another client's documents and foreign download paths are refused", () => {
  assert.throws(() => adaptDiscoveryDocuments({ ...payload(), opportunity_id: "77777777-7777-4777-8777-777777777777" }, opportunity), /another client/);
  const tampered = adaptDiscoveryDocuments(payload({ docx_download_path: "https://evil.example/file.docx" } as never), opportunity);
  assert.deepEqual([tampered.pre_meeting.state, tampered.pre_meeting.state === "unavailable" && tampered.pre_meeting.reason], ["unavailable", "INVALID_RESPONSE"]);
  const otherPath = adaptDiscoveryDocuments(payload({ version_path: "/opportunities/77777777-7777-4777-8777-777777777777/discovery-paper/versions/" + version } as never), opportunity);
  assert.equal(otherPath.pre_meeting.state, "unavailable");
  assert.equal(otherPath.post_meeting.state, "available", "one broken document does not hide the others");
});

test("an unavailable original keeps its reason; the post-meeting document stays separate", () => {
  const unavailable = adaptDiscoveryDocuments({
    ...payload(),
    pre_meeting: { state: "unavailable", label: "x", reason: "DISCOVERY_ORIGIN_AMBIGUOUS", message: "The Discovery version Presentation V1 was built from is not recorded, and more than one approved version exists." },
    post_meeting: { state: "unavailable", label: "y", reason: "NO_APPROVED_DISCOVERY2", message: "No approved Post-Meeting Discovery Document exists yet." },
  }, opportunity);
  assert.equal(unavailable.pre_meeting.label, PRE_MEETING_LABEL);
  assert.ok(unavailable.pre_meeting.state === "unavailable" && unavailable.pre_meeting.message.includes("not recorded"));
  const both = adaptDiscoveryDocuments(payload(), opportunity);
  assert.ok(both.pre_meeting.state === "available" && both.post_meeting.state === "available");
  assert.notEqual(both.pre_meeting.version_id, both.post_meeting.version_id);
});

// --------------------------------------------------------------- downloads

test("Word downloads use the saved version endpoints and never generate", async () => {
  mockFetch(() => new Response(new Uint8Array([80, 75, 3, 4]), { headers: { "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document" } }));
  const documents = adaptDiscoveryDocuments(payload(), opportunity);
  assert.ok(documents.pre_meeting.state === "available" && documents.post_meeting.state === "available");
  assert.equal((await downloadPreMeetingDocx("token", documents.pre_meeting)).size, 4);
  assert.equal((await downloadPostMeetingDocx("token", documents.post_meeting)).size, 4);
  assert.match(calls[0].url, new RegExp(`${base}/discovery-paper/versions/${version}/download/docx$`));
  assert.match(calls[1].url, new RegExp(`${base}/post-meeting-discovery/versions/${discovery2}/download/docx$`));
  noGeneration();
});

test("a meeting version that cannot be determined is shown as unavailable with its reason", () => {
  const unclear = adaptDiscoveryDocuments({
    ...payload(),
    pre_meeting: { state: "unavailable", label: "x", reason: "FIRST_MEETING_ORIGIN_AMBIGUOUS", message: "Presentation V1 was rebuilt after the first meeting, and the Discovery version used in the meeting cannot be determined." },
  }, opportunity);
  assert.ok(unclear.pre_meeting.state === "unavailable" && unclear.pre_meeting.reason === "FIRST_MEETING_ORIGIN_AMBIGUOUS");
  assert.equal(unclear.pre_meeting.label, PRE_MEETING_LABEL);
  assert.equal(unclear.post_meeting.state, "available", "the other documents stay");
});

test("the earlier format offers no Word file", async () => {
  const legacy = adaptDiscoveryDocuments(payload({ format: "legacy", docx_available: false, docx_download_path: null } as never), opportunity);
  assert.ok(legacy.pre_meeting.state === "available" && legacy.pre_meeting.format === "legacy");
  await assert.rejects(downloadPreMeetingDocx("token", legacy.pre_meeting as never), /no Word export/);
  const advertised = adaptDiscoveryDocuments(payload({ format: "legacy", docx_available: false, docx_download_path: `${base}/discovery-paper/versions/${version}/download/docx` } as never), opportunity);
  assert.equal(advertised.pre_meeting.state, "unavailable", "a Word path for a format without Word export is not trusted");
});

function stubDocument() {
  const written: string[] = [];
  const frame = {
    style: { cssText: "" }, setAttribute() {}, remove() {},
    contentDocument: { open() {}, write(html: string) { written.push(html); }, close() {}, fonts: { ready: Promise.resolve() }, images: [] },
    contentWindow: { addEventListener() {}, focus() {}, print() { written.push("[printed]"); } },
  };
  (globalThis as Record<string, unknown>).document = { createElement: () => frame, body: { appendChild() {} } };
  (globalThis as Record<string, unknown>).window = { location: { origin: "http://localhost" } };
  return written;
}

test("the PDF is printed from the saved version's stored pages", async () => {
  const written = stubDocument();
  const pages = [{ id: "cover", number: 1, type: "M1", dark: true, chapter: null, nav_label: "Cover", edit_targets: [], content: { title: "AI opportunities for Hansa", eyebrow: "AI", subtitle: "Saved subtitle", running: {} } }];
  mockFetch(() => Response.json({ id: version, status: "approved", paper_json: { schema_version: "2.0", opportunity_id: opportunity, page_manifest: pages } }));
  const documents = adaptDiscoveryDocuments(payload(), opportunity);
  assert.ok(documents.pre_meeting.state === "available");
  await printPreMeetingDiscovery("token", opportunity, documents.pre_meeting, "Hansa");
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.match(calls[0].url, new RegExp(`${base}/discovery-paper/versions/${version}$`));
  assert.ok(written[0].includes("AI opportunities for Hansa") && written[0].includes("Saved subtitle"), "the stored page content");
  assert.ok(written[0].includes(`<title>AI opportunities for Hansa - ${PRE_MEETING_LABEL}</title>`));
  assert.equal(written.at(-1), "[printed]");
  noGeneration();
});

test("a version that is not the saved, approved one of this client is not printed", async () => {
  const documents = adaptDiscoveryDocuments(payload(), opportunity);
  assert.ok(documents.pre_meeting.state === "available");
  for (const [row, message] of [
    [{ id: discovery2, status: "approved", paper_json: { opportunity_id: opportunity } }, /could not be read/],
    [{ id: version, status: "draft", paper_json: { opportunity_id: opportunity } }, /could not be read/],
    [{ id: version, status: "approved", paper_json: { opportunity_id: "77777777-7777-4777-8777-777777777777" } }, /another client/],
    [{ id: version, status: "approved", paper_json: { schema_version: "2.0", opportunity_id: opportunity, page_manifest: [] } }, /no pages/],
  ] as const) {
    mockFetch(() => Response.json(row));
    await assert.rejects(printPreMeetingDiscovery("token", opportunity, documents.pre_meeting, "Hansa"), message);
  }
});

test("the version list keeps the latest version (V2) with its number", async () => {
  mockFetch(() => Response.json([
    { presentation_version_id: v2, version_number: 2, status: "ready", is_latest: true, created_at: "2026-10-20T10:00:00Z", source: { product_version: "V2" },
      pptx_download_url: `/presentations/${presentation}/versions/${v2}/download/pptx`, pdf_download_url: `/presentations/${presentation}/versions/${v2}/download/pdf` },
    { presentation_version_id: v1, version_number: 1, status: "ready", is_latest: false, created_at: "2026-10-15T10:00:00Z", source: { product_version: "V1" },
      pptx_download_url: `/presentations/${presentation}/versions/${v1}/download/pptx`, pdf_download_url: `/presentations/${presentation}/versions/${v1}/download/pdf` },
  ]));
  const versions = await loadPresentationVersions("token", presentation);
  assert.deepEqual(versions.map((row) => [row.versionId, row.versionNumber]), [[v2, 2], [v1, 1]]);
  noGeneration();
});

// --------------------------------------------------------------- the workspace

test("the post-meeting workspace shows the documents section independently of the deck", () => {
  const section = readFileSync(new URL("../components/PostMeetingDocuments.tsx", import.meta.url), "utf8");
  const workspace = readFileSync(new URL("../components/PostMeetingPresentationWorkspace.tsx", import.meta.url), "utf8");
  assert.ok(workspace.includes("<PostMeetingDocuments opportunityId={opportunityId} accessToken={accessToken} master={master} clientName={companyName}"));
  assert.ok(workspace.includes("ppt1={workflow?.documents.ppt1} ppt2={workflow?.documents.ppt2} />"));
  assert.ok(section.includes('<h2 id="post-meeting-documents-title">Documents</h2>'));
  for (const label of ["{PRE_MEETING_LABEL}", '"Master Presentation V1" : "Presentation V1"', '"Master Presentation V2" : "Presentation V2"', "{POST_MEETING_LABEL}"]) {
    assert.ok(section.includes(label), label);
  }
  assert.ok(section.includes('{pre.docx_available ? <button'), "Word is offered only where the format supports it");
  assert.ok(section.includes("Retry</button>"), "a failed load can be retried");
  assert.ok(section.includes('{post?.state === "available" ? <li data-testid="document-post-meeting-discovery">'), "the post-meeting document appears once approved");
  assert.ok(section.includes(".catch(() => undefined);"), "a version list that cannot be read does not hide the presentations");
  assert.ok(
    section.includes('const v1Version = (pre?.state === "available" && pre.presentation_version_id) || ppt1?.latest_ready_version_id || null;'),
    "Presentation V1 is the version the resolved Pre-Meeting Discovery Document belongs to, even after V1 was rebuilt",
  );
  assert.doesNotMatch(section, /\b\w*(?:generate|approve)\w*\(/i, "the list never calls a generation or approval");
});
