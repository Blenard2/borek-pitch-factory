import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  chapterGroups,
  DiscoveryAnalysisError,
  editableSection,
  isOpportunityAnalysis,
  pagePosition,
  PAGE_TYPES,
  parseDiscoveryAnalysis,
  sourceLink,
  stageProgress,
  type AnalysisPage,
} from "./discoveryAnalysis.js";
import { sampleAnalysis } from "./discoveryAnalysisSample.js";
import { renderAnalysisDocument, renderAnalysisSheet } from "./discoveryAnalysisSheets.js";
import { DISCOVERY_PAGE_CATALOG } from "./discoveryFirst.js";
import { canDownloadDiscoveryPdf, createDiscoveryWorkspaceFixture, createFixtureDiscoveryWorkspaceAdapter } from "./discoveryWorkspace.js";

const OPPORTUNITY = "11111111-1111-4111-8111-111111111111";

async function run() {
  const paper = parseDiscoveryAnalysis(sampleAnalysis(OPPORTUNITY), OPPORTUNITY);
  const manifest = paper.page_manifest;

  // The page count comes from the server's manifest. It is not seven and nothing expects a number.
  assert.ok(manifest.length > 7);
  assert.deepEqual(manifest.map((page) => page.number), manifest.map((_, index) => index + 1));
  for (const count of [3, 9, manifest.length]) {
    const shorter = { ...sampleAnalysis(OPPORTUNITY) as object, page_manifest: structuredClone(manifest.slice(0, count)) };
    assert.equal(parseDiscoveryAnalysis(shorter, OPPORTUNITY).page_manifest.length, count);
  }
  const longer = structuredClone(manifest);
  for (let extra = 0; extra < 20; extra += 1) longer.push({ ...structuredClone(manifest[4]), id: `extra-${extra}`, number: longer.length + 1 });
  assert.equal(parseDiscoveryAnalysis({ ...sampleAnalysis(OPPORTUNITY) as object, page_manifest: longer }).page_manifest.length, manifest.length + 20);
  assert.equal(pagePosition(manifest, manifest[4].id), `Page 5 of ${manifest.length}`);

  // Navigation is grouped by the chapters the document actually has.
  const groups = chapterGroups(manifest);
  assert.deepEqual(groups.map((group) => group.key), ["front", "chapter-1", "chapter-2", "chapter-3", "chapter-4", "chapter-5"]);
  // The conclusion is the three steps as numbered points (M4), then the branded closing (M9).
  assert.deepEqual(groups[5].pages.map((page) => [page.id, page.type]), [["closing-steps", "M4"], ["closing", "M9"]]);
  const steps = renderAnalysisSheet(groups[5].pages[0], "/wp");
  for (const text of [">01<", ">02<", ">03<", "Baseline workshop", "Pilot within weeks", "Scaling roadmap"]) assert.ok(steps.includes(text), text);
  assert.doesNotMatch(steps, /<table/);
  assert.equal(groups.reduce((total, group) => total + group.pages.length, 0), manifest.length);
  assert.deepEqual(groups[0].pages.map((page) => page.type), ["M1", "M2"]);
  assert.match(groups[1].label, /^01 · /);
  assert.equal(groups[5].pages.at(-1)?.type, "M9");

  // Only the nine master page types are rendered, and every one of them is in use.
  assert.deepEqual([...new Set(manifest.map((page) => page.type))].sort(), [...PAGE_TYPES]);
  for (const page of manifest) {
    const html = renderAnalysisSheet(page, "/whitepaper");
    assert.match(html, new RegExp(`^<section class="page" data-page-id="${page.id}" data-page-type="${page.type}"`));
    assert.match(html, /overflow:hidden/);
    assert.ok(html.includes(`<span>${String(page.number).padStart(2, "0")}</span>`) || page.type === "M1", `page number on ${page.id}`);
    assert.doesNotMatch(html, /undefined|\[object Object\]|NaN/);
  }
  assert.throws(() => renderAnalysisSheet({ ...manifest[0], type: "M10" as AnalysisPage["type"] }, "/whitepaper"), /Unknown white paper page type/);
  assert.throws(() => parseDiscoveryAnalysis({ ...sampleAnalysis(OPPORTUNITY) as object, page_manifest: [{ ...manifest[0], type: "M10" }] }), DiscoveryAnalysisError);

  // Master structure per type, with the content of this document.
  const of = (type: string) => manifest.filter((page) => page.type === type);
  const cover = renderAnalysisSheet(manifest[0], "/wp");
  assert.match(cover, /src="\/wp\/cover\.png"/);
  assert.match(cover, /font-size:62px;font-weight:300/);
  assert.ok(cover.includes("AI opportunity analysis"));
  const contents = renderAnalysisSheet(manifest[1], "/wp");
  for (const entry of manifest[1].content.entries as { label: string; page: number; page_id: string }[]) {
    assert.ok(contents.includes(entry.label.replace(/&/g, "&amp;")));
    assert.equal(manifest.find((page) => page.id === entry.page_id)?.number, entry.page);
  }
  assert.match(contents, />In short</);
  const opener = renderAnalysisSheet(of("M3")[0], "/wp");
  assert.match(opener, /font-size:88px/);
  assert.match(opener, />Key message</);
  const deepDives = of("M5");
  assert.ok(deepDives.every((page) => { const items = page.content.items as unknown[]; return items.length >= 1 && items.length <= 2; }));
  const deepDive = renderAnalysisSheet(deepDives[0], "/wp");
  for (const label of ["Solution", "How it works", "Result", "Q1", "Q2", "Opportunity if:"]) assert.ok(deepDive.includes(label), label);
  assert.match(deepDive, /Technically:/);
  const tables = of("M6");
  assert.ok(tables.every((page) => (page.content.rows as unknown[]).length <= 8 && (page.content.columns as unknown[]).length === 3));
  // The master's column geometry is binding for every M6 table: 24 % · fluid · 20 %.
  for (const table of tables) {
    const html = renderAnalysisSheet(table, "/wp");
    assert.deepEqual(html.match(/width:\d+%/g), ["width:100%", "width:24%", "width:20%"], table.id);
  }
  assert.ok(tables.filter((page) => page.id.startsWith("shadow-table-")).length > 1, "shadow processes continue on a second M6 page");
  const diagrams = of("M7");
  assert.ok(diagrams.every((page) => (page.content.rows as unknown[]).length <= 5));
  const diagram = renderAnalysisSheet(diagrams[0], "/wp");
  assert.match(diagram, /grid-template-columns:96px 1fr 1fr 1fr 128px 44px/);
  assert.match(diagram, new RegExp(`grid-row:span ${(diagrams[0].content.rows as unknown[]).length}`));
  assert.equal((diagram.match(/>Human decides</g) ?? []).length, (diagrams[0].content.rows as unknown[]).length);
  assert.match(diagram, />Learning loop</);
  assert.match(renderAnalysisSheet(of("M8")[0], "/wp"), /font-size:34px/);
  const closing = renderAnalysisSheet(manifest.at(-1)!, "/wp");
  assert.ok(closing.includes("Let us establish the first baseline together."));
  assert.ok(closing.includes("Your AI Department. Delivered, not built."));

  // User text is escaped, never interpreted.
  const hostile = structuredClone(manifest[0]);
  hostile.content.title = `<img src=x onerror="alert(1)">`;
  assert.ok(renderAnalysisSheet(hostile, "/wp").includes("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"));

  // The PDF is the manifest in order: as many pages as the document has.
  const pdf = renderAnalysisDocument(manifest, "Sample", "https://host/whitepaper");
  assert.equal((pdf.match(/<section class="page"/g) ?? []).length, manifest.length);
  assert.match(pdf, /@page\{size:210mm 297mm;margin:0\}/);

  // Generation progress is counted in stages; skipped (not requested) parts do not count.
  assert.deepEqual(stageProgress([
    { key: "research", label: "Research", status: "ready" },
    { key: "overview", label: "Overview", status: "generating" },
    { key: "optional_parts", label: "Optional", status: "skipped" },
    { key: "layout", label: "Layout", status: "waiting" },
  ]), { done: 1, total: 3, current: { key: "overview", label: "Overview", status: "generating" }, failed: null });

  // A paper stored in the old seven-page format is recognised and left to the legacy viewer.
  const legacy = JSON.parse(readFileSync("../../packages/contracts/fixtures/discovery_paper.ready.json", "utf8"));
  assert.equal(isOpportunityAnalysis(legacy), false);
  assert.equal(isOpportunityAnalysis(sampleAnalysis(OPPORTUNITY)), true);
  assert.throws(() => parseDiscoveryAnalysis(legacy), /older format/);
  assert.throws(() => parseDiscoveryAnalysis(sampleAnalysis(OPPORTUNITY), "22222222-2222-4222-8222-222222222222"), /another opportunity/);
  assert.throws(() => parseDiscoveryAnalysis({ ...sampleAnalysis(OPPORTUNITY) as object, page_manifest: [] }), /do not agree/);

  // Editing addresses logical sections, not pages.
  const analysis = {
    research: { core_thesis: { text: "Working hypothesis", origin: "WORKING_HYPOTHESIS" } },
    areas: [{ id: "sales", name: "Sales", lead: "Lead", priority: true, business_case: { payback_type: "quick_win" },
      opportunities: [{ id: "quotes", title: "Quote drafting", solution: "s", ai_technology: "t", how_it_works: "h", result: "r", discovery_questions: ["a?", "b?"], opportunity_signal: "sig" }] }],
    shadow_processes: [{ id: "tracker", decision: "Master tracker", handled_today_via: "Excel", ai_approach: "Live view" }],
    closing: { name: "Conclusion", headline: "H", paragraphs: ["a", "b"], steps: ["1", "2", "3"] },
  };
  const opportunity = editableSection(analysis, "opportunity:sales/quotes");
  assert.equal(opportunity?.label, "Opportunity: Quote drafting");
  assert.deepEqual(Object.keys(opportunity!.value), ["title", "solution", "ai_technology", "how_it_works", "result", "discovery_questions", "opportunity_signal"]);
  assert.equal("id" in opportunity!.value, false);
  assert.equal(editableSection(analysis, "area:sales")?.label, "Area and business case: Sales");
  assert.deepEqual(editableSection(analysis, "thesis")?.value, { text: "Working hypothesis" });
  assert.equal(editableSection(analysis, "shadow_process:tracker")?.label, "Shadow process: Master tracker");
  assert.equal(editableSection(analysis, "opportunity:sales/missing"), null);
  assert.equal(editableSection(analysis, "research"), null);
  assert.equal(editableSection(null, "closing"), null);
  assert.equal(editableSection(analysis, "closing")?.label, "Conclusion");

  // No fixed page count is left in the analysis workspace or the shared shell.
  for (const file of [
    "src/components/DiscoveryAnalysisWorkspace.tsx",
    "src/components/OpportunityWorkflowShell.tsx",
    "src/lib/discoveryAnalysis.ts",
    "src/lib/discoveryAnalysisSheets.ts",
    "src/app/opportunities/[opportunityId]/discovery/page.tsx",
  ]) {
    const source = readFileSync(file, "utf8").replace(/\/\/.*$/gm, "").replace(/\{\/\*[\s\S]*?\*\/\}/g, "");
    assert.doesNotMatch(source, /\bseven\b|of 7\b|[!=]== 7\b|length === 7|\* 7\b/i, file);
  }
  const workspace = readFileSync("src/components/DiscoveryAnalysisWorkspace.tsx", "utf8");
  assert.match(workspace, /pagePosition\(manifest, selected\.id\)/);
  assert.match(workspace, /max=\{manifest\.length\}/);
  assert.match(workspace, /findOverflowingPages/);
  assert.match(readFileSync("src/components/PresentationWorkspace.tsx", "utf8"), /Approve the Discovery analysis before generating Master Presentation V1\./);

  // Preview mode has no server: approving the sample records the approval in the local journey.
  const adapter = createFixtureDiscoveryWorkspaceAdapter(createDiscoveryWorkspaceFixture("opp-preview", DISCOVERY_PAGE_CATALOG.map(() => "ready")));
  const approved = await adapter.approve({ opportunity_id: "opp-preview", version_id: "discovery-v1", expected_revision: 1 });
  assert.equal(approved.document_state, "approved");
  assert.equal(canDownloadDiscoveryPdf(approved), true, "the preview journey accepts this record as an approved Discovery");

  // BAP-5: a cited website source is linked only when it is an http(s) address.
  assert.equal(sourceLink({ source_id: "https://www.hansa-praezision.test/about-us" }), "https://www.hansa-praezision.test/about-us");
  for (const unsafe of ["javascript:alert(1)", "data:text/html,x", "annual-report", ""]) {
    assert.equal(sourceLink({ source_id: unsafe }), null, unsafe);
  }
  assert.equal(sourceLink(undefined), null);
  assert.match(workspace, /rel="noopener noreferrer nofollow">source<\/a>/);

  console.log("discoveryAnalysis tests passed");
}

void run();
