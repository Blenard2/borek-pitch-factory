import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync as readFileFromDisk } from "node:fs";
import { ApiRequestError } from "./api";
import {
  MISSING_TEXT, SOURCE_TEXT, headlineFromVersion, headlineRequestFor, loadPresentationHeadline, opportunityStatement, presentationSourceFor,
  type DeckPhase,
} from "./presentationHeadline";
// Sources are compared as text below. A Windows checkout may hold them with CRLF line ends.
const readFileSync = (path: URL | string, encoding: "utf8"): string => readFileFromDisk(path, encoding).replace(/\r\n/g, "\n");

const acme = "11111111-1111-4111-8111-111111111111";
const nordwind = "22222222-2222-4222-8222-222222222222";
const version = "33333333-3333-4333-8333-333333333333";

function approved(opportunityId: string, overrides: Record<string, unknown> = {}, paper: Record<string, unknown> = {}) {
  return {
    id: version, opportunity_id: opportunityId, version_number: 2, status: "approved",
    paper_json: {
      schema_version: "2.0", opportunity_id: opportunityId,
      analysis: { framing: { document: { title: opportunityId === acme ? "AI opportunities for Acme" : "AI opportunities for Nordwind" } } },
      presentation_brief: {
        client_name: opportunityId === acme ? "Acme" : "Nordwind",
        priority_opportunities: [
          { area: "Finance & Controlling", title: "Invoice processing (three-way match)", result: "60–90 % dark processing (industry benchmark)." },
          { area: "Sales", title: "Quote drafting", result: "Faster quotes." },
        ],
      },
      ...paper,
    },
    ...overrides,
  };
}

test("the title and the first priority opportunity come from the approved Discovery of this client", () => {
  const headline = headlineFromVersion(approved(acme), acme);
  assert.equal(headline.state, "ready");
  if (headline.state !== "ready") return;
  assert.equal(headline.title, "AI opportunities for Acme");
  assert.deepEqual(headline.opportunity, { area: "Finance & Controlling", title: "Invoice processing (three-way match)", result: "60–90 % dark processing (industry benchmark)." });
  assert.equal(opportunityStatement(headline.opportunity!), "Finance & Controlling: Invoice processing (three-way match) — 60–90 % dark processing (industry benchmark).");
  assert.equal(headline.versionId, version);
  assert.equal(headline.versionNumber, 2);
  assert.equal(headline.clientName, "Acme");
});

test("another client's Discovery is never shown", () => {
  for (const foreign of [approved(nordwind), approved(acme, {}, { opportunity_id: nordwind })]) {
    const headline = headlineFromVersion(foreign, acme);
    assert.equal(headline.state, "failed");
    assert.doesNotMatch(JSON.stringify(headline), /Nordwind/);
  }
});

test("nothing is made up when the AI output is missing", () => {
  assert.deepEqual(headlineFromVersion(approved(acme, { status: "draft" }), acme), { state: "missing", reason: "not_approved" });
  assert.deepEqual(headlineFromVersion(approved(acme, {}, { schema_version: "1.0" }), acme), { state: "missing", reason: "earlier_format" });
  assert.deepEqual(headlineFromVersion(approved(acme, {}, { analysis: { framing: { document: { title: "   " } } } }), acme), { state: "missing", reason: "not_generated" });
  assert.deepEqual(headlineFromVersion(approved(acme, {}, { analysis: null }), acme), { state: "missing", reason: "not_generated" });
  const noOpportunity = headlineFromVersion(approved(acme, {}, { presentation_brief: { priority_opportunities: [] } }), acme);
  assert.equal(noOpportunity.state === "ready" && noOpportunity.opportunity, null, "a title without an opportunity, not an invented one");
  for (const reason of ["not_approved", "not_generated", "earlier_format"] as const) assert.ok(MISSING_TEXT[reason].length > 20);
});

test("the loader reads the deck's Discovery version, or the latest approved one, and maps 404 to missing", async () => {
  const original = globalThis.fetch;
  const urls: string[] = [];
  let status = 200;
  globalThis.fetch = async (input) => {
    urls.push(String(input));
    return status === 200 ? Response.json(approved(acme)) : Response.json({ error: { code: "DISCOVERY_PAPER_NOT_APPROVED", message: "none" } }, { status });
  };
  try {
    assert.equal((await loadPresentationHeadline("token", acme, version)).state, "ready");
    assert.equal((await loadPresentationHeadline("token", acme, null)).state, "ready");
    status = 404;
    assert.deepEqual(await loadPresentationHeadline("token", acme, null), { state: "missing", reason: "not_approved" });
    status = 500;
    await assert.rejects(loadPresentationHeadline("token", acme, null), (error: unknown) => error instanceof ApiRequestError && error.status === 500);
  } finally { globalThis.fetch = original; }
  assert.match(urls[0], new RegExp(`/opportunities/${acme}/discovery-paper/versions/${version}$`));
  assert.match(urls[1], new RegExp(`/opportunities/${acme}/discovery-paper/approved$`));
});

test("long titles and statements are kept whole and wrap in the layout", () => {
  const long = "Opportunity ".repeat(40).trim();
  const headline = headlineFromVersion(approved(acme, {}, {
    analysis: { framing: { document: { title: `AI opportunities for ${"Very Long Client Name ".repeat(6).trim()}` } } },
    presentation_brief: { priority_opportunities: [{ area: "Area", title: "Title", result: long }] },
  }), acme);
  assert.ok(headline.state === "ready" && opportunityStatement(headline.opportunity!).endsWith(long), "never shortened");
  const css = readFileSync(new URL("../app/pitch-shell.css", import.meta.url), "utf8");
  assert.match(css, /\.presentation-headline h2 \{[^}]*overflow-wrap: anywhere;/);
  assert.match(css, /\.presentation-headline-opportunity \{[^}]*overflow-wrap: anywhere;/);
  assert.match(css, /\.presentation-headline \{[^}]*min-width: 0;/);
  assert.doesNotMatch(css.slice(css.indexOf(".presentation-headline {"), css.indexOf(".discovery-workflow-meta {")), /text-overflow: ellipsis|-webkit-line-clamp/);
});

test("the Client Presentation heading replaces the generic title and follows the deck's source", () => {
  const shell = readFileSync(new URL("../components/OpportunityWorkflowShell.tsx", import.meta.url), "utf8");
  const workspace = readFileSync(new URL("../components/PresentationWorkspace.tsx", import.meta.url), "utf8");
  const component = readFileSync(new URL("../components/PresentationHeadline.tsx", import.meta.url), "utf8");
  assert.ok(shell.includes('pathname.endsWith("/presentations") ? (\n              <PresentationHeadline opportunityId={fixture.opportunity_id} kicker={kicker} fallbackTitle={copy.workflow.creating} />'));
  assert.ok(workspace.includes("useReportPresentationSource(live ? presentationSourceFor(opportunityId, livePhase, liveDeck) : null);"));
  // A result belongs to one opportunity, one Discovery request and one attempt; anything else shows "loading".
  assert.ok(component.includes('const key = `${opportunityId}:${request.load === "version" ? `version:${request.versionId}` : request.load}:${attempt}`;'));
  assert.ok(component.includes('result?.key === key ? result.view : { state: "loading" }'));
  assert.ok(component.includes('if (!live || !accessToken || request.load === "none") return;'), "nothing is fetched until the source is settled");
  assert.ok(component.includes('data-testid="presentation-title"') && component.includes('data-testid="presentation-opportunity"'));
  assert.ok(component.includes("Loading the client-specific title and main opportunity...") && component.includes("could not be loaded") && component.includes(">Retry</button>"));
});

// ------------------------------------------------------------------ which Discovery the heading loads

const older = "44444444-4444-4444-8444-444444444444";
/** The requests the heading makes while the page passes through these states. */
function requests(steps: [DeckPhase, { source?: { approved_discovery_version_id?: string | null } | null } | null][], opportunityId = acme) {
  return steps.map(([phase, deck]) => headlineRequestFor(presentationSourceFor(opportunityId, phase, deck), opportunityId));
}

test("while the existing deck is still loading, no Discovery is loaded or shown", () => {
  const steps = requests([["loading", null], ["loading", null], ["ready", { source: { approved_discovery_version_id: older } }]]);
  assert.deepEqual(steps.slice(0, 2), [{ load: "none", view: "loading" }, { load: "none", view: "loading" }]);
  assert.deepEqual(steps[2], { load: "version", versionId: older });
  assert.ok(!steps.some((step) => step.load === "latest"), "the latest approved Discovery is never requested for a deck pinned to an older one");
});

test("a deck pinned to an older Discovery version always shows that version", () => {
  assert.deepEqual(headlineRequestFor(presentationSourceFor(acme, "ready", { source: { approved_discovery_version_id: older } }), acme), { load: "version", versionId: older });
  // Reloading the same deck keeps the pinned version while the reload runs.
  assert.deepEqual(headlineRequestFor(presentationSourceFor(acme, "loading", { source: { approved_discovery_version_id: older } }), acme), { load: "version", versionId: older });
});

test("a legacy deck without source metadata is not matched to the latest Discovery", () => {
  for (const deck of [{ source: null }, { source: {} }, { source: { approved_discovery_version_id: null } }]) {
    assert.deepEqual(headlineRequestFor(presentationSourceFor(acme, "ready", deck), acme), { load: "none", view: "legacy" });
  }
  assert.match(SOURCE_TEXT.legacy, /no generated title can be matched/);
});

test("only when no deck exists is the latest approved Discovery loaded", () => {
  assert.deepEqual(requests([["loading", null], ["idle", null]]), [{ load: "none", view: "loading" }, { load: "latest" }]);
  assert.deepEqual(requests([["failed", null]]), [{ load: "latest" }], "a failed generation left no deck");
  // A failed load does not prove that no deck exists.
  assert.deepEqual(requests([["load-failed", null]]), [{ load: "none", view: "unavailable" }]);
});

test("regenerating and navigating never show a stale Discovery", () => {
  // Regeneration: the old deck is cleared, the new one is generated, then it is pinned to its source.
  assert.deepEqual(requests([["ready", { source: { approved_discovery_version_id: older } }], ["generating", null], ["ready", { source: { approved_discovery_version_id: version } }]]), [
    { load: "version", versionId: older }, { load: "none", view: "loading" }, { load: "version", versionId: version },
  ]);
  // A report still naming the previous opportunity is not used for this one.
  assert.deepEqual(headlineRequestFor({ kind: "pinned", opportunityId: nordwind, versionId: older }, acme), { load: "none", view: "loading" });
  assert.deepEqual(headlineRequestFor(null, acme), { load: "none", view: "loading" }, "no report yet: nothing is loaded");
});
