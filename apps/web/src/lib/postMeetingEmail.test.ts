import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync as readFileFromDisk } from "node:fs";
import { ApiRequestError } from "./api";
import {
  EMAIL_REVIEW_CHECKS, canPreparePostMeetingEmail, clipboardText, advancedReviewUi, announcesAttachment, approvalFor, writerLabel, attachmentMismatch, confirmPostMeetingEmail, emailReviewWarnings, contentWordCount, downloadEmailAttachment,
  emailError, emailExportable, emailFlagText, emailState, exportPostMeetingEmail, formatFileSize, generatePostMeetingEmail,
  isEmailConflict, isEmailHasEdits, loadPostMeetingEmail, openReviewFlags, parsePostMeetingEmail, saveFollowupStatics, savePostMeetingEmail,
  staticsFormFrom, staticsPayload, unsavedLengths, validateStaticsForm, workingCopy,
} from "./postMeetingEmail";
import { showsWorkflowCheckpoints, type PostMeetingWorkflow } from "./postMeeting";
// Sources are compared as text below. A Windows checkout may hold them with CRLF line ends.
const readFileSync = (path: URL | string, encoding: "utf8"): string => readFileFromDisk(path, encoding).replace(/\r\n/g, "\n");

const id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const presentation = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const version = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const text = (subject: string, body: string, edited = false) => ({ subject, body, word_count: 12, edited });
const attachment = (format: "pptx" | "pdf", selected: boolean, available = true) => ({
  format, file_name: `Master Presentation - Acme - V2.${format}`, selected, available, size_bytes: available ? 3_250_000 : null,
  presentation_id: presentation, presentation_version_id: version, version_number: 2, product_version: "V2",
});
const body = "Hi Dana,\n\nthank you for your time in our meeting.\n\nKey points\n- Quotes must go out within one day.\n\nBest regards\nJörg Müller\nManaging Partner - BOREK";
function envelope(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "1.0", opportunity_id: id, journey_stage: "deepening",
    draft: {
      id: "draft-1", status: "draft", send_status: "not_sent", selected_length: null, revision: 3, confirmed_revision: null, confirmed_at: null,
      created_at: "2026-10-09T08:00:00Z", updated_at: "2026-10-09T09:00:00Z",
      lengths: { short: text("Short subject", body), medium: text("Medium subject", body, true), extensive: text("Long subject", body) },
      word_limits: { short: 150, medium: 300, extensive: 500 },
      source: { kind: "master_presentation_v2", product_version: "V2", finalized_at: "2026-10-09T07:00:00Z", presentation_id: presentation, presentation_version_id: version, version_number: 2, snapshot_hash: "a".repeat(64), extraction_execution_mode: "fixture", confirmed_finding_count: 6, excluded_finding_count: 1 },
      source_status: "valid", review_flags: ["fixture_extraction", "action_owner_unconfirmed"], review: null,
      recipients: { to: [{ name: "Dana Weber", email: "dana@acme.example" }], cc: [], sender: { name: "Jörg Müller", role: "Managing Partner", email: "joerg@borek.example" } },
      attachments: [attachment("pptx", false), attachment("pdf", true)],
      ...overrides,
    },
  };
}
const parsed = (overrides: Record<string, unknown> = {}) => parsePostMeetingEmail(envelope(overrides), id)!;

async function withFetch<T>(handler: (url: string, init: RequestInit | undefined) => Response, run: () => Promise<T>) {
  const original = globalThis.fetch;
  const calls: Array<{ url: string; method: string; body: unknown }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ url: String(input), method: init?.method ?? "GET", body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined });
    return handler(String(input), init);
  };
  try { return { result: await run(), calls }; } finally { globalThis.fetch = original; }
}

test("the parser keeps all three lengths, the pinned source and the real attachment selection", () => {
  const draft = parsed();
  assert.equal(draft.revision, 3);
  assert.deepEqual([draft.lengths.short.subject, draft.lengths.medium.subject, draft.lengths.extensive.subject], ["Short subject", "Medium subject", "Long subject"]);
  assert.equal(draft.lengths.medium.edited, true);
  // BAP-12: a draft written before BAP-13 names no language and is English.
  assert.deepEqual(draft.source, { presentationId: presentation, presentationVersionId: version, versionNumber: 2, finalizedAt: "2026-10-09T07:00:00Z", extractionMode: "fixture", confirmedFindings: 6, excludedFindings: 1, language: "en" });
  assert.deepEqual(draft.attachments.map((item) => [item.format, item.selected, item.available]), [["pptx", false, true], ["pdf", true, true]]);
  assert.equal(draft.recipients?.to[0].email, "dana@acme.example");
  assert.equal(parsePostMeetingEmail({ ...envelope(), draft: null }, id), null);
  assert.throws(() => parsePostMeetingEmail({ ...envelope(), opportunity_id: "another" }, id), /does not belong/);
  assert.throws(() => parsePostMeetingEmail({ ...envelope(), journey_stage: "first_contact" }, id), /does not belong/);
  assert.throws(() => parsePostMeetingEmail({ ...envelope(), draft: {} }, id), /incomplete/);
  assert.throws(() => parsePostMeetingEmail(envelope({ send_status: "sent" }), id), /unexpected delivery state/, "a draft is never shown as sent");
  // A draft of an earlier opportunity: no source, nothing to attach.
  const legacy = parsed({ source: null, source_status: "not_applicable", attachments: [], review_flags: [] });
  assert.equal(legacy.source, null);
  assert.deepEqual(legacy.attachments, []);
});

test("preparation requires authoritative review and finalization, not merely a status label", () => {
  const workflow: PostMeetingWorkflow = { opportunity_id: id, current_status: "finalized", steps: [], documents: { ppt1: null, ppt2: null, approved_discovery: null }, finalization: null };
  assert.equal(canPreparePostMeetingEmail(workflow), false);
  workflow.steps = [{ key: "owner_review", state: "completed" }, { key: "finalized", state: "completed" }];
  assert.equal(canPreparePostMeetingEmail(workflow), false);
  workflow.finalization = { id: "snapshot-1" };
  assert.equal(canPreparePostMeetingEmail(workflow), true);
});

test("switching lengths keeps every unsaved edit, and the state follows what is saved", () => {
  const draft = parsed();
  const copy = workingCopy(draft);
  assert.deepEqual(unsavedLengths(draft, copy), []);
  assert.equal(emailState(draft, copy), "saved");
  copy.short = { ...copy.short, subject: "Edited short" };
  copy.extensive = { ...copy.extensive, body: `${body}\nPS` };
  assert.deepEqual(unsavedLengths(draft, copy), ["short", "extensive"], "both edits are still there after looking at another length");
  assert.equal(copy.medium.subject, "Medium subject");
  assert.equal(emailState(draft, copy), "unsaved");
  assert.equal(emailState(null, null), "none");

  const confirmed = parsed({ status: "confirmed", selected_length: "medium", confirmed_revision: 3, confirmed_at: "2026-10-09T10:00:00Z" });
  assert.equal(emailState(confirmed, workingCopy(confirmed)), "confirmed");
  assert.equal(emailExportable(confirmed), true);
  // Edited after confirmation, an older confirmation, or a changed package: not exportable.
  assert.equal(emailState(confirmed, { ...workingCopy(confirmed), medium: { subject: "Changed", body } }), "unsaved");
  assert.equal(emailExportable(parsed({ status: "confirmed", selected_length: "medium", confirmed_revision: 2 })), false);
  assert.equal(emailExportable(parsed({ status: "confirmed", selected_length: "medium", confirmed_revision: 3, source_status: "changed" })), false);
  assert.equal(emailExportable(draft), false);
  assert.equal(clipboardText(confirmed), `Subject: Medium subject\n\n${body}`);
  assert.throws(() => clipboardText(draft), /confirm/);
});

test("content words exclude greeting and signature, as the API counts them", () => {
  assert.equal(contentWordCount(body), 18);
  assert.equal(contentWordCount("Hi Dana,\r\n\r\none two three\r\n\r\nBest regards\r\nJ"), 3);
  assert.equal(formatFileSize(3_250_000), "3.1 MB");
  assert.equal(formatFileSize(20_480), "20 KB");
  assert.equal(formatFileSize(null), "");
});

test("saving names the revision it is based on and a stale save is recognised as a conflict", async () => {
  const draft = parsed();
  const saved = await withFetch(() => Response.json(envelope({ revision: 4, selected_length: "short" })), () =>
    savePostMeetingEmail("token", id, draft, { selectedLength: "short", lengths: { short: { subject: "Edited", body } }, attachments: { pptx: true } }));
  assert.equal(saved.result.revision, 4);
  assert.deepEqual(saved.calls.map((call) => [call.method, call.url.replace(/^.*\/opportunities/, "/opportunities")]), [["PATCH", `/opportunities/${id}/email-drafts/draft-1`]]);
  assert.deepEqual(saved.calls[0].body, { expected_revision: 3, selected_length: "short", lengths: { short: { subject: "Edited", body } }, attachments: { pptx: true } });

  const stale = () => new Response(JSON.stringify({ error: { code: "EMAIL_DRAFT_CONFLICT", message: "changed" } }), { status: 409, headers: { "Content-Type": "application/json" } });
  await assert.rejects(withFetch(stale, () => savePostMeetingEmail("token", id, draft, { lengths: { short: { subject: "Mine", body } } })), (error) => {
    assert.equal(isEmailConflict(error), true);
    assert.equal(isEmailHasEdits(error), false);
    assert.match(emailError(error), /changed elsewhere/);
    return true;
  });
  assert.equal(isEmailHasEdits(new ApiRequestError("x", 409, "EMAIL_DRAFT_HAS_EDITS")), true);
  assert.match(emailError(new ApiRequestError("The short draft has 160 content words; the limit is 150.", 400, "EMAIL_LENGTH_EXCEEDED")), /160 content words/);
  assert.match(emailError(new ApiRequestError("x", 409, "FOLLOWUP_SOURCE_CHANGED")), /Regenerate/);
});

test("generate, confirm and export call their own endpoints and never a delivery endpoint", async () => {
  const draft = parsed();
  const generated = await withFetch(() => Response.json(envelope({ revision: 1 })), () => generatePostMeetingEmail("token", id, true));
  assert.deepEqual(generated.calls[0].body, { journey_stage: "deepening", overwrite_edits: true });
  assert.match(generated.calls[0].url, /\/email-drafts\/generate$/);

  const checks = EMAIL_REVIEW_CHECKS.map(([key]) => key);
  assert.deepEqual(checks, ["recipients", "dates_and_owners", "supported_statements", "review_flags", "tone", "attachments"]);
  const confirmedEnvelope = envelope({ status: "confirmed", selected_length: "medium", revision: 4, confirmed_revision: 4, confirmed_at: "2026-10-09T10:00:00Z" });
  const confirmed = await withFetch(() => Response.json(confirmedEnvelope), () => confirmPostMeetingEmail("token", id, draft, "medium", checks, ["action_owner_unconfirmed", "fixture_extraction"]));
  assert.match(confirmed.calls[0].url, /\/email-drafts\/draft-1\/confirm$/);
  assert.deepEqual(confirmed.calls[0].body, { selected_length: "medium", expected_revision: 3, review_checks: checks, acknowledged_flags: ["fixture_extraction", "action_owner_unconfirmed"] });

  await assert.rejects(() => exportPostMeetingEmail("token", id, draft), /confirm/, "an unconfirmed draft is not requested for export");
  const exported = await withFetch(() => new Response("From: a\r\n\r\nbody", { headers: { "Content-Type": "message/rfc822" } }), () => exportPostMeetingEmail("token", id, confirmed.result));
  assert.match(exported.calls[0].url, /\/email-drafts\/draft-1\/export\?revision=4$/);
  assert.equal(exported.calls[0].method, "GET");
  assert.equal(await exported.result.text(), "From: a\r\n\r\nbody");

  const file = await withFetch(() => new Response("PK"), () => downloadEmailAttachment("token", confirmed.result.attachments[1]));
  assert.match(file.calls[0].url, new RegExp(`/presentations/${presentation}/versions/${version}/download/pdf$`), "the pinned version, not the latest");
  await assert.rejects(() => downloadEmailAttachment("token", { ...confirmed.result.attachments[0], available: false }), /not available/);

  const loaded = await withFetch(() => Response.json(envelope()), () => loadPostMeetingEmail("token", id));
  assert.match(loaded.calls[0].url, new RegExp(`/opportunities/${id}/email-drafts\\?journey_stage=deepening$`));
  const every = [...generated.calls, ...confirmed.calls, ...exported.calls, ...file.calls, ...loaded.calls];
  assert.equal(every.some((call) => /\/send\b|smtp|sendMail/i.test(call.url)), false);
});

test("recipient and sender are entered by the owner and validated before saving", async () => {
  const empty = staticsFormFrom(null, { companyName: "Acme GmbH", contactPerson: "Dana Weber" });
  assert.deepEqual([empty.clientShort, empty.recipientFirstName, empty.recipientLastName, empty.recipientEmail, empty.senderEmail], ["Acme GmbH", "Dana", "Weber", "", ""], "names are prefilled from the contact person, addresses are never invented");
  assert.match(validateStaticsForm(empty) ?? "", /project name/);
  const form = { ...empty, projectName: "Acme Quoting", recipientEmail: "dana@acme.example", senderName: "Jörg Müller", senderRole: "Managing Partner", senderEmail: "joerg@borek.example" };
  assert.equal(validateStaticsForm(form), null);
  assert.match(validateStaticsForm({ ...form, recipientEmail: "dana" }) ?? "", /recipient's email/);
  assert.match(validateStaticsForm({ ...form, recipientFirstName: " " }) ?? "", /first name/);
  assert.match(validateStaticsForm({ ...form, style: "formal" }) ?? "", /salutation/);
  assert.equal(validateStaticsForm({ ...form, style: "formal", recipientSalutation: "Ms" }), null);
  assert.match(validateStaticsForm({ ...form, senderEmail: "" }) ?? "", /sender's email/);

  const previous = { project_name: "Old", client_short: "Acme", salutation_style: "informal", standard_recipients: [
    { email: "old@acme.example", first_name: "Old", kind: "to", primary: true }, { email: "cc@acme.example", first_name: "Tom", kind: "cc" },
  ], sender_profile: { name: "A", role: "B", email: "a@borek.example" } };
  const payload = staticsPayload(form, previous);
  assert.deepEqual(payload.standard_recipients.map((item) => [item.email, item.kind, item.primary ?? false]), [["dana@acme.example", "to", true], ["cc@acme.example", "cc", false]], "a stored Cc recipient is kept");
  assert.equal(staticsFormFrom(payload, { companyName: "x", contactPerson: "y z" }).recipientFirstName, "Dana");

  const saved = await withFetch(() => Response.json({ followup_statics: payload }), () => saveFollowupStatics("token", id, form, previous));
  assert.deepEqual(saved.calls.map((call) => [call.method, call.url.replace(/^.*\/opportunities/, "/opportunities")]), [["PATCH", `/opportunities/${id}`]]);
  assert.deepEqual(saved.calls[0].body, { followup_statics: payload });
});

test("review flags are explained in plain words, including that demo extraction is not live AI", () => {
  assert.match(emailFlagText("fixture_extraction"), /demo extraction, not from a live AI analysis/);
  assert.match(emailFlagText("owner_notes_omitted"), /not said in the meeting/);
  assert.match(emailFlagText("action_date_unconfirmed"), /date to be confirmed/);
  assert.match(emailFlagText("something_new"), /something new/);
});

const panel = readFileSync(new URL("../components/FollowUpDraftPanel.tsx", import.meta.url), "utf8");
// The original on-page review, kept behind NEXT_PUBLIC_FOLLOWUP_ADVANCED_REVIEW_UI.
const advanced = readFileSync(new URL("../components/FollowUpAdvancedReview.tsx", import.meta.url), "utf8");
const dialog = readFileSync(new URL("../components/FollowUpReviewDialog.tsx", import.meta.url), "utf8");
const css = readFileSync(new URL("../components/post-meeting.module.css", import.meta.url), "utf8");

test("the email screen saves on the server, reviews the saved revision and never sends", () => {
  // Editing is held per length and saved through the API, not in browser storage.
  assert.match(panel, /setCopy\(\{ \.\.\.copy, \[length\]: \{ \.\.\.text, subject: event.target.value \} \}\)/);
  assert.match(panel, /setCopy\(\{ \.\.\.copy, \[length\]: \{ \.\.\.text, body: event.target.value \} \}\)/);
  assert.match(panel, /savePostMeetingEmail\(token, opportunityId, draft, \{ selectedLength: length, lengths \}\)/);
  assert.doesNotMatch(panel, /localStorage|sessionStorage/);
  assert.match(panel, /window.addEventListener\("beforeunload", warn\)/);
  // Conflict and regenerate are explicit decisions.
  assert.match(panel, /if \(isEmailConflict\(cause\)\) setConflict\(true\)/);
  assert.match(panel, /data-testid="email-conflict"/);
  assert.match(panel, /Load the saved version/);
  assert.match(panel, /Keep my text and review/);
  assert.match(panel, /data-testid="email-regenerate-warning"/);
  assert.match(panel, /Regenerating replaces all three lengths/);
  // The review is of the saved text and needs every check.
  assert.match(panel, /const allChecked = EMAIL_REVIEW_CHECKS.every\(\(\[key\]\) => checks.includes\(key\)\)/);
  assert.match(advanced, /disabled=\{working \|\| dirty.length > 0\} data-testid="email-checklist"/);
  assert.match(panel, /&& dirty.length === 0 && !overLimit && !conflict && draft!.sourceStatus !== "changed"/);
  assert.match(panel, /setChecks\(\[\]\)/, "a new revision needs a new review");
  // Export only of a confirmed draft; every statement about delivery says that nothing is sent.
  assert.match(panel, /data-testid="email-export" disabled=\{!exportable \|\| working\}/);
  assert.match(panel, /data-testid="email-copy" disabled=\{!exportable \|\| working\}/);
  assert.match(panel, /Nothing is sent from this application/);
  assert.match(panel, /Draft confirmed\. Nothing was sent\./);
  assert.doesNotMatch(panel, /Send email|>Send<|mailto:|\/send|smtp/i, "the Figma 'Send email' action is an export here");
  assert.doesNotMatch(panel, /concretisation/);
});

test("the package sidebar shows the pinned V2 version and a real attachment selection", () => {
  const sidebar = panel.slice(panel.indexOf("<aside"), panel.indexOf("</aside>") + "</aside>".length);
  let previous = -1;
  for (const heading of ["Updated presentation", "What the email is based on", "Email attachments", "Next step"]) {
    const index = sidebar.indexOf(heading);
    assert.ok(index > previous, `${heading} follows the Figma section order`);
    previous = index;
  }
  assert.match(sidebar, /styles.card.*styles.packageSidebar/);
  assert.match(sidebar, /Open in Deck Center/);
  assert.match(sidebar, /draft\?\.attachments.filter\(\(item\) => item.artifact === "master_presentation_v2"\).map\(\(item\) => <button key=\{item.key\}/, "PPTX and PDF downloads of the pinned version");
  // BAP-7: every generated document is listed with what it is, and can be downloaded and selected by its key.
  assert.ok(sidebar.includes("data-testid={`email-attach-${item.key}`}") && sidebar.includes("{ATTACHMENT_ARTIFACT_LABEL[item.artifact]}"));
  assert.match(sidebar, /never a newer one/);
  assert.match(sidebar, /type="checkbox" checked=\{item.selected\}/);
  assert.match(sidebar, /Not available — this file cannot be attached/);
  assert.match(sidebar, /A file that is not selected is not attached/);
  assert.match(sidebar, /not by a live AI analysis/);
  assert.doesNotMatch(sidebar, /\d+(?:\.\d+)? MB|Acme|Northwind|Opportunity Analysis\.pdf|Delivery Overview/, "no invented documents or sizes");
  // Earlier standalone PPT #2 opportunities keep their guarded download.
  assert.match(panel, /!master && !draft\?\.source \? <StandalonePresentationCard/);
  assert.match(panel, /downloadPostMeetingPresentation\(token, opportunityId, deck, "pptx"\)/);
  assert.doesNotMatch(panel, /loadExistingFirstPitch|downloadLivePresentation/);
});

test("layout follows the Figma frame and stacks on tablet and mobile", () => {
  assert.match(css, /\.followUpColumns\s*\{[^}]*align-items: stretch/);
  assert.match(css, /\.packageSidebar\s*\{[^}]*display: flex;[^}]*flex-direction: column/);
  assert.match(css, /\.packageExport\s*\{[^}]*margin-top: auto/);
  assert.match(css, /\.packageExport :global\(\.btn-primary\)\s*\{[^}]*width: 100%/);
  assert.match(css, /\.emailFields > div\s*\{[^}]*grid-template-columns: 96px minmax\(0, 1fr\)/, "label column as in the To / Subject rows");
  assert.match(css, /\.emailLengths\s*\{[^}]*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(css, /\.phaseNavThree\s*\{[^}]*repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(css, /@media \(max-width: 960px\)\s*\{\s*\.columns, \.followUpColumns\s*\{\s*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(css, /@media \(max-width: 640px\)[^\n]*\.emailStaticsGrid, \.emailLengths \{ grid-template-columns: minmax\(0, 1fr\); \}/);
  assert.match(css, /@media \(max-width: 640px\)[^\n]*\.emailFields > div, \.root \.emailSubject \{ grid-template-columns: minmax\(0, 1fr\)/);
  const shell = readFileSync(new URL("../components/PostMeetingShell.tsx", import.meta.url), "utf8");
  assert.match(shell, /<span>4<\/span><div><small>Follow-up email<\/small><strong>Review, confirm &amp; export<\/strong>/);
  assert.match(shell, /<span>2<\/span><div><small>Discovery 2<\/small><strong>Review, edit &amp; approve<\/strong>/, "BAP-7: Discovery 2 is the step before V2");
});

test("the email route has its own page title and is reachable from the finalized owner checkpoint", () => {
  const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
  const header = read("../components/SiteHeader.tsx");
  assert.ok(header.includes("const followUpEmail = /^\\/opportunities\\/[^/]+\\/follow-up\\/?$/.test(pathname);"), "only the follow-up route");
  assert.match(header, /const title = followUpEmail\s+\? copy\.header\.followUpEmail\s+: postMeeting\s+\? copy\.sidebar\.postMeeting\s+: pathname\.startsWith/);
  assert.ok(header.includes('{followUpEmail ? <p className="pitch-page-subtitle">{copy.header.followUpEmailSubtitle}</p> : null}'));
  assert.ok(header.includes("? copy.header.pitchGeneration"), "other pitch routes keep their title");
  const route = /^\/opportunities\/[^/]+\/follow-up\/?$/;
  assert.deepEqual(["/opportunities/abc/follow-up", "/opportunities/abc/follow-up/", "/opportunities/abc/meeting", "/opportunities/abc/post-meeting-presentation", "/opportunities/new/client-information", "/clients"].map((path) => route.test(path)),
    [true, true, false, false, false, false]);
  const language = read("../components/LanguageProvider.tsx");
  assert.ok(language.includes('followUpEmail: "Follow-up Email", followUpEmailSubtitle: "Review, edit and prepare your client follow-up."'));
  assert.ok(language.includes('followUpEmail: "Follow-up-E-Mail"'));
  const checkpoint = read("../components/OwnerCheckpointPanel.tsx");
  assert.match(checkpoint, /\{finalized\s+\? <div className=\{styles\.nextStep\} data-testid="finalized-next-step"/, "shown only once the documents are finalized");
  assert.ok(checkpoint.includes('data-testid="continue-to-follow-up" href={`/opportunities/${encodeURIComponent(opportunityId)}/follow-up`}>Continue to the follow-up email</Link>'));
  assert.ok(checkpoint.includes("disabled={disabled || !identity || confirmation !== identity || !reviewed} onClick={() => void act(true)}>Finalize documents"), "the finalization gate is unchanged");
  assert.ok(panel.includes('<PostMeetingPhaseNav opportunityId={opportunityId} active="review" />'));
  assert.ok(read("../components/PostMeetingPresentationWorkspace.tsx").includes("<Link href={`${root}/follow-up`}>Follow-up email</Link>"));
});

test("only the review flags the owner ticked are sent; an unticked flag is never acknowledged for them", async () => {
  const draft = parsed();
  assert.deepEqual(draft.reviewFlags, ["fixture_extraction", "action_owner_unconfirmed"]);
  assert.deepEqual(openReviewFlags(draft, []), ["fixture_extraction", "action_owner_unconfirmed"]);
  assert.deepEqual(openReviewFlags(draft, ["fixture_extraction"]), ["action_owner_unconfirmed"]);
  assert.deepEqual(openReviewFlags(draft, ["action_owner_unconfirmed", "fixture_extraction"]), []);
  assert.deepEqual(openReviewFlags(null, []), []);

  const checks = EMAIL_REVIEW_CHECKS.map(([key]) => key);
  const refuse = () => new Response(JSON.stringify({ error: { code: "EMAIL_REVIEW_FLAGS_OPEN", message: "Acknowledge every review flag first: action_owner_unconfirmed." } }), { status: 400, headers: { "Content-Type": "application/json" } });
  const sent: unknown[] = [];
  for (const ticked of [[], ["fixture_extraction"], ["not_a_flag_of_this_draft", "fixture_extraction"]]) {
    const original = globalThis.fetch;
    globalThis.fetch = async (_input, init) => { sent.push(JSON.parse(String(init?.body)).acknowledged_flags); return refuse(); };
    try {
      await assert.rejects(() => confirmPostMeetingEmail("token", id, draft, "medium", checks, ticked), (error) => {
        assert.match(emailError(error), /Acknowledge every review flag/);
        return true;
      });
    } finally { globalThis.fetch = original; }
  }
  assert.deepEqual(sent, [[], ["fixture_extraction"], ["fixture_extraction"]], "exactly the ticked flags of this draft, nothing added");

  // The screen keeps one tick per flag, starts every draft revision with none, and gates the button on them.
  assert.ok(panel.includes("const [acknowledged, setAcknowledged] = useState<string[]>([]);"));
  assert.ok(panel.includes("setChecks([]);\n    setAcknowledged([]);"), "a new or changed draft needs new acknowledgements");
  assert.ok(advanced.includes("data-testid={`email-flag-${flag}`} checked={acknowledged.includes(flag)}"));
  assert.ok(advanced.includes("setAcknowledged(event.target.checked ? [...acknowledged, flag] : acknowledged.filter((item) => item !== flag))"));
  assert.ok(panel.includes("confirmPostMeetingEmail(token, opportunityId, draft, length, checks, acknowledged)"));
  assert.ok(panel.includes("(!needsReview || (allChecked && openFlags.length === 0))"));
  const lib = readFileSync(new URL("./postMeetingEmail.ts", import.meta.url), "utf8");
  assert.ok(!lib.includes("acknowledged_flags: draft.reviewFlags }"), "the flags are no longer acknowledged wholesale");
  assert.match(emailFlagText("speaker_unverified"), /not who made them/);
  assert.match(emailError(new ApiRequestError("x", 400, "FOLLOWUP_CONTENT_TOO_LONG")), /never shortened automatically/);
});

test("the default screen has no review clutter: one Review & confirm action opens the dialog", () => {
  // The two long blocks are not part of the default page; they live in the legacy component only.
  for (const text of ["Review flags — acknowledge each one", "<legend>Checklist</legend>", 'data-testid="email-checklist"', "data-testid={`email-flag-${flag}`}"]) {
    assert.ok(!panel.includes(text), text);
    assert.ok(advanced.includes(text), `${text} is preserved in the legacy review`);
  }
  assert.ok(panel.includes("{draft && ADVANCED_REVIEW ? <FollowUpAdvancedReview "), "the legacy card renders only with the switch");
  assert.ok(panel.includes('<button className="btn btn-primary" data-testid="email-review-open" disabled={!canReview} onClick={() => { setReviewError(null); setReview("approve"); }}>Review &amp; confirm</button>'));
  assert.ok(panel.includes("const canReview = Boolean(draft) && !working && dirty.length === 0 && !overLimit && !conflict && !exportable;"), "the review is of the saved text");
  assert.ok(panel.includes("{draft && review && !ADVANCED_REVIEW ? <FollowUpReviewDialog "));
  // One next action in the sidebar: review until confirmed, export afterwards.
  assert.ok(panel.includes("{exportable || ADVANCED_REVIEW ? <>"));
  assert.ok(panel.includes('className={dirty.length ? "btn btn-primary" : "btn btn-secondary"} data-testid="email-save"'), "saving is the primary action only while there is something to save");
  assert.doesNotMatch(panel + dialog, /Send email|>Send<|mailto:|\/send|smtp/i);
});

test("the legacy review is restored only by the exact switch value", () => {
  assert.equal(advancedReviewUi(undefined), false);
  for (const value of ["", "false", "1", "TRUE", "yes", "on"]) assert.equal(advancedReviewUi(value), false, value);
  assert.equal(advancedReviewUi("true"), true);
  assert.equal(advancedReviewUi(" true "), true);
  const lib = readFileSync(new URL("./postMeetingEmail.ts", import.meta.url), "utf8");
  assert.ok(lib.includes("process.env.NEXT_PUBLIC_FOLLOWUP_ADVANCED_REVIEW_UI"));
  assert.ok(panel.includes("const ADVANCED_REVIEW = advancedReviewUi();"));
  const dockerfile = readFileSync(new URL("../../../../docker/web/Dockerfile", import.meta.url), "utf8");
  assert.match(dockerfile, /ARG NEXT_PUBLIC_FOLLOWUP_ADVANCED_REVIEW_UI=false/);
  assert.match(dockerfile, /ENV NEXT_PUBLIC_FOLLOWUP_ADVANCED_REVIEW_UI=\$NEXT_PUBLIC_FOLLOWUP_ADVANCED_REVIEW_UI/);
  assert.match(readFileSync(new URL("../../../../.env.example", import.meta.url), "utf8"), /NEXT_PUBLIC_FOLLOWUP_ADVANCED_REVIEW_UI=false/);
  const doc = readFileSync(new URL("../../../../docs/followup-email-review-ui.md", import.meta.url), "utf8");
  assert.match(doc, /NEXT_PUBLIC_FOLLOWUP_ADVANCED_REVIEW_UI=true/);
  assert.match(doc, /FollowUpAdvancedReview\.tsx/);
  // Both interfaces confirm through the same call.
  assert.equal((panel.match(/confirmPostMeetingEmail\(/g) ?? []).length, 2);
  assert.ok(!advanced.includes("apiFetch") && !dialog.includes("apiFetch") && !dialog.includes("confirmPostMeetingEmail"), "neither review component talks to the API itself");
});

test("the review dialog keeps every warning visible, including what the current selection adds", () => {
  const draft = parsed();
  const flags = emailReviewWarnings(draft, "medium");
  assert.deepEqual(flags.filter((item) => item.flag).map((item) => item.key), draft.reviewFlags, "every stored review flag is listed");
  assert.match(flags.find((item) => item.key === "fixture_extraction")!.text, /not from a live AI analysis/);
  assert.ok(flags.every((item) => !item.blocking));

  const noFile = { ...draft, attachments: draft.attachments.map((item) => ({ ...item, selected: false })) };
  const announced = { ...noFile, lengths: { ...noFile.lengths, medium: { ...noFile.lengths.medium, body: "Hi Dana,\n\nPlease find attached the presentation.\n\nBest regards\nJ" } } };
  const contradiction = emailReviewWarnings(announced, "medium");
  assert.ok(!contradiction.some((item) => item.key === "no_attachment_selected"));
  assert.deepEqual(contradiction.filter((item) => item.blocking).map((item) => item.key), ["attachment_contradiction"], "a contradiction is resolved, not ticked");
  assert.match(contradiction.find((item) => item.key === "attachment_contradiction")!.text, /says that something is attached, but no file is selected/);
  const silent = { ...noFile, lengths: { ...noFile.lengths, medium: { ...noFile.lengths.medium, body: "Hi Dana,\n\nThank you.\n\nBest regards\nJ" } } };
  assert.match(emailReviewWarnings(silent, "medium").find((item) => item.key === "no_attachment_selected")!.text, /without an attachment/);
  assert.ok(!emailReviewWarnings({ ...draft, attachments: [] }, "medium").some((item) => item.key === "no_attachment_selected"), "a draft without approved files has nothing to select");

  const missing = { ...draft, attachments: draft.attachments.map((item) => ({ ...item, selected: true, available: item.format !== "pdf" })) };
  const blocked = emailReviewWarnings(missing, "medium").filter((item) => item.blocking);
  assert.deepEqual(blocked.map((item) => item.key), ["attachment_unavailable_pdf"]);
  assert.match(blocked[0].text, /selected but not available/);
  const changed = emailReviewWarnings({ ...draft, sourceStatus: "changed" }, "medium").find((item) => item.key === "source_changed")!;
  assert.ok(changed.blocking && /Regenerate the email/.test(changed.text));

  // The dialog lists them all, and a blocking one disables the approval.
  assert.ok(dialog.includes("const warnings = emailReviewWarnings(draft, length);"));
  assert.ok(dialog.includes("{warnings.map((item) => <li key={item.key} data-testid={`email-warning-${item.key}`}"));
  assert.ok(dialog.includes("const canApprove = !busy && blocking.length === 0 && open.length === 0 && checksConfirmed;"));
  assert.ok(dialog.includes('data-testid="email-approve" disabled={!canApprove} onClick={() => onApprove(acknowledgedFlags, checksConfirmed)}'));
  // A blocking point has no tick: it cannot be accepted, only resolved.
  assert.ok(dialog.includes("{item.blocking || readOnly\n              ? <span>{item.blocking ? <strong>Cannot be approved: </strong> : null}{item.text}</span>"));
  for (const shown of ["recipients.to.map(personLabel)", "recipients.sender.name", "{text.subject}", "{text.body}", "files.map((item) =>", "EMAIL_REVIEW_CHECKS.map(([key, label]) => <li key={key}>{label}</li>)"]) {
    assert.ok(dialog.includes(shown), shown);
  }
});

test("the approval records exactly what the owner ticked: each flag, and the six checks only when confirmed", async () => {
  const draft = parsed();
  const six = EMAIL_REVIEW_CHECKS.map(([key]) => key);
  // Nothing ticked: nothing is reported. One generic tick is never turned into six checks or into flags.
  assert.deepEqual(approvalFor(draft, [], false), { checks: [], acknowledgedFlags: [] });
  assert.deepEqual(approvalFor(draft, draft.reviewFlags, false).checks, [], "flags alone do not confirm the review checks");
  assert.deepEqual(approvalFor(draft, [], true), { checks: six, acknowledgedFlags: [] }, "the checks alone acknowledge no flag");
  assert.deepEqual(approvalFor(draft, ["fixture_extraction"], true).acknowledgedFlags, ["fixture_extraction"], "only the ticked flag");
  assert.deepEqual(approvalFor(draft, ["fixture_extraction", "not_a_flag_of_this_draft"], true).acknowledgedFlags, ["fixture_extraction"]);
  assert.deepEqual(approvalFor(draft, draft.reviewFlags, true), { checks: six, acknowledgedFlags: draft.reviewFlags });

  // The dialog: one tick per point, none preset, and the six checks written out next to their own tick.
  assert.ok(dialog.includes("const [acknowledged, setAcknowledged] = useState<string[]>([]);"));
  assert.ok(dialog.includes("const [checksConfirmed, setChecksConfirmed] = useState(false);"));
  assert.ok(dialog.includes("<input type=\"checkbox\" data-testid={`email-acknowledge-${item.key}`} checked={acknowledged.includes(item.key)} disabled={busy}"));
  assert.ok(dialog.includes("setAcknowledged(event.target.checked ? [...acknowledged, item.key] : acknowledged.filter((key) => key !== item.key))"));
  assert.ok(dialog.includes("const acknowledgedFlags = warnings.filter((item) => item.flag && acknowledged.includes(item.key)).map((item) => item.key);"), "only ticked flags are reported");
  assert.ok(dialog.includes("<ol>{EMAIL_REVIEW_CHECKS.map(([key, label]) => <li key={key}>{label}</li>)}</ol>"), "all six requirements are shown");
  assert.ok(dialog.includes('<input type="checkbox" data-testid="email-review-checks" checked={checksConfirmed} onChange={(event) => setChecksConfirmed(event.target.checked)} />'));
  assert.ok(dialog.includes("I have checked all six points above and confirm each of them for this email."));
  assert.doesNotMatch(dialog, /setAcknowledged\(warnings|setAcknowledged\(draft|setChecksConfirmed\(true\)|useState\(true\)|defaultChecked|reviewFlags\)/, "nothing is ticked for the owner");
  assert.doesNotMatch(dialog, /I have read this email and checked the recipients/, "the single generic acknowledgement is gone");
  assert.equal((panel.match(/approve\(/g) ?? []).length, 1, "the approval has one caller");
  assert.ok(panel.includes("onApprove={(acknowledgedFlags, checksConfirmed) => void approve(acknowledgedFlags, checksConfirmed)}"));
  assert.ok(panel.includes("const approval = approvalFor(draft, acknowledgedFlags, checksConfirmed);"));
  assert.ok(panel.includes("confirmPostMeetingEmail(token, opportunityId, draft, length, approval.checks, approval.acknowledgedFlags)"));
  assert.doesNotMatch(panel, /useEffect\([^)]*approve/);

  // The request is the existing confirmation: saved revision, the confirmed checks, the ticked flags.
  const original = globalThis.fetch;
  const sent: Record<string, unknown>[] = [];
  let url = "";
  globalThis.fetch = async (input, init) => { url = String(input); sent.push(JSON.parse(String(init?.body))); return Response.json(envelope({ status: "confirmed", selected_length: "medium", confirmed_revision: 4, revision: 4, confirmed_at: "2026-10-12T09:00:00Z" })); };
  try {
    for (const approval of [approvalFor(draft, draft.reviewFlags, true), approvalFor(draft, ["fixture_extraction"], false)]) {
      await confirmPostMeetingEmail("token", id, draft, "medium", approval.checks, approval.acknowledgedFlags);
    }
  } finally { globalThis.fetch = original; }
  assert.match(url, /\/email-drafts\/draft-1\/confirm$/);
  assert.deepEqual(sent[0], { selected_length: "medium", expected_revision: draft.revision, review_checks: ["recipients", "dates_and_owners", "supported_statements", "review_flags", "tone", "attachments"], acknowledged_flags: draft.reviewFlags });
  assert.deepEqual(sent[1], { selected_length: "medium", expected_revision: draft.revision, review_checks: [], acknowledged_flags: ["fixture_extraction"] }, "an incomplete review is sent as incomplete; the API refuses it");

  // A refusal by the API stays visible in the dialog; a conflict returns to the page decision.
  assert.ok(panel.includes("if (isEmailConflict(cause)) { setReview(null); throw cause; }"));
  assert.ok(panel.includes("setReviewError(emailError(cause));"));
  assert.ok(dialog.includes('role="alert" data-testid="email-review-error"'));
});

test("the page keeps text and attachment selection consistent without rewriting the owner's text", () => {
  const draft = parsed();
  const none = { ...draft, attachments: draft.attachments.map((item) => ({ ...item, selected: false })) };
  const says = (body: string) => ({ ...none.lengths.medium, body });
  const mixed = { ...none, lengths: { short: says("Hi Dana,\n\nThank you.\n\nBest regards\nJ"), medium: says("Hi Dana,\n\nAs discussed, I have attached the presentation.\n\nBest regards\nJ"), extensive: says("Hi Dana,\n\nSee the attachment.\n\nBest regards\nJ") } };
  assert.equal(announcesAttachment("Please find attached the presentation."), true);
  assert.equal(announcesAttachment("Thank you for your time."), false);
  assert.equal(announcesAttachment("The outline is enclosed."), true);
  for (const opposite of ["There is no attachment this time.", "Nothing is attached to this message.", "I have not attached the deck yet."]) assert.equal(announcesAttachment(opposite), false, opposite);
  assert.equal(announcesAttachment("No attachment last time. This time the deck is attached."), true);
  assert.match(emailError(new ApiRequestError("x", 400, "EMAIL_ATTACHMENT_CONTRADICTION")), /Select the approved file or change that sentence/);
  assert.deepEqual(attachmentMismatch(mixed), ["medium", "extensive"]);
  assert.deepEqual(attachmentMismatch({ ...mixed, attachments: draft.attachments.map((item) => ({ ...item, selected: item.format === "pdf" })) }), [], "with a file selected the sentence is true");
  assert.deepEqual(attachmentMismatch({ ...mixed, attachments: [] }), [], "a draft without approved files has no selection");
  assert.deepEqual(attachmentMismatch(null), []);
  // The page says which texts the API rewrote and that edited texts were left alone, and points at a remaining mismatch.
  assert.ok(panel.includes("const rewritten = EMAIL_LENGTHS.filter((name) => saved.lengths[name].body !== draft.lengths[name].body);"));
  assert.ok(panel.includes("Texts you edited were not changed."));
  assert.ok(panel.includes('data-testid="email-attachment-mismatch"') && panel.includes("it cannot be approved like this"));
  assert.doesNotMatch(panel, /setCopy\([^)]*replace\(|body\.replace\(/, "the page itself never edits the message text");
});

test("the review dialog is a modal dialog that works with the keyboard and on a phone", () => {
  assert.ok(dialog.includes("<dialog ref={dialog} className={styles.reviewDialog} aria-labelledby=\"email-review-dialog-title\" aria-describedby=\"email-review-dialog-note\""));
  assert.ok(dialog.includes("if (element && !element.open) element.showModal();"), "modal: focus stays inside, Escape closes, focus returns");
  assert.ok(dialog.includes("onCancel={(event) => { if (busy) event.preventDefault(); }} onClose={onClose}"));
  assert.ok(dialog.includes('<pre className={styles.reviewBody} tabIndex={0}'), "the message can be scrolled with the keyboard");
  assert.ok(dialog.includes('<label className={styles.check}>'), "the acknowledgement has a visible label");
  const css = readFileSync(new URL("../components/post-meeting.module.css", import.meta.url), "utf8");
  assert.match(css, /\.reviewDialog \{ width: min\(760px, calc\(100vw - 32px\)\);[^}]*max-height: calc\(100dvh - 32px\)/);
  assert.match(css, /\.reviewScroll \{[^}]*overflow-y: auto/);
  assert.match(css, /\.reviewBody \{[^}]*white-space: pre-wrap; overflow-wrap: anywhere/);
  assert.match(css, /\.reviewWarnings li\[data-blocking="true"\] \{ border-left-color: var\(--pitch-red\)/);
  assert.match(css, /@media \(max-width: 640px\) \{\s*\.reviewDialog \{ width: calc\(100vw - 16px\)/);
  // The legacy styles stay for the switch.
  assert.match(css, /\.emailChecklist \.check \{ margin: 8px 0; \}/);
});

test("the page says who wrote the text and never words a fallback as AI-written", () => {
  const writer = (value: unknown) => parsed({ writer: value }).writer;
  const ai = writer({ mode: "ai", model: "claude-sonnet-4-5", prompt_version: "followup-email-writer:v1", verified: true, fallback_reason: null, checked_sentences: 15 });
  assert.deepEqual(ai, { mode: "ai", model: "claude-sonnet-4-5", verified: true, fallbackReason: null, checkedSentences: 15 });
  assert.equal(writerLabel(ai), "Written by the AI writer (claude-sonnet-4-5) from your confirmed findings · 15 sentences passed the grounding check.");
  const fallback = writer({ mode: "fallback", model: "claude-sonnet-4-5", prompt_version: "followup-finalized:v3", verified: false, fallback_reason: { code: "AI_GROUNDING_REJECTED", message: "The AI draft failed 2 grounding or format checks." }, checked_sentences: null });
  assert.equal(writerLabel(fallback), "Rule-based writer (fallback) — the AI draft was not used: The AI draft failed 2 grounding or format checks.");
  assert.doesNotMatch(writerLabel(fallback)!, /Written by the AI/);
  const rules = writer({ mode: "deterministic", model: null, prompt_version: "followup-finalized:v3", verified: false, fallback_reason: null, checked_sentences: null });
  assert.equal(writerLabel(rules), "Rule-based writer — AI writing is not enabled in this environment.");
  // A fallback that claims to be verified, an unverified "ai" draft, or an unknown mode is not labelled AI-written.
  assert.equal(writer({ mode: "fallback", verified: true, fallback_reason: null })!.verified, false);
  assert.match(writerLabel(writer({ mode: "fallback", verified: true, fallback_reason: null }))!, /^Rule-based writer \(fallback\)/);
  assert.doesNotMatch(writerLabel(writer({ mode: "ai", verified: false, model: "m", fallback_reason: null })) ?? "", /Written by the AI/);
  assert.equal(writer({ mode: "gpt" }), null);
  assert.equal(parsed().writer, null, "a draft from before the writer was recorded has no label");
  assert.equal(writerLabel(null), null);
  assert.ok(panel.includes('{writerLabel(draft.writer) ? <small data-testid="email-writer" data-writer={draft.writer?.mode}>'));
  // The flags of both modes are explained and acknowledged like every other flag.
  assert.match(emailFlagText("ai_written"), /written by the AI writer.*read it against the findings yourself/);
  assert.match(emailFlagText("ai_fallback"), /AI writing was not used.*rule-based writer/);
});

test("BAP-8: the follow-up email step shows no workflow checkpoints, and keeps every email control", () => {
  // Hidden on the follow-up email and the meeting input, with or without a trailing slash.
  for (const path of ["/opportunities/abc/follow-up", "/opportunities/abc/follow-up/", "/opportunities/abc/meeting", "/opportunities/abc/meeting/"]) {
    assert.equal(showsWorkflowCheckpoints(path), false, path);
  }
  // Every other post-meeting page keeps the checklist as before.
  for (const path of ["/opportunities/abc/post-meeting-presentation", "/opportunities/abc/review", "/opportunities/follow-up-co/post-meeting-presentation"]) {
    assert.equal(showsWorkflowCheckpoints(path), true, path);
  }

  const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
  const shell = read("../components/PostMeetingShell.tsx");
  assert.match(shell, /\{live && accessGranted && showsWorkflowCheckpoints\(pathname\) \? <details className=\{styles\.workflow\}>/, "the checklist is gated by the route helper only");
  assert.doesNotMatch(shell, /pathname\.endsWith\("\/meeting"\)/);
  // Workflow status loading and the access gate are unchanged.
  assert.match(shell, /loadPostMeetingWorkflow\(accessToken, opportunityId, controller\.signal\)/);
  assert.match(shell, /setAccessGranted\(workflowCompleted\(value, "first_meeting_completed"\)\)/);
  assert.match(shell, /const canView = previewMode \? preview\.enabled && preview\.completed : live && accessGranted/);

  // The email step keeps generation, editing, attachments, review/approval and export.
  const panel = read("../components/FollowUpDraftPanel.tsx");
  for (const call of ["generatePostMeetingEmail(token, opportunityId, overwrite, gate.language)", "savePostMeetingEmail(token, opportunityId, draft,",
    "attachments: { [attachment.key]: !attachment.selected }", "confirmPostMeetingEmail(token, opportunityId, draft,",
    "exportPostMeetingEmail(token, opportunityId, draft)", "downloadEmailAttachment(token, attachment)",
    '<PostMeetingPhaseNav opportunityId={opportunityId} active="review" />']) {
    assert.ok(panel.includes(call), call);
  }
  assert.doesNotMatch(panel, /Workflow checkpoints|WORKFLOW_STATUS_CATALOG/);
});
