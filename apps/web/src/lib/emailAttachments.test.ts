import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync as readFileFromDisk } from "node:fs";
import { ApiRequestError } from "./api";
import {
  MAX_EXTERNAL_ATTACHMENT_BYTES, MAX_EXTERNAL_ATTACHMENTS, MAX_TOTAL_ATTACHMENT_BYTES, checkAttachmentFile, encodedAttachmentSize, emailError, parsePostMeetingEmail,
  removeEmailAttachment, selectedAttachmentBytes, uploadEmailAttachment,
} from "./postMeetingEmail";
// Sources are compared as text below. A Windows checkout may hold them with CRLF line ends.
const readFileSync = (path: URL | string, encoding: "utf8"): string => readFileFromDisk(path, encoding).replace(/\r\n/g, "\n");

const id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const upload = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const text = { subject: "S", body: "B", word_count: 1, edited: false };
const generated = (key: string, artifact: string, format: string, selected = false) => ({
  key, artifact, format, origin: "generated", uploaded_at: null, file_name: `${key}.${format}`, selected, available: true, size_bytes: 1_000_000,
  presentation_id: null, presentation_version_id: null, artifact_version_id: id, version_number: 1, product_version: "V2", download_path: `/x/${key}`,
});
function envelope(attachments: unknown[]) {
  return {
    schema_version: "1.0", opportunity_id: id, journey_stage: "deepening",
    draft: {
      id: "draft-1", status: "draft", send_status: "not_sent", selected_length: "medium", revision: 4, confirmed_revision: null, confirmed_at: null, review: null,
      created_at: "2026-10-19T08:00:00Z", updated_at: "2026-10-19T09:00:00Z",
      lengths: { short: text, medium: text, extensive: text }, word_limits: { short: 150, medium: 300, extensive: 500 },
      source: null, source_status: "valid", review_flags: [], recipients: null, writer: null, attachments,
    },
  };
}
const external = {
  key: `external:${upload}`, artifact: "external", format: "xlsx", origin: "external", uploaded_at: "2026-10-19T09:30:00Z",
  file_name: "volumes.xlsx", selected: true, available: true, size_bytes: 2_000_000, presentation_id: null, presentation_version_id: null,
  artifact_version_id: upload, version_number: null, product_version: "Uploaded", download_path: `/opportunities/${id}/email-drafts/draft-1/attachments/${upload}/download`,
};
const parsed = () => parsePostMeetingEmail(envelope([
  generated("pptx", "master_presentation_v2", "pptx"), generated("pdf", "master_presentation_v2", "pdf", true),
  generated("discovery2_docx", "discovery_2", "docx"), external,
  { ...external, key: "external:../../x" },
]), id)!;

test("generated documents and uploaded files are parsed apart, with stable keys", () => {
  const draft = parsed();
  assert.deepEqual(draft.attachments.map((item) => [item.key, item.origin, item.artifact]), [
    ["pptx", "generated", "master_presentation_v2"], ["pdf", "generated", "master_presentation_v2"], ["discovery2_docx", "generated", "discovery_2"],
    [`external:${upload}`, "external", "external"],
  ], "a malformed key is not shown");
  const file = draft.attachments[3];
  assert.equal(file.format, "xlsx");
  assert.equal(file.uploadedAt, "2026-10-19T09:30:00Z");
  assert.equal(file.downloadPath, `/opportunities/${id}/email-drafts/draft-1/attachments/${upload}/download`);
  // Counted as they will be in the email: Base64, about 4/3 of the file.
  assert.equal(selectedAttachmentBytes(draft), encodedAttachmentSize(1_000_000) + encodedAttachmentSize(2_000_000));
  assert.ok(selectedAttachmentBytes(draft) > 3_000_000 * 4 / 3);
});

test("files are checked in the browser before they are sent", () => {
  const draft = parsed();
  assert.equal(checkAttachmentFile({ name: "Agenda.PDF", size: 10 }, draft), null);
  assert.match(checkAttachmentFile({ name: "setup.exe", size: 10 }, draft)!, /cannot be attached/);
  assert.match(checkAttachmentFile({ name: "empty.pdf", size: 0 }, draft)!, /empty/);
  assert.equal(checkAttachmentFile({ name: "exact.pdf", size: MAX_EXTERNAL_ATTACHMENT_BYTES }, draft), null);
  assert.match(checkAttachmentFile({ name: "big.pdf", size: MAX_EXTERNAL_ATTACHMENT_BYTES + 1 }, draft)!, /larger than 10 MB/);
  const full = { attachments: Array.from({ length: MAX_EXTERNAL_ATTACHMENTS }, () => ({ ...draft.attachments[3], selected: false })) };
  assert.match(checkAttachmentFile({ name: "one more.pdf", size: 10 }, full)!, /at most 10 uploaded files/);
  const heavy = { attachments: [{ ...draft.attachments[3], selected: true, sizeBytes: MAX_TOTAL_ATTACHMENT_BYTES - 5 }] };
  assert.match(checkAttachmentFile({ name: "more.pdf", size: 10 }, heavy)!, /larger than 25 MB once encoded/);
  // 20 MB of files are fine as raw bytes, but not once encoded.
  const twenty = { attachments: [{ ...draft.attachments[3], selected: true, sizeBytes: 10 * 1024 * 1024 }, { ...draft.attachments[3], selected: true, sizeBytes: 9 * 1024 * 1024 }] };
  assert.match(checkAttachmentFile({ name: "one more.pdf", size: 1024 * 1024 }, twenty)!, /once encoded/);
});

test("upload and removal name the revision they were made on", async () => {
  const original = globalThis.fetch;
  const calls: { url: string; method: string; body: unknown }[] = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ url: String(input), method: init?.method ?? "GET", body: init?.body });
    return Response.json(envelope([external]));
  };
  try {
    const file = new File(["month;quotes"], "volumes.csv", { type: "text/csv" });
    await uploadEmailAttachment("token", id, { id: "draft-1", revision: 4 }, file);
    const draft = parsed();
    await removeEmailAttachment("token", id, { id: "draft-1", revision: 5 }, draft.attachments[3]);
    await assert.rejects(removeEmailAttachment("token", id, { id: "draft-1", revision: 5 }, draft.attachments[0]), /Only an uploaded file/);
  } finally { globalThis.fetch = original; }
  assert.equal(calls[0].method, "POST");
  assert.match(calls[0].url, new RegExp(`/opportunities/${id}/email-drafts/draft-1/attachments$`));
  const form = calls[0].body as FormData;
  assert.equal(form.get("expected_revision"), "4");
  assert.equal((form.get("file") as File).name, "volumes.csv");
  assert.equal(calls[1].method, "DELETE");
  assert.match(calls[1].url, new RegExp(`/email-drafts/draft-1/attachments/${upload}\\?expected_revision=5$`));
  assert.equal(calls.length, 2, "a generated document is never removed");
});

test("the API's file-specific upload errors are shown as they are", () => {
  const refused = new ApiRequestError("fake.pdf is damaged or is not the kind of file its name says.", 400, "EMAIL_ATTACHMENT_CORRUPT");
  assert.equal(refused.code, "EMAIL_ATTACHMENT_CORRUPT");
  assert.equal(emailError(refused), "fake.pdf is damaged or is not the kind of file its name says.");
  // Other codes keep their own wording.
  assert.match(emailError(new ApiRequestError("raw", 409, "FOLLOWUP_SOURCE_CHANGED")), /finalized presentation package changed/);
});

test("the attachment section separates generated documents from uploaded files", () => {
  const panel = readFileSync(new URL("../components/FollowUpDraftPanel.tsx", import.meta.url), "utf8");
  const dialog = readFileSync(new URL("../components/FollowUpReviewDialog.tsx", import.meta.url), "utf8");
  const generatedAt = panel.indexOf(">Generated documents<");
  const uploadedAt = panel.indexOf(">Additional attachments<");
  assert.ok(generatedAt > 0 && uploadedAt > generatedAt, "two labelled groups, generated documents first");
  assert.ok(panel.includes('<input ref={fileInput} type="file" multiple hidden accept={EXTERNAL_ATTACHMENT_EXTENSIONS.join(",")}'));
  assert.ok(panel.includes('data-testid={`email-remove-${item.key}`}') && panel.includes("aria-label={`Remove ${item.fileName}`}"));
  assert.ok(panel.includes("current = await uploadEmailAttachment(token, opportunityId, current, file);"), "each upload uses the revision the previous one produced");
  assert.ok(panel.includes("adopt(current, dirty);") && panel.includes("adopt(saved, dirty);"), "typed, unsaved text stays in the editor");
  assert.ok(panel.includes('data-testid="email-attachment-total"'));
  assert.ok(panel.includes("they are not used to write the text"), "uploads are attachments, not sources");
  assert.ok(dialog.includes("ATTACHMENT_ARTIFACT_LABEL[item.artifact]"), "the review lists every selected file with its origin");
});
