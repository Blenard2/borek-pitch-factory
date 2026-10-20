import assert from "node:assert/strict";
import { readFileSync as readFileFromDisk } from "node:fs";
import { test } from "node:test";

import { websiteResearchState, type AnalysisPage, type DiscoveryAnalysis, type WebsiteResearchRecord } from "./discoveryAnalysis";
import { linkableSource, renderAnalysisDocument, renderAnalysisSheet } from "./discoveryAnalysisSheets";

// Sources are compared as text below. A Windows checkout may hold them with CRLF line ends.
const readFileSync = (path: URL, encoding: "utf8"): string => readFileFromDisk(path, encoding).replace(/\r\n/g, "\n");

function paper(record: WebsiteResearchRecord | undefined, { website = "https://www.hansa-praezision.test", researched = false } = {}): DiscoveryAnalysis {
  return {
    schema_version: "2.0",
    opportunity_id: "11111111-1111-4111-8111-111111111111",
    document_id: "22222222-2222-4222-8222-222222222222",
    latest_approved_version_id: null,
    status: "ready",
    generated_at: "2026-10-20T10:00:00Z",
    language: "en",
    intake_context: { client_name: "Hansa", meeting_purpose: "", website_url: website },
    generation: {
      mode: "fixture", specificity: "company", research_mode: researched ? "provider" : "user_context_only",
      optional_parts: {}, stages: [], ...(record ? { website_research: record } : {}),
    },
    analysis: {},
    page_manifest: [],
  } as unknown as DiscoveryAnalysis;
}

// --------------------------------------------------------------- research status (BAP-10)

test("complete research shows the research summary and offers no retry", () => {
  const state = websiteResearchState(paper({ status: "complete", site: "hansa-praezision.test", summary: "Website research read 6 pages of hansa-praezision.test and quotes 15 statements with their source." }, { researched: true }));
  assert.equal(state?.kind, "complete");
  assert.equal(state?.title, "Website analysed: hansa-praezision.test");
  assert.match(state!.message, /read 6 pages/);
  assert.equal(state?.canRetry, false);
});

test("partial research names what is missing; a missing linked page does not count", () => {
  const state = websiteResearchState(paper({
    status: "partial", site: "vortex.test",
    errors: [
      { code: "WEBSITE_REQUIRES_JAVASCRIPT", url: "https://vortex.test/", message: "The homepage is rendered with JavaScript; only its metadata could be read." },
      { code: "WEBSITE_PAGE_NOT_FOUND", url: "https://vortex.test/products", message: "The page does not exist (HTTP 404)." },
    ],
  }, { researched: true }));
  assert.equal(state?.kind, "partial");
  assert.deepEqual(state?.reasons, ["The homepage is rendered with JavaScript; only its metadata could be read."]);
  assert.equal(state?.canRetry, true);
  const onlyMinor = websiteResearchState(paper({ status: "partial", site: "x.test", summary: "Website research read 2 pages.", errors: [] }, { researched: true }));
  assert.equal(onlyMinor?.message, "Website research read 2 pages.", "no sentence ends on a dangling colon");
});

test("failed research shows the actual reason and the recovery actions", () => {
  const state = websiteResearchState(paper({ status: "failed", requested_url: "https://www.festo.test", errors: [{ code: "WEBSITE_ACCESS_RESTRICTED", url: "https://www.festo.test/", message: "The website refused automated access (HTTP 403); this is respected." }] }));
  assert.equal(state?.kind, "failed");
  assert.equal(state?.title, "Website could not be analysed: www.festo.test", "shown as a host name, not a raw address");
  assert.deepEqual(state?.reasons, ["The website refused automated access (HTTP 403); this is respected."]);
  assert.equal(state?.canRetry, true);
  // Research that read pages but found nothing citable is not presented as researched either.
  const empty = websiteResearchState(paper({ status: "partial", site: "thin.test", errors: [] }));
  assert.equal(empty?.kind, "failed");
  assert.deepEqual(empty?.reasons, ["No usable business content was found on the website."]);
});

test("disabled research explains the configuration and never offers a retry", () => {
  const state = websiteResearchState(paper(undefined));
  assert.equal(state?.kind, "disabled");
  assert.match(state!.message, /not enabled in this environment/);
  assert.equal(state?.canRetry, false, "a retry cannot switch the research on");
  assert.equal(websiteResearchState(paper(undefined, { website: "" })), null, "no website entered: nothing to report");
  assert.equal(websiteResearchState(paper({ status: "not_provided" })), null);
});

test("the workspace shows the status accessibly and links to this client's information", () => {
  const workspace = readFileSync(new URL("../components/DiscoveryAnalysisWorkspace.tsx", import.meta.url), "utf8");
  assert.match(workspace, /data-testid="website-research-status"/);
  assert.match(workspace, /aria-labelledby="discovery-research-status-title"/);
  assert.match(workspace, /role=\{researchState\.kind === "failed" \? "alert" : "status"\}/);
  assert.ok(workspace.includes("const clientInformationHref = `/opportunities/${encodeURIComponent(opportunityId)}/client-information`;"));
  assert.ok(workspace.includes('<Link className="btn btn-secondary" href={clientInformationHref}>Add company information</Link>'));
  // Retry is the existing regeneration: a new draft, and the approved version stays.
  assert.ok(workspace.includes('window.confirm("Analyse the website again? This generates a new draft analysis; an approved version stays unchanged.")) void generate();'));
  assert.ok(workspace.includes("{researchState.canRetry ? ("));
});

// --------------------------------------------------------------- printed sources (M6)

const backgroundPage: AnalysisPage = {
  id: "company-background-1", number: 4, type: "M6", dark: false, chapter: { number: 1, name: "Overview" }, nav_label: "Company background", edit_targets: [],
  content: {
    lead: "What the client's public website states about the company, quoted with the page it comes from.",
    columns: ["Topic", "Statement on the website", "Source"],
    rows: [
      { id: "website_fact_1", subject: "Service", description: "CNC turning up to 65 mm diameter", verdict: "hansa-praezision.test/services", source_url: "https://www.hansa-praezision.test/services", grouped: false },
      { id: "website_fact_2", subject: "Product", description: "Bone screws", verdict: "evil", source_url: "javascript:alert(1)", grouped: false },
      { id: "website_fact_3", subject: "Address", description: "Hafenstraße 12", verdict: "x.test/\"a<b>", source_url: "https://x.test/?q=\"a<b>", grouped: false },
    ],
    footnote: "Read from hansa-praezision.test on 20 October 2026.",
    running: { left: "", right: "", footer: "" },
  },
} as unknown as AnalysisPage;

test("sources link to the quoted page in the preview and in the exported document alike", () => {
  const sheet = renderAnalysisSheet(backgroundPage, "/whitepaper");
  const link = '<a href="https://www.hansa-praezision.test/services" target="_blank" rel="noopener noreferrer nofollow"';
  assert.ok(sheet.includes(link) && sheet.includes(">hansa-praezision.test/services</a>"));
  assert.ok(renderAnalysisDocument([backgroundPage], "Analysis", "/whitepaper").includes(link), "the PDF export renders the same link");
  assert.ok(!sheet.includes("javascript:"), "only http(s) addresses are linked");
  assert.ok(sheet.includes(">evil</td>"), "an unlinkable source stays plain text");
  assert.ok(sheet.includes('href="https://x.test/?q=%22a%3Cb%3E"') && sheet.includes("x.test/&quot;a&lt;b&gt;"), "addresses and labels are escaped");
  assert.ok(sheet.includes("overflow-wrap:anywhere"), "a long source address wraps instead of overflowing");
  assert.equal(linkableSource("data:text/html,x"), null);
  assert.equal(linkableSource("http://x.test/a"), "http://x.test/a");
});
