import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync as readFileFromDisk } from "node:fs";
import {
  approvePostMeetingDiscovery, changedSections, editableSections, generatePostMeetingDiscovery, needsDiscovery2, parsePostMeetingDiscovery,
  savePostMeetingDiscovery, type Discovery2Document, type Discovery2Version,
} from "./postMeetingDiscovery";
import { downloadEmailAttachment, emailFlagText, parsePostMeetingEmail } from "./postMeetingEmail";
// Sources are compared as text below. A Windows checkout may hold them with CRLF line ends.
const readFileSync = (path: URL | string, encoding: "utf8"): string => readFileFromDisk(path, encoding).replace(/\r\n/g, "\n");

const id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const versionId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const hash = "a".repeat(64);
const item = (itemId: string, text: string, origin: "discovery1" | "meeting" | "owner" = "meeting") => ({
  id: itemId, text, origin, status: origin === "discovery1" ? "carried_forward" as const : "new" as const,
  source: origin === "discovery1" ? "discovery1" as const : "transcript" as const, evidence: origin === "meeting" ? text : null, discovery1_ref: null, edited: false,
});
const documentFixture: Discovery2Document = {
  status: "ready", document_id: id, generated_at: "2026-10-16T09:00:00Z", title: "Discovery 2 — Acme", client_name: "Acme", error: null,
  generation: { mode: "fixture", generator_version: "discovery2:v1" }, summary: { previous: "Thesis.", meeting: "From the first meeting: 1 requirements." }, sources: null,
  sections: [
    { key: "context", title: "Context", items: [item("d1-1-context", "Thesis.", "discovery1")] },
    { key: "requirements", title: "Requirements", items: [item("m-requirements-1", "Quotes in one day.")] },
    { key: "challenges", title: "Challenges", items: [] },
  ],
};
const version: Discovery2Version = {
  id: versionId, version_number: 1, status: "draft", content_hash: hash, basis_hash: "b".repeat(64), discovery1_version_id: id,
  generation_mode: "fixture", created_at: null, updated_at: null, approved_at: null, approved_by: null,
};
const envelope = { opportunity_id: id, state: { status: "draft", stale_reasons: [] }, document: documentFixture, current_version: version, versions: [version], input_blockers: [], finalized: false, ready_for_v2: false };

async function withFetch<T>(run: () => Promise<T>) {
  const original = globalThis.fetch;
  const calls: { url: string; method: string; body: unknown }[] = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ url: String(input), method: init?.method ?? "GET", body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined });
    return Response.json(envelope);
  };
  try { return { result: await run(), calls }; } finally { globalThis.fetch = original; }
}

test("only changed sections are saved, with the version and content they were made on", async () => {
  const edits = editableSections(documentFixture);
  assert.deepEqual(changedSections(documentFixture, edits), [], "nothing changed, nothing to save");
  edits.requirements[0] = { ...edits.requirements[0], text: "Quotes in one working day." };
  edits.challenges = [{ text: "Pricing data is spread out." }];
  const changes = changedSections(documentFixture, edits);
  assert.deepEqual(changes, [
    { key: "requirements", items: [{ id: "m-requirements-1", text: "Quotes in one working day." }] },
    { key: "challenges", items: [{ text: "Pricing data is spread out." }] },
  ]);
  const { calls } = await withFetch(() => savePostMeetingDiscovery("token", id, version, changes));
  assert.equal(calls[0].method, "PATCH");
  assert.match(calls[0].url, new RegExp(`/opportunities/${id}/post-meeting-discovery$`));
  assert.deepEqual(calls[0].body, { expected_version_id: versionId, expected_content_hash: hash, sections: changes });
});

test("generate and approve call the Discovery 2 endpoints; approval names the exact version", async () => {
  const generated = await withFetch(() => generatePostMeetingDiscovery("token", id));
  assert.equal(generated.calls[0].method, "POST");
  assert.match(generated.calls[0].url, /\/post-meeting-discovery\/generate$/);
  const approved = await withFetch(() => approvePostMeetingDiscovery("token", id, version));
  assert.deepEqual(approved.calls[0].body, { expected_version_id: versionId, expected_content_hash: hash });
  assert.throws(() => parsePostMeetingDiscovery({ state: {} }), /incomplete/);
  assert.equal(needsDiscovery2(["DISCOVERY2_STALE"]), true);
  assert.equal(needsDiscovery2(["MEETING_REVIEW_NOT_CONFIRMED"]), false);
});

test("the email lists all four generated documents and downloads each from its own pinned version", async () => {
  const attachment = (key: string, artifact: string, format: string, path: string) => ({
    key, artifact, format, file_name: `${key}.${format}`, selected: key === "pdf", available: true, size_bytes: 10,
    presentation_id: artifact.startsWith("master") ? id : null, presentation_version_id: artifact.startsWith("master") ? versionId : null,
    artifact_version_id: versionId, version_number: 1, product_version: "V2", download_path: path,
  });
  const draft = parsePostMeetingEmail({
    opportunity_id: id, journey_stage: "deepening",
    draft: {
      id, revision: 1, status: "draft", send_status: "not_sent", selected_length: "medium", confirmed_revision: null, confirmed_at: null, review: null,
      created_at: "2026-10-16T08:00:00Z", updated_at: "2026-10-16T09:00:00Z",
      lengths: Object.fromEntries(["short", "medium", "extensive"].map((name) => [name, { subject: "S", body: "B", word_count: 1, edited: false }])),
      word_limits: { short: 150, medium: 250, extensive: 400 }, source_status: "valid", review_flags: [], recipients: null, writer: null, source: null,
      attachments: [
        attachment("pptx", "master_presentation_v2", "pptx", `/presentations/${id}/versions/${versionId}/download/pptx`),
        attachment("pdf", "master_presentation_v2", "pdf", `/presentations/${id}/versions/${versionId}/download/pdf`),
        attachment("v1_pdf", "master_presentation_v1", "pdf", `/presentations/${id}/versions/${versionId}/download/pdf`),
        attachment("discovery1_docx", "discovery_1", "docx", `/opportunities/${id}/discovery-paper/versions/${versionId}/download/docx`),
        attachment("discovery2_docx", "discovery_2", "docx", `/opportunities/${id}/post-meeting-discovery/versions/${versionId}/download/docx`),
        { format: "pdf", file_name: "legacy.pdf", selected: false, available: true, presentation_id: id, presentation_version_id: versionId },
      ],
    },
  }, id);
  assert.ok(draft);
  assert.deepEqual(draft.attachments.map((file) => [file.key, file.artifact, file.format]), [
    ["pptx", "master_presentation_v2", "pptx"], ["pdf", "master_presentation_v2", "pdf"], ["v1_pdf", "master_presentation_v1", "pdf"],
    ["discovery1_docx", "discovery_1", "docx"], ["discovery2_docx", "discovery_2", "docx"], ["pdf", "master_presentation_v2", "pdf"],
  ], "an attachment stored before BAP-7 keeps meaning the V2 file of its format");
  const original = globalThis.fetch;
  let url = "";
  globalThis.fetch = async (input) => { url = String(input); return new Response(new Blob(["docx"])); };
  try { await downloadEmailAttachment("token", draft.attachments[4]); } finally { globalThis.fetch = original; }
  assert.match(url, new RegExp(`/opportunities/${id}/post-meeting-discovery/versions/${versionId}/download/docx$`));
});

test("Discovery 2 page: one place to generate, edit, approve and download, with the origin of every item", () => {
  const panel = readFileSync(new URL("../components/PostMeetingDiscoveryPanel.tsx", import.meta.url), "utf8");
  const page = readFileSync(new URL("../app/opportunities/[opportunityId]/post-meeting-discovery/page.tsx", import.meta.url), "utf8");
  assert.match(page, /<PostMeetingDiscoveryPanel opportunityId=\{opportunityId\} \/>/);
  assert.ok(panel.includes('<PostMeetingPhaseNav opportunityId={opportunityId} active="discovery2" />'));
  // Approval only for the current, saved, not stale draft; the server re-checks all of it.
  assert.ok(panel.includes('const canApprove = Boolean(current && current.status === "draft" && state?.status === "draft" && !dirty && !data?.finalized);'));
  assert.ok(panel.includes('data-testid="discovery2-approve" disabled={!canApprove || busy !== null}'));
  assert.ok(panel.includes('disabled={!live || !state.can_generate || busy !== null}'), "generation keeps the server's gate");
  // Fixture output is never presented as AI output.
  assert.ok(panel.includes('"Generated by the deterministic test generator — no AI model was used"'));
  assert.ok(panel.includes('document?.generation?.mode === "live" ? "Generated with the AI model"'));
  // Previous and new information stay distinguishable, and meeting items show their evidence.
  assert.ok(panel.includes("{ORIGIN_LABEL[origin]}") && panel.includes("Evidence: “{item.evidence}”"));
  assert.ok(panel.includes('data-testid="discovery2-download"') && panel.includes('data-testid="discovery2-versions"'));
  assert.ok(panel.includes("an approved version is never changed - your edit becomes a new draft version."));
  assert.ok(panel.includes('href={`${root}/post-meeting-presentation`}>Continue to Master Presentation V2</Link>'));
});

test("the email names owner-written Discovery 2 points as left out, not as personal notes only", () => {
  const text = emailFlagText("owner_notes_omitted");
  assert.match(text, /personal notes/);
  assert.match(text, /wrote or reworded in Discovery 2/);
  assert.match(text, /not said in the meeting in this form/);
});
