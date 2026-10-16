import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync as readFileFromDisk } from "node:fs";
import {
  blockerText, confirmMeetingReview, excludedFromConfirmation, extractionIsCurrent, FINDING_SOURCE_LABEL, findingsState, isStaleReviewError,
  loadMeetingInputs, loadPostMeetingReview, loadPostMeetingWorkflow, MeetingNotesConflict, parseMeetingExtraction,
  parsePostMeetingReview, prepareMeetingEvidence, reviewHeadline, savePersonalNotesChecked, staleReasonText,
  MAX_TRANSCRIPT_BYTES, validateTranscript, workflowCompleted, type MeetingExtraction, type PersonalNotes, type PostMeetingReview,
} from "./postMeeting";
// Sources are compared as text below. A Windows checkout may hold them with CRLF line ends.
const readFileSync = (path: URL | string, encoding: "utf8"): string => readFileFromDisk(path, encoding).replace(/\r\n/g, "\n");

const opportunityId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const extraction: MeetingExtraction = {
  transcript_id: "transcript-1", generated_at: "2026-10-06T10:00:00Z", personal_notes_updated_at: null,
  requirements: ["Keep approved content"], challenges: [], priorities: [], opportunities: [], discussed_solutions: [], decisions: [], follow_ups: [],
};

test("extraction accepts the implemented flat response and explicit missing state only", () => {
  assert.deepEqual(parseMeetingExtraction(extraction), extraction);
  assert.equal(parseMeetingExtraction({ status: "not_generated", extraction: null }), null);
  assert.throws(() => parseMeetingExtraction({ requirements: [] }), /incomplete/);
  assert.throws(() => parseMeetingExtraction({ ...extraction, decisions: [123] }), /incomplete/);
});

test("extraction is stale after transcript or saved notes change", () => {
  assert.equal(extractionIsCurrent(extraction, "transcript-1", { text: null, updated_at: null }), true);
  assert.equal(extractionIsCurrent(extraction, "transcript-2", { text: null, updated_at: null }), false);
  assert.equal(extractionIsCurrent(extraction, "transcript-1", { text: "changed", updated_at: "later" }), false);
  assert.equal(extractionIsCurrent(null, "transcript-1", { text: null, updated_at: null }), false);
  assert.equal(extractionIsCurrent(extraction, "transcript-1", { text: null, updated_at: "cleared-at" }), true);
  assert.equal(extractionIsCurrent(extraction, "transcript-1", { text: "  ", updated_at: "cleared-at" }), true);
});

test("transcript validation accepts backend text formats and rejects empty or unsupported files", () => {
  for (const name of ["call.txt", "call.VTT", "call.srt", "call.docx"]) assert.equal(validateTranscript({ name, size: 1 }), null);
  assert.match(validateTranscript({ name: "call.pdf", size: 1 })!, /TXT/);
  assert.match(validateTranscript({ name: "call.txt", size: 0 })!, /empty/);
  // BAP-6: uppercase extensions and any length up to the API's technical limit are accepted.
  for (const name of ["CALL.TXT", "Call.Srt", "MEETING.DOCX"]) assert.equal(validateTranscript({ name, size: 1 }), null);
  assert.equal(validateTranscript({ name: "long.txt", size: MAX_TRANSCRIPT_BYTES }), null);
  assert.match(validateTranscript({ name: "huge.txt", size: MAX_TRANSCRIPT_BYTES + 1 })!, /larger than 10 MB/);
  assert.match(validateTranscript({ name: "call.doc", size: 1 })!, /TXT, VTT, SRT, or DOCX/);
});

test("meeting reload reads only transcripts, notes and processing state, not use-case resources", async () => {
  const original = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    calls.push(url);
    assert.equal(init?.method ?? "GET", "GET");
    assert.match(url, new RegExp(`/opportunities/${opportunityId}/`));
    const resource = url.split("/").pop()!;
    const data: Record<string, unknown> = {
      transcripts: [{ id: "transcript-1", file_name: "call.txt", processing_status: "pending" }],
      "personal-notes": { text: "Owner observation", updated_at: null },
      "meeting-extraction": extraction,
    };
    return Response.json(data[resource]);
  };
  try {
    const data = await loadMeetingInputs("token", opportunityId);
    assert.equal(calls.length, 3);
    assert.equal(data.notes.text, "Owner observation");
    assert.ok(calls.every((url) => !url.includes("use-cases")));
    assert.equal(data.extraction?.requirements[0], "Keep approved content");
  } finally { globalThis.fetch = original; }
});

test("workflow rejects another opportunity and never infers completed checkpoints from current status", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => Response.json({ opportunity_id: "another-client", steps: [], documents: {} });
  try { await assert.rejects(loadPostMeetingWorkflow("token", opportunityId), /another opportunity/); }
  finally { globalThis.fetch = original; }
  assert.equal(workflowCompleted(null, "first_meeting_completed"), false);
});

test("meeting input failures are surfaced rather than converted into empty inputs", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => Response.json({ error: { code: "FORBIDDEN", message: "Not authorized" } }, { status: 403 });
  try { await assert.rejects(loadMeetingInputs("token", opportunityId), /Not authorized/); }
  finally { globalThis.fetch = original; }
});

test("post-meeting scope retains staged edits during token rotation and leaves the pre-meeting route separate", () => {
  const shell = readFileSync(new URL("../components/PostMeetingShell.tsx", import.meta.url), "utf8");
  const meeting = readFileSync(new URL("../components/MeetingEvidencePanel.tsx", import.meta.url), "utf8");
  const boundary = readFileSync(new URL("../components/OpportunityWorkflowShell.tsx", import.meta.url), "utf8");
  assert.match(shell, /key=\{`\$\{opportunityId\}:\$\{previewMode\}:\$\{ownerId\}`\}/);
  assert.match(meeting, /!accessToken \|\| loaded.current/);
  assert.match(meeting, /prepareMeetingEvidence\(token, opportunityId, transcriptId, notes, baseline/);
  assert.match(meeting, /if \(initial\) setNotes\(value.personal_notes.text \?\? ""\)/, "a refresh never overwrites typed notes");
  assert.match(boundary, /meeting\|review\|follow-up\|post-meeting-presentation/);
  assert.match(shell, /step.id === "ppt_2_generated" \? "post-meeting-presentation"/);
});

function mockPreparation(initialNotes: PersonalNotes, currentExtraction: MeetingExtraction | null = null, failExtraction = false, eligible = true) {
  const calls: string[] = [];
  let savedNotes = { ...initialNotes };
  const writes: unknown[] = [];
  globalThis.fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    const method = init?.method ?? "GET";
    calls.push(`${method} ${path}`);
    assert.ok(path.startsWith(`/opportunities/${opportunityId}/`));
    if (path.endsWith("/workflow-status")) return Response.json({ opportunity_id: opportunityId, current_status: "transcript_added", steps: [{ key: "first_meeting_completed", state: eligible ? "completed" : "current" }], documents: { ppt1: null, ppt2: null, approved_discovery: { version_id: "approved-1", version_number: 1 } }, finalization: null });
    if (path.endsWith("/transcripts")) return Response.json([{ id: "transcript-1", file_name: "call.txt", created_at: "2026-10-06" }]);
    if (path.endsWith("/personal-notes")) {
      if (method === "PUT") {
        const body = JSON.parse(String(init?.body));
        writes.push(body);
        savedNotes = { text: body.text.trim() || null, updated_at: "saved-revision" };
      }
      return Response.json(savedNotes);
    }
    if (path.endsWith("/meeting-extraction")) return Response.json(currentExtraction ?? { status: "not_generated", extraction: null });
    if (path.endsWith("/meeting-extraction/generate")) {
      assert.equal(method, "POST");
      assert.deepEqual(JSON.parse(String(init?.body)), { transcript_id: "transcript-1" });
      return Response.json(failExtraction ? { status: "not_generated", extraction: null } : { ...extraction, personal_notes_updated_at: savedNotes.text ? savedNotes.updated_at : null });
    }
    throw new Error(`Unexpected endpoint ${path}`);
  };
  return { calls, writes };
}

test("transcript-only generation preparation accepts blank or whitespace notes without a notes write", async () => {
  const original = globalThis.fetch;
  try {
    for (const text of ["", "   "]) {
      const baseline = { text: null, updated_at: null };
      const { calls, writes } = mockPreparation(baseline);
      const result = await prepareMeetingEvidence("token", opportunityId, "transcript-1", text, baseline);
      assert.equal(result.inputs.extraction?.transcript_id, "transcript-1");
      assert.equal(writes.length, 0);
      assert.ok(calls.some((call) => call.includes("POST ") && call.endsWith("meeting-extraction/generate")));
      assert.ok(calls.every((call) => !call.includes("use-cases")));
    }
  } finally { globalThis.fetch = original; }
});

test("additional notes save separately before automatic transcript processing", async () => {
  const original = globalThis.fetch;
  try {
    const baseline = { text: null, updated_at: null };
    const { calls, writes } = mockPreparation(baseline);
    const progress: string[] = [];
    const result = await prepareMeetingEvidence("token", opportunityId, "transcript-1", "  Optional observation  ", baseline, { onProgress: (value) => progress.push(value) });
    assert.deepEqual(writes, [{ text: "  Optional observation  " }]);
    assert.ok(calls.findIndex((call) => call.startsWith("PUT ")) < calls.findIndex((call) => call.startsWith("POST ")));
    assert.deepEqual(progress, ["Saving additional notes", "Processing transcript"]);
    assert.equal(result.inputs.notes.text, "Optional observation");
    assert.equal(extractionIsCurrent(result.inputs.extraction, "transcript-1", result.inputs.notes), true);
  } finally { globalThis.fetch = original; }
});

test("clearing old optional notes persists the clear and refreshes extraction", async () => {
  const original = globalThis.fetch;
  try {
    const baseline = { text: "Old notes", updated_at: "old-revision" };
    const { writes } = mockPreparation(baseline, { ...extraction, personal_notes_updated_at: "old-revision" });
    const result = await prepareMeetingEvidence("token", opportunityId, "transcript-1", "", baseline);
    assert.deepEqual(writes, [{ text: "" }]);
    assert.equal(result.inputs.notes.text, null);
    assert.equal(result.inputs.extraction?.personal_notes_updated_at, null);
    assert.equal(extractionIsCurrent(result.inputs.extraction, "transcript-1", result.inputs.notes), true);
  } finally { globalThis.fetch = original; }
});

test("already-current processing is reused and failed processing retains the saved notes baseline", async () => {
  const original = globalThis.fetch;
  try {
    const baseline = { text: null, updated_at: null };
    const first = mockPreparation(baseline, extraction);
    await prepareMeetingEvidence("token", opportunityId, "transcript-1", "", baseline);
    assert.ok(first.calls.every((call) => call.startsWith("GET ")));
    mockPreparation(baseline, null, true);
    const persisted: PersonalNotes[] = [];
    await assert.rejects(prepareMeetingEvidence("token", opportunityId, "transcript-1", "New notes", baseline, { onInputs: (value) => persisted.push(value.notes) }), /not finished processing/);
    assert.deepEqual(persisted, [{ text: "New notes", updated_at: "saved-revision" }]);
  } finally { globalThis.fetch = original; }
});

test("missing transcript, ineligible workflow and conflicting notes stop preparation before writes", async () => {
  const original = globalThis.fetch;
  try {
    const baseline = { text: null, updated_at: null };
    let mocked = mockPreparation(baseline);
    await assert.rejects(prepareMeetingEvidence("token", opportunityId, "missing", "", baseline), /Upload a transcript/);
    assert.ok(mocked.calls.every((call) => call.startsWith("GET ")));
    mocked = mockPreparation(baseline, null, false, false);
    await assert.rejects(prepareMeetingEvidence("token", opportunityId, "transcript-1", "", baseline), /not eligible/);
    assert.ok(mocked.calls.every((call) => call.startsWith("GET ")));
    mocked = mockPreparation({ text: "Someone else's edit", updated_at: "newer" });
    await assert.rejects(prepareMeetingEvidence("token", opportunityId, "transcript-1", "My notes", baseline), MeetingNotesConflict);
    assert.ok(mocked.calls.every((call) => call.startsWith("GET ")));
  } finally { globalThis.fetch = original; }
});

test("meeting screen shows the sources separately, the findings with their source, and an explicit confirmation", () => {
  const source = readFileSync(new URL("../components/MeetingEvidencePanel.tsx", import.meta.url), "utf8");
  // One meeting-input card with the transcript and the notes as two separate inputs, one analysis
  // card and the use cases; notes are optional and never required.
  assert.ok(source.includes('aria-labelledby="meeting-input-title" data-testid="meeting-input"'));
  assert.ok(source.includes('role="tabpanel" id="panel-transcript"') && source.includes('role="tabpanel" id="panel-notes"'));
  assert.ok(source.includes('<span className={styles.eyebrow}>Borek use cases</span>'));
  assert.ok(source.includes('<span className={styles.eyebrow}>Analysis</span><h2 id="findings-title">Meeting findings</h2>'));
  assert.match(source, /notes are your view and are never treated as something said in the meeting/);
  assert.match(source, /Findings are an interpretation of the sources until you confirm them/);
  assert.doesNotMatch(source, /<textarea[^>]*\brequired\b/);
  // Transcript choice, notes save state, seven categories with per-finding source and include box.
  assert.match(source, /type="radio" name="transcript"/);
  assert.match(source, /notesDirty \? "Unsaved changes"/);
  assert.match(source, /addEventListener\("beforeunload"/);
  assert.match(source, /EXTRACTION_FIELDS.map/);
  assert.match(source, /FINDING_SOURCE_LABEL\[item.source\]/);
  assert.match(source, /Nothing captured/);
  // Loading, failure and retry, stale and ready states are all visible.
  assert.match(source, /Loading meeting inputs/);
  assert.match(source, /Retry loading/);
  assert.match(source, /role="alert"/);
  assert.match(source, /data-testid="findings-status"/);
  assert.match(source, /review.extraction.stale_reasons.map\(staleReasonText\)/);
  assert.match(source, /data-testid="v2-ready"/);
  assert.match(source, /data-testid="v2-blockers"/);
  // Analysing and confirming need saved notes and a current analysis; nothing is confirmed implicitly.
  assert.match(source, /disabled=\{editingDisabled \|\| !transcript \|\| notesDirty\} onClick=\{\(\) => void analyse\(\)\}/);
  // The confirm action exists only for a current, not yet confirmed analysis - the same gate as before.
  assert.ok(source.includes(': state !== "current" ? <button className="btn btn-primary" disabled={editingDisabled} onClick={() => void analyse()}>'));
  assert.ok(source.includes(': !confirmedCurrent ? <button className="btn btn-primary" disabled={editingDisabled} onClick={() => void confirm()}>'));
  assert.equal(source.match(/void confirm\(\)/g)?.length, 1, "one explicit confirmation action; nothing confirms by itself");
  // The test extractor is named as such, and preview mode never shows findings.
  assert.match(source, /review\?.execution_mode === "fixture"/);
  assert.match(source, /No AI model is called/);
  assert.match(source, /\{live && review \? <article className=\{styles.card\} aria-labelledby="findings-title"/);
  assert.match(source, /if \(previewMode\) \{ setPreviewFile\(file\)/);
  // Confirming findings and the owner review of the presentation are two different, separately shown steps.
  assert.ok(source.includes('label: "Meeting findings confirmed", value: confirmedCurrent ? `Yes · ${review.confirmation.confirmed_count} included`') && source.includes('testid: "findings-confirmation"'));
  assert.ok(source.includes('label: "Presentation owner review", value: workflowCompleted(workflow, "owner_review") ? "Completed" : v2?.state === "ready" ? "Pending · review V2" : "Pending · after V2"') && source.includes('testid: "presentation-owner-review"'));
  assert.match(source, /is not the owner review of the presentation/);
  assert.doesNotMatch(source, /markOwnerReviewed|workflow\/owner-reviewed|workflow\/finalize/);
  // The old PPT #2 generator is reachable only from the labelled previous flow.
  assert.equal(source.match(/generateAndAwaitPostMeetingPresentation\(/g)?.length, 1);
  assert.equal(source.match(/generateDocuments\(\)/g)?.length, 2, "one definition, one button");
  assert.ok(source.indexOf("Previous document flow</small></summary>") < source.indexOf("onClick={() => void generateDocuments()}"));
  const END_OF_FUNCTION = String.fromCharCode(10) + "  }" + String.fromCharCode(10);
  for (const action of ["onTranscript", "saveNotes", "analyse", "confirm", "saveUseCases"]) {
    const body = source.slice(source.indexOf(`async function ${action}(`), source.indexOf(END_OF_FUNCTION, source.indexOf(`async function ${action}(`)));
    assert.ok(body.length > 0 && !/generateAndAwaitPostMeetingPresentation|prepareMeetingEvidence|ppt2/i.test(body), action);
  }
  // V2 has its own action; the earlier PPT #2 flow is kept apart from it.
  assert.match(source, /Generate Master Presentation V2/);
  assert.match(source, /Previous document flow/);
});

const review = (overrides: Partial<PostMeetingReview> = {}): PostMeetingReview => ({
  opportunity_id: opportunityId, review_fingerprint: "a".repeat(64), execution_mode: "fixture", first_meeting_completed: true, finalized: false,
  transcripts: [{ id: "transcript-1", file_name: "call.txt", processing_status: "pending", created_at: "2026-10-08T09:00:00Z", turn_count: 4, analysed: true }],
  personal_notes: { status: "available", text: "Owner observation", updated_at: "2026-10-08T09:05:00Z" },
  extraction: {
    status: "current", stale_reasons: [], transcript_id: "transcript-1", transcript_file_name: "call.txt",
    generated_at: "2026-10-08T09:10:00Z", execution_mode: "fixture", item_count: 2,
    categories: {
      requirements: [{ text: "Quotes within one day", source: "transcript" }], challenges: [], priorities: [],
      opportunities: [], discussed_solutions: [], decisions: [{ text: "Run a pilot", source: "personal_notes" }], follow_ups: [],
    },
  },
  selected_use_cases: { status: "empty", use_case_ids: [], use_cases: [] },
  approved_discovery: { status: "available", version_id: "discovery-1", version_number: 1 },
  master_presentation: { status: "ready", presentation_id: "deck-1", version_id: "version-1", product_version: "V1" },
  confirmation: { status: "none", stale_reasons: [], confirmed_at: null, items: null, confirmed_count: 0, excluded_count: 0 },
  readiness: { ready_for_v2: false, blockers: ["MEETING_REVIEW_NOT_CONFIRMED"] },
  v2_sources: null,
  ...overrides,
});

test("review responses are validated and never accepted for another opportunity", () => {
  assert.equal(parsePostMeetingReview(review(), opportunityId).extraction.item_count, 2);
  assert.throws(() => parsePostMeetingReview({ ...review(), opportunity_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" }, opportunityId), /another opportunity/);
  assert.throws(() => parsePostMeetingReview({ ...review(), readiness: undefined }, opportunityId), /incomplete/);
  assert.throws(() => parsePostMeetingReview({ ...review(), review_fingerprint: undefined }, opportunityId), /incomplete/, "a review without a fingerprint cannot be confirmed safely");
  assert.throws(() => parsePostMeetingReview({ ...review(), review_fingerprint: "abc" }, opportunityId), /incomplete/);
  const broken = review();
  (broken.extraction.categories as Record<string, unknown>).decisions = [{ text: 5, source: "transcript" }];
  assert.throws(() => parsePostMeetingReview(broken, opportunityId), /incomplete/);
  assert.throws(() => parsePostMeetingReview(null, opportunityId), /incomplete/);
});

test("findings are only called current for the selected transcript and saved notes", () => {
  assert.equal(findingsState(review(), "transcript-1", false), "current");
  assert.equal(findingsState(review(), "transcript-1", true), "notes-unsaved");
  assert.equal(findingsState(review(), "transcript-2", false), "other-transcript");
  assert.equal(findingsState(review(), "", false), "no-transcript");
  assert.equal(findingsState(review({ transcripts: [] }), "transcript-1", false), "no-transcript");
  const stale = review();
  stale.extraction = { ...stale.extraction, status: "stale", stale_reasons: ["NOTES_CHANGED"] };
  assert.equal(findingsState(stale, "transcript-1", false), "stale");
  const missing = review();
  missing.extraction = { ...missing.extraction, status: "missing", transcript_id: null };
  assert.equal(findingsState(missing, "transcript-1", false), "not-analysed");
  assert.match(staleReasonText("NOTES_CHANGED"), /notes changed/);
  assert.match(staleReasonText("TRANSCRIPT_REMOVED"), /removed/);
  assert.equal(staleReasonText("SOMETHING_NEW"), "A source changed.");
  assert.match(blockerText("MEETING_EXTRACTION_STALE"), /again/);
  assert.equal(blockerText("SOMETHING_NEW"), "A required input is missing.");
});

test("headline and exclusions follow the stored confirmation, not the extraction alone", () => {
  assert.equal(reviewHeadline(review()), "Review the findings");
  assert.equal(reviewHeadline(review({ transcripts: [], extraction: { ...review().extraction, status: "missing" } })), "Add your transcript");
  assert.equal(reviewHeadline(review({ extraction: { ...review().extraction, status: "stale" } })), "Analysis is out of date");
  assert.deepEqual(excludedFromConfirmation(review()), {});
  const confirmed = review({
    confirmation: {
      status: "current", stale_reasons: [], confirmed_at: "2026-10-08T09:20:00Z", confirmed_count: 1, excluded_count: 1,
      items: {
        requirements: [{ text: "Quotes within one day", source: "transcript", status: "confirmed" }], challenges: [], priorities: [],
        opportunities: [], discussed_solutions: [], decisions: [{ text: "Run a pilot", source: "personal_notes", status: "excluded" }], follow_ups: [],
      },
    },
    readiness: { ready_for_v2: true, blockers: [] },
  });
  assert.deepEqual(excludedFromConfirmation(confirmed).decisions, ["Run a pilot"]);
  assert.deepEqual(excludedFromConfirmation(confirmed).requirements, []);
  assert.equal(reviewHeadline(confirmed), "Ready for Master Presentation V2");
  // An out-of-date confirmation still offers its exclusions, but only for findings that still exist.
  const outdated = { ...confirmed, confirmation: { ...confirmed.confirmation, status: "stale" as const, stale_reasons: ["USE_CASES_CHANGED"] } };
  assert.deepEqual(excludedFromConfirmation(outdated).decisions, ["Run a pilot"]);
  const reanalysed = { ...outdated, extraction: { ...outdated.extraction, categories: { ...outdated.extraction.categories, decisions: [] } } };
  assert.deepEqual(excludedFromConfirmation(reanalysed).decisions, []);
  assert.equal(reviewHeadline({ ...confirmed, confirmation: { ...confirmed.confirmation, status: "stale" }, readiness: { ready_for_v2: false, blockers: ["MEETING_REVIEW_STALE"] } }), "Confirmation is out of date");
  assert.equal(FINDING_SOURCE_LABEL.personal_notes, "Personal notes");
});

test("confirmation sends the reviewed analysis identity and refuses a stale one before any request", async () => {
  const original = globalThis.fetch;
  const requests: { url: string; method: string; body: unknown }[] = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ url: String(input), method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
    return Response.json(review({ readiness: { ready_for_v2: true, blockers: [] } }));
  };
  try {
    const stale = review();
    stale.extraction = { ...stale.extraction, status: "stale", stale_reasons: ["NOTES_CHANGED"] };
    await assert.rejects(confirmMeetingReview("token", opportunityId, stale, {}), /Analyse the meeting again/);
    assert.equal(requests.length, 0);
    const result = await confirmMeetingReview("token", opportunityId, review(), { decisions: ["Run a pilot"], requirements: [] });
    assert.equal(result.readiness.ready_for_v2, true);
    assert.equal(requests.length, 1);
    assert.match(requests[0].url, new RegExp(`/opportunities/${opportunityId}/post-meeting-review/confirm$`));
    assert.equal(requests[0].method, "POST");
    assert.deepEqual(requests[0].body, {
      transcript_id: "transcript-1", extraction_generated_at: "2026-10-08T09:10:00Z",
      review_fingerprint: "a".repeat(64), excluded: { decisions: ["Run a pilot"] },
    });
    // A source changed between loading and confirming: the API answers 409 and nothing is treated as confirmed.
    globalThis.fetch = async () => new Response(JSON.stringify({ error: { code: "MEETING_REVIEW_STALE", message: "A source of this review changed after it was opened." } }), { status: 409 });
    const refused = await confirmMeetingReview("token", opportunityId, review(), {}).then(() => null, (error: unknown) => error);
    assert.equal(isStaleReviewError(refused), true);
    assert.match(String((refused as Error).message), /changed after it was opened/);
    assert.equal(isStaleReviewError(new Error("network")), false);
    const panel = readFileSync(new URL("../components/MeetingEvidencePanel.tsx", import.meta.url), "utf8");
    assert.match(panel, /if \(!isStaleReviewError\(cause\)\) throw cause;\s+\/\/ Nothing was confirmed[^\n]*\s+await refresh\(token, signal\);/, "a refused confirmation reloads the review");
    assert.match(panel, /nothing was confirmed/);
  } finally { globalThis.fetch = original; }
});

test("notes are saved only when nobody else changed them, and a failed load is an error, not an empty review", async () => {
  const original = globalThis.fetch;
  let stored: PersonalNotes = { text: "Owner observation", updated_at: "2026-10-08T09:05:00Z" };
  const writes: string[] = [];
  globalThis.fetch = async (input, init) => {
    if ((init?.method ?? "GET") === "PUT") {
      const text = (JSON.parse(String(init!.body)) as { text: string }).text.trim();
      writes.push(text);
      stored = { text: text || null, updated_at: "2026-10-08T09:30:00Z" };
    }
    return Response.json(stored);
  };
  try {
    const baseline = { ...stored };
    const saved = await savePersonalNotesChecked("token", opportunityId, "  Updated observation  ", baseline);
    assert.deepEqual(saved, { text: "Updated observation", updated_at: "2026-10-08T09:30:00Z" });
    assert.deepEqual(writes, ["Updated observation"]);
    // The baseline is now out of date: another session (here: the save above) changed the notes.
    await assert.rejects(savePersonalNotesChecked("token", opportunityId, "My other text", baseline), MeetingNotesConflict);
    assert.deepEqual(writes, ["Updated observation"], "a conflicting save writes nothing");
    await assert.rejects(savePersonalNotesChecked("token", opportunityId, "x".repeat(20001), saved), /20,000/);
    globalThis.fetch = async () => new Response(JSON.stringify({ error: { code: "INTERNAL", message: "Database unavailable" } }), { status: 500 });
    await assert.rejects(loadPostMeetingReview("token", opportunityId), /Database unavailable|failed|500/i);
  } finally { globalThis.fetch = original; }
});

test("Figma post-meeting layout: tabs keep both inputs, findings are grouped, the sidebar has one primary action", () => {
  const source = readFileSync(new URL("../components/MeetingEvidencePanel.tsx", import.meta.url), "utf8");
  const css = readFileSync(new URL("../components/post-meeting.module.css", import.meta.url), "utf8");
  // "Upload transcript" and "Type notes" are accessible tabs; both panels stay mounted, so typed notes survive switching.
  assert.ok(source.includes('<div className={styles.inputTabs} role="tablist" aria-label="Meeting input"'));
  assert.ok(source.includes('role="tab" id="tab-transcript" aria-controls="panel-transcript" aria-selected={inputTab === "transcript"}'));
  assert.ok(source.includes('role="tab" id="tab-notes" aria-controls="panel-notes" aria-selected={inputTab === "notes"}'));
  assert.ok(source.includes('hidden={inputTab !== "transcript"}') && source.includes('hidden={inputTab !== "notes"}'));
  assert.equal(source.match(/id="personal-notes"/g)?.length, 1, "one notes field, always rendered");
  assert.ok(source.includes('data-testid="source-metadata"'));
  assert.doesNotMatch(source, /meeting-feedback|meeting_feedback|Meeting feedback|Promised attachments/, "no field for data this journey does not use, and no invented attachments");
  // Findings: grouped per category with a count, source badges, included/excluded totals and a state label.
  assert.ok(source.includes('<details key={key} open={items.length > 0} className={styles.findingGroup} aria-label={label}>'));
  assert.ok(source.includes('<span className={styles.findingCount}>{items.length}</span>'));
  assert.ok(source.includes('data-testid="findings-totals">{found - excludedNow} included · {excludedNow} excluded</strong>'));
  assert.ok(source.includes('{confirmedCurrent ? "Confirmed by you" : state === "current" ? "Analysis · not confirmed" : "Out of date"}'));
  assert.ok(source.includes("styles.sourceBadgeNotes") && source.includes("styles.sourceBadgeBoth"));
  assert.ok(source.includes('disabled={editingDisabled || state !== "current"} onChange={() => toggle(key, item.text)}'), "the include box keeps its gate");
  // Sidebar: a status list from real state and exactly one primary action for the current state.
  const action = source.slice(source.indexOf('data-testid="primary-action"'), source.indexOf("{confirmedCurrent ? <p><small>Confirmed"));
  for (const label of ["Open follow-up email", "Generating Master Presentation V2...", ">Upload transcript<", ">Save notes<", "Analyse meeting", "Confirm meeting information", "Open Master Presentation V2", "Generate Master Presentation V2"]) {
    assert.ok(action.includes(label), label);
  }
  assert.equal(action.match(/ \? </g)?.length ?? 0, 10, "one branch per state: the actions are alternatives, never shown together");
  assert.ok(action.indexOf("v2Generating ?") < action.indexOf("!transcript ?"), "a running job shows a disabled progress button before anything else can be started");
  assert.equal(source.match(/className="btn btn-primary"/g)?.length, 10, "primary buttons exist only in that one slot");
  assert.ok(action.includes('disabled={editingDisabled || !v2.can_generate} onClick={() => void generateV2()}'), "generation keeps the server's gate");
  assert.ok(source.includes('{ key: "v2", label: "Master Presentation V2", value: confirmedCurrent && review.readiness.ready_for_v2 ? v2Label : "After confirmation"'), "never shown as ready before it is");
  // Layout: main column and compact card on desktop, stacked with the status first on smaller screens.
  assert.match(css, /\.inputTabs button \{[^}]*min-width: 168px/);
  assert.match(css, /@media \(max-width: 960px\) \{\s+\.meetingSummary \{ order: -1; display: flex; flex-direction: column; \}/);
  assert.match(css, /@media \(max-width: 640px\) \{ \.inputTabs button \{ flex: 1; min-width: 0; \}/);
  assert.match(css, /\.primaryAction :global\(\.btn\) \{[^}]*width: 100%/);
  const header = readFileSync(new URL("../components/SiteHeader.tsx", import.meta.url), "utf8");
  assert.ok(header.includes(": postMeeting\n    ? copy.sidebar.postMeeting"), "the page title reads Post-meeting on the meeting-input route only");
});

test("polish: keyboard tabs, compact readiness card, and unchanged conflict and use-case handling", () => {
  const source = readFileSync(new URL("../components/MeetingEvidencePanel.tsx", import.meta.url), "utf8");
  const css = readFileSync(new URL("../components/post-meeting.module.css", import.meta.url), "utf8");
  // Tabs: arrow keys, Home and End; only the selected tab is in the tab order; panels are focusable.
  assert.ok(source.includes('<div className={styles.inputTabs} role="tablist" aria-label="Meeting input" onKeyDown={onTabKey}>'));
  assert.ok(source.includes('event.key === "ArrowRight"') && source.includes('event.key === "ArrowLeft"') && source.includes('event.key === "Home"') && source.includes('event.key === "End"'));
  assert.ok(source.includes('tabIndex={inputTab === "transcript" ? 0 : -1}') && source.includes('tabIndex={inputTab === "notes" ? 0 : -1}'));
  assert.ok(source.includes('aria-labelledby="tab-transcript" tabIndex={0} hidden={inputTab !== "transcript"}') && source.includes('aria-labelledby="tab-notes" tabIndex={0} hidden={inputTab !== "notes"}'));
  const key = source.slice(source.indexOf("function onTabKey("), source.indexOf("return <section"));
  assert.doesNotMatch(key, /setNotes|setReview|saveNotes|setExcluded/, "switching tabs never touches typed or saved data");
  assert.ok(source.includes('data-attention={notesDirty ? "true" : undefined}'), "unsaved notes are flagged on the tab that hides them");
  assert.match(source, /addEventListener\("beforeunload"/);
  // One transcript is a plain row; several keep the explicit choice.
  assert.ok(source.includes("{review.transcripts.length > 1") && source.includes('? <input type="radio" name="transcript" checked={item.id === transcriptId} disabled={editingDisabled}'));
  // The readiness card keeps the height of its content and stays in view on desktop only.
  assert.match(css, /\.meetingColumns \{ align-items: start; \}/);
  assert.ok(css.lastIndexOf(".meetingColumns { align-items: start; }") > css.indexOf(".meetingColumns { align-items: stretch; }"), "the later rule wins");
  assert.match(css, /@media \(min-width: 961px\) and \(min-height: 920px\) \{ \.meetingSummary \{ position: sticky; top: 24px; \} \}/);
  assert.match(css, /\.root \.meetingSummary h2 \{[^}]*font-size: 22px/);
  assert.match(css, /\.inputTabs button \{[^}]*min-height: 40px/);
  // Conflict resolution and use-case selection are the handlers that were there before.
  assert.ok(source.includes("onClick={() => adoptNotes(conflictingNotes, false)}>Use saved notes") && source.includes("onClick={() => adoptNotes(conflictingNotes, true)}>Keep my notes"));
  assert.ok(source.includes("disabled={editingDisabled || !notesDirty || Boolean(conflictingNotes)} onClick={() => void saveNotes()}>Save notes"), "no save while a conflict is open");
  assert.ok(source.includes("disabled={editingDisabled || !useCaseDraft} onClick={() => void saveUseCases()}>Save use cases"));
  assert.ok(source.includes('checked={selectedIds.includes(item.fact_id)} disabled={editingDisabled}'), "read-only once finalized");
  assert.ok(source.includes("const editingDisabled = Boolean(busy) || v2Running || finalized"));
});

test("compact readiness summary at 960px and below; the desktop card is unchanged", () => {
  const source = readFileSync(new URL("../components/MeetingEvidencePanel.tsx", import.meta.url), "utf8");
  const css = readFileSync(new URL("../components/post-meeting.module.css", import.meta.url), "utf8");
  // A summary line with progress and a real toggle button that names what it controls.
  assert.ok(source.includes('<span data-testid="readiness-progress">{requiredSteps.filter((row) => row.mark === "done").length} of {requiredSteps.length} steps complete</span>'));
  assert.ok(source.includes('aria-expanded={detailsOpen} aria-controls="readiness-details readiness-notes" onClick={() => setDetailsOpen((open) => !open)}>{detailsOpen ? "Hide details" : "Show details"}</button>'));
  assert.ok(source.includes('<div id="readiness-details" className={styles.readinessDetails} data-collapsed={!detailsOpen}>'));
  assert.ok(source.includes('<div id="readiness-notes" className={styles.readinessDetails} data-collapsed={!detailsOpen}>'));
  assert.ok(source.includes("const [detailsOpen, setDetailsOpen] = useState(false);"), "collapsed until the owner opens it");
  // The checklist, the blockers and the explanations are inside the collapsible parts; the status
  // heading, the V2 messages and the one action are not.
  const details = source.slice(source.indexOf('<div id="readiness-details"'), source.indexOf('{canGenerateV2 ? <div data-testid="v2-ready">'));
  assert.ok(details.includes("<ul className={styles.readiness}>") && details.includes('data-testid="v2-blockers"'));
  const always = source.slice(source.indexOf('{canGenerateV2 ? <div data-testid="v2-ready">'), source.indexOf('<div id="readiness-notes"'));
  assert.ok(always.includes('data-testid="v2-error"') && always.includes('data-testid="v2-progress"') && always.includes('data-testid="primary-action"'));
  assert.ok(source.indexOf('id="readiness-title"') < source.indexOf('data-testid="readiness-summary"'), "the status headline comes first");
  // Still exactly one action slot, with the same gates; the toggle is not a primary button and starts nothing.
  assert.equal(source.match(/data-testid="primary-action"/g)?.length, 1);
  assert.equal(source.match(/className="btn btn-primary"/g)?.length, 10);
  const toggle = source.slice(source.indexOf('data-testid="readiness-toggle"'), source.indexOf("</button>", source.indexOf('data-testid="readiness-toggle"')));
  assert.doesNotMatch(toggle, /analyse|confirm\(|generateV2|saveNotes|fileInput/);
  // CSS: hidden toggle and always-open checklist on desktop; collapsed parts hidden only at 960px and below.
  assert.match(css, /\n\.readinessSummary \{ display: none; \}/);
  const mobile = css.slice(css.indexOf("@media (max-width: 960px) {\n  .meetingSummary { order: -1;"));
  assert.ok(mobile.includes('.readinessDetails[data-collapsed="true"] { display: none; }'));
  assert.ok(mobile.includes(".root .readinessSummary { display: flex;"));
  assert.ok(mobile.includes(".meetingSummary .primaryAction { order: 1;") && mobile.includes(".meetingSummary .readinessDetails { order: 2; }"));
  assert.equal(css.match(/\[data-collapsed="true"\]/g)?.length, 1, "nothing collapses outside that media query");
  assert.match(css, /@media \(min-width: 961px\) and \(min-height: 920px\) \{ \.meetingSummary \{ position: sticky; top: 24px; \} \}/);
});

test("optional personal notes are not a required step in the readiness progress", () => {
  const source = readFileSync(new URL("../components/MeetingEvidencePanel.tsx", import.meta.url), "utf8");
  const css = readFileSync(new URL("../components/post-meeting.module.css", import.meta.url), "utf8");
  // The notes row: done when saved, attention when unsaved, otherwise "optional" - never an open step.
  assert.ok(source.includes('value: notesDirty ? "Unsaved" : baseline.text ? "Saved" : "Optional · none", mark: notesDirty ? "attention" : baseline.text ? "done" : "optional", optional: true'));
  assert.ok(source.includes("const requiredSteps = readiness.filter((row) => !row.optional);"));
  assert.equal(source.match(/optional: true/g)?.length, 1, "only the notes are optional; every other row still counts");
  assert.doesNotMatch(source, /\{readiness\.length\} steps complete/, "the total is the required steps, not all rows");
  assert.equal(source.match(/\{ key: "/g)?.length, 8, "all eight rows are still shown");
  assert.ok(source.includes('row.mark === "optional" ? "–" : ""'));
  assert.match(css, /\.readiness li\[data-mark="optional"\] \.readinessMark, \.readiness li\[data-mark="optional"\] strong \{ color: var\(--pitch-gray-500\); \}/);
  // Whether V2 can be generated still comes from the API, not from this counter.
  assert.ok(source.includes("const canGenerateV2 = Boolean(review?.readiness.ready_for_v2 && confirmedCurrent);"));
  assert.doesNotMatch(source, /requiredSteps[^;\n]*(disabled|can_generate|ready_for_v2)/);
});

test("an analysis without findings is explained and cannot be confirmed or generated", () => {
  const empty = review({
    extraction: { ...review().extraction, item_count: 0, categories: { requirements: [], challenges: [], priorities: [], opportunities: [], discussed_solutions: [], decisions: [], follow_ups: [] } },
    readiness: { ready_for_v2: false, blockers: ["MEETING_FINDINGS_EMPTY", "MEETING_REVIEW_NOT_CONFIRMED"] },
  });
  assert.equal(parsePostMeetingReview(empty, opportunityId).readiness.blockers[0], "MEETING_FINDINGS_EMPTY");
  assert.equal(reviewHeadline(empty), "No findings to confirm");
  assert.equal(reviewHeadline(review()), "Review the findings", "with findings the headline is unchanged");
  assert.match(blockerText("MEETING_FINDINGS_EMPTY"), /found no findings.*at least one confirmed finding/);
  assert.match(blockerText("MEETING_FINDINGS_NONE_CONFIRMED"), /Include at least one finding/);

  const source = readFileSync(new URL("../components/MeetingEvidencePanel.tsx", import.meta.url), "utf8");
  // The status line does not call an empty result a match, and the explanation names the labelled format.
  assert.ok(source.includes('"The analysis ran on the selected transcript and the saved notes, and found no findings."'));
  assert.ok(source.includes('{state === "current" && found === 0 ? <div className={styles.source} data-testid="no-findings">'));
  assert.ok(source.includes('review.extraction.execution_mode === "fixture"'));
  assert.ok(source.includes("A time stamp and a speaker name in front of the label are fine."));
  for (const label of ["Requirement", "Challenge", "Priority", "Opportunity", "Discussed solution", "Decision", "Follow-up"]) assert.ok(source.includes(`“${label}: …”`), label);
  // With nothing found the single primary action is another upload - never a confirmation.
  const actions = source.slice(source.indexOf('data-testid="primary-action"'));
  const none = actions.indexOf(': found === 0 ? <button className="btn btn-primary" data-testid="no-findings-action"');
  const blocked = actions.indexOf(': !confirmedCurrent && found === excludedNow ? <button className="btn btn-primary" data-testid="confirm-blocked" disabled>Confirm meeting information</button>');
  const confirmAction = actions.indexOf(': !confirmedCurrent ? <button className="btn btn-primary" disabled={editingDisabled} onClick={() => void confirm()}>');
  assert.ok(none > 0 && none < blocked && blocked < confirmAction, "empty and all-excluded come before the confirmation");
  assert.ok(source.includes('data-testid="all-excluded"'));
  assert.equal(source.match(/onClick=\{\(\) => void confirm\(\)\}/g)?.length, 1, "confirmation has exactly one entry point");
  // The notice, the readiness row and the blocker list agree with the empty result.
  assert.ok(source.includes('analysedReview.extraction.item_count === 0 ? "Meeting analysed. No findings were found." : "Meeting analysed. Review the findings below."'));
  assert.ok(source.includes('current: review.extraction.item_count === 0 ? "None found" : `${review.extraction.item_count} found` }[state], mark: state === "current" && review.extraction.item_count > 0 ? "done" : analysed ? "attention" : "todo" }'));
  assert.ok(source.includes('review.readiness.blockers.filter((code) => code !== "MEETING_REVIEW_NOT_CONFIRMED" || !review.readiness.blockers.includes("MEETING_FINDINGS_EMPTY"))'));
  // Whether V2 can be generated still comes from the API.
  assert.ok(source.includes("const canGenerateV2 = Boolean(review?.readiness.ready_for_v2 && confirmedCurrent);"));
});
