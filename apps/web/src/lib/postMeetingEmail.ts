import { ApiRequestError, apiFetch, apiFetchBlob, resolveBackendOpportunityId } from "./api";
import { postMeetingPath, workflowCompleted, type PostMeetingWorkflow } from "./postMeeting";

/** The post-meeting follow-up email: three saved lengths, the owner's review and an unsent export. */

export const EMAIL_LENGTHS = ["short", "medium", "extensive"] as const;
export type EmailLength = (typeof EMAIL_LENGTHS)[number];
export const EMAIL_LENGTH_LABEL: Record<EmailLength, string> = { short: "Short", medium: "Medium", extensive: "Extensive" };

export interface EmailText { subject: string; body: string; wordCount: number; edited: boolean }
export interface EmailPerson { name: string | null; email: string }
export interface EmailRecipients { to: EmailPerson[]; cc: EmailPerson[]; sender: { name: string; role: string | null; email: string } }
/**
 * BAP-7: the four generated documents of the finalized package, each pinned to the version the
 * finalized V2 was built from. ``key`` is the stable selection key; "pptx" and "pdf" are the V2 files.
 */
export const ATTACHMENT_KEYS = ["pptx", "pdf", "v1_pptx", "v1_pdf", "discovery1_docx", "discovery2_docx"] as const;
export type GeneratedAttachmentKey = (typeof ATTACHMENT_KEYS)[number];
/** BAP-3: a file the owner uploaded is selected by its stable id. */
export type ExternalAttachmentKey = `external:${string}`;
export type AttachmentKey = GeneratedAttachmentKey | ExternalAttachmentKey;
export type AttachmentArtifact = "master_presentation_v2" | "master_presentation_v1" | "discovery_1" | "discovery_2" | "external";
export const ATTACHMENT_ARTIFACT_LABEL: Record<AttachmentArtifact, string> = {
  discovery_1: "Discovery 1", discovery_2: "Discovery 2", master_presentation_v1: "Master Presentation 1", master_presentation_v2: "Master Presentation 2",
  external: "Uploaded by you",
};
export type AttachmentFormat = "pptx" | "pdf" | "docx" | "xlsx" | "png" | "jpg" | "jpeg" | "txt" | "csv";
export interface EmailAttachment {
  key: AttachmentKey; artifact: AttachmentArtifact; origin: "generated" | "external";
  format: AttachmentFormat; fileName: string; selected: boolean; available: boolean; sizeBytes: number | null;
  presentationId: string | null; presentationVersionId: string | null; artifactVersionId: string | null;
  versionNumber: number | null; downloadPath: string | null; uploadedAt: string | null;
}

/** Same limits as the API (services/followup/attachments.py). */
export const EXTERNAL_ATTACHMENT_EXTENSIONS = [".pdf", ".docx", ".xlsx", ".pptx", ".png", ".jpg", ".jpeg", ".txt", ".csv"] as const;
export const MAX_EXTERNAL_ATTACHMENT_BYTES = 10 * 1024 * 1024;
export const MAX_EXTERNAL_ATTACHMENTS = 10;
/**
 * The 25 MB policy applies to the exported email: attachments count with their Base64-encoded
 * size, about 4/3 of the file (same estimate as the API's ``encoded_size``).
 */
export const MAX_EMAIL_BYTES = 25 * 1024 * 1024;
export const MAX_TOTAL_ATTACHMENT_BYTES = MAX_EMAIL_BYTES - 128 * 1024;
export const encodedAttachmentSize = (raw: number) => {
  const base64 = 4 * Math.floor((raw + 2) / 3);
  return base64 + 2 * Math.floor((base64 + 75) / 76) + 1024;
};
const ATTACHMENT_FORMATS: readonly string[] = ["pptx", "pdf", "docx", "xlsx", "png", "jpg", "jpeg", "txt", "csv"];
const EXTERNAL_KEY = /^external:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** A first check in the browser; the API checks the content again and decides. */
export function checkAttachmentFile(file: { name: string; size: number }, draft: Pick<PostMeetingEmail, "attachments"> | null): string | null {
  const extension = file.name.includes(".") ? `.${file.name.split(".").pop()!.toLowerCase()}` : "";
  if (!(EXTERNAL_ATTACHMENT_EXTENSIONS as readonly string[]).includes(extension)) {
    return `${file.name}: this file type cannot be attached. Supported: PDF, DOCX, XLSX, PPTX, PNG, JPG, TXT, CSV.`;
  }
  if (!file.size) return `${file.name} is empty.`;
  if (file.size > MAX_EXTERNAL_ATTACHMENT_BYTES) return `${file.name} is larger than 10 MB.`;
  const uploaded = draft?.attachments.filter((item) => item.origin === "external").length ?? 0;
  if (uploaded >= MAX_EXTERNAL_ATTACHMENTS) return `An email can carry at most ${MAX_EXTERNAL_ATTACHMENTS} uploaded files. Remove one first.`;
  if (draft && selectedAttachmentBytes(draft) + encodedAttachmentSize(file.size) > MAX_TOTAL_ATTACHMENT_BYTES) {
    return `With ${file.name} the email would be larger than 25 MB once encoded. Deselect a file first.`;
  }
  return null;
}

/** What the selected attachments add to the email, encoded. */
export const selectedAttachmentBytes = (draft: Pick<PostMeetingEmail, "attachments">) =>
  draft.attachments.filter((item) => item.selected).reduce((total, item) => total + encodedAttachmentSize(item.sizeBytes ?? 0), 0);
export interface EmailSource {
  presentationId: string; presentationVersionId: string; versionNumber: number; finalizedAt: string;
  extractionMode: string | null; confirmedFindings: number | null; excludedFindings: number | null;
}
export interface PostMeetingEmail {
  id: string; revision: number; status: "draft" | "confirmed"; selectedLength: EmailLength | null;
  lengths: Record<EmailLength, EmailText>; wordLimits: Record<EmailLength, number>;
  confirmedAt: string | null; confirmedRevision: number | null; updatedAt: string;
  source: EmailSource | null; sourceStatus: "valid" | "changed" | "not_applicable";
  reviewFlags: string[]; recipients: EmailRecipients | null; attachments: EmailAttachment[];
  writer: EmailWriter | null;
}
/**
 * Who wrote the generated text. "ai": the model, accepted by the grounding checks. "fallback": AI
 * writing was attempted and not used. "deterministic": AI writing is not enabled here.
 */
export interface EmailWriter {
  mode: "ai" | "fallback" | "deterministic"; model: string | null; verified: boolean; fallbackReason: string | null; checkedSentences: number | null;
}

/** The six checks the owner makes before a draft is confirmed. The API requires all of them. */
export const EMAIL_REVIEW_CHECKS = [
  ["recipients", "Names and recipients are correct."],
  ["dates_and_owners", "Dates and action owners are correct."],
  ["supported_statements", "Every statement is supported by the approved meeting sources."],
  ["review_flags", "All review flags are acknowledged or resolved."],
  ["tone", "The tone matches the client."],
  ["attachments", "The selected attachments are the approved files."],
] as const;

const FLAG_TEXT: Record<string, string> = {
  speaker_unverified: "The package records that these points were made in the meeting, not who made them. Check that nothing a Borek colleague proposed reads as a client statement or as agreed.",
  fixture_extraction: "The meeting findings came from the built-in demo extraction, not from a live AI analysis. Check every statement against the transcript.",
  meeting_date_unconfirmed: "No approved source states the meeting date, so the email names none. Add it if you want it mentioned.",
  owner_notes_omitted: "Findings from your personal notes, and points you wrote or reworded in Discovery 2, are left out: they were not said in the meeting in this form.",
  action_owner_unconfirmed: "At least one next step does not name who is responsible. Add the owner or remove the step.",
  action_date_unconfirmed: "At least one next step has no deadline and is marked “date to be confirmed”.",
  no_next_steps_confirmed: "No next step was confirmed for this package, so the email lists none.",
  content_trimmed: "Some confirmed points did not fit the longest draft and were left out.",
  ai_written: "This text was written by the AI writer from your confirmed findings. Every sentence passed the automatic grounding check; read it against the findings yourself before you approve.",
  ai_fallback: "AI writing was not used for this draft, so the text comes from the rule-based writer. The reason is shown under the message.",
};
export const emailFlagText = (flag: string) => FLAG_TEXT[flag] ?? `Check before sending: ${flag.replaceAll("_", " ")}.`;

const ERROR_TEXT: Record<string, string> = {
  FOLLOWUP_STATICS_REQUIRED: "Enter the project name, recipient and sender first.",
  FOLLOWUP_STATICS_INCOMPLETE: "The recipient needs a first name (informal) or a salutation and last name (formal), and the sender a name and address.",
  FOLLOWUP_FINALIZATION_REQUIRED: "Review and finalize Master Presentation V2 before preparing the follow-up email.",
  FOLLOWUP_SOURCE_UNAVAILABLE: "The finalized presentation package could not be read. The email is not generated from other sources.",
  FOLLOWUP_SOURCE_CHANGED: "The finalized presentation package changed. Regenerate the email from the current package.",
  FOLLOWUP_CONTENT_TOO_LONG: "A confirmed finding is too long for one of the three drafts and is never shortened automatically, so no draft was generated.",
  FOLLOWUP_NO_CONFIRMED_CONTENT: "The finalized package contains no confirmed statement from the meeting, so there is nothing to summarise.",
  EMAIL_DRAFT_CONFLICT: "This draft was changed elsewhere after you loaded it.",
  EMAIL_DRAFT_HAS_EDITS: "This draft contains saved edits or a confirmation.",
  EMAIL_REVIEW_INCOMPLETE: "Complete every point of the review checklist first.",
  EMAIL_REVIEW_FLAGS_OPEN: "Acknowledge every review flag first.",
  EMAIL_ATTACHMENT_UNAVAILABLE: "A selected attachment is not available. Deselect it or restore the approved file.",
  EMAIL_ATTACHMENT_CONTRADICTION: "The message says that something is attached, but no file is selected. Select the approved file or change that sentence, then save and review again.",
  EMAIL_NOT_CONFIRMED: "Review and confirm the saved draft before exporting it.",
  EMAIL_EXPORT_STALE: "The draft changed after it was confirmed. Review and confirm it again.",
  EMAIL_PLACEHOLDER_UNRESOLVED: "The email still contains a template placeholder such as {{name}}.",
  EMAIL_SUBJECT_INVALID: "The subject must be a single line of 1 to 200 characters.",
  EMAIL_BODY_INVALID: "The message is empty or too long.",
};

const UPLOAD_ERRORS = new Set([
  "EMAIL_ATTACHMENT_INVALID", "EMAIL_ATTACHMENT_UNSUPPORTED_TYPE", "EMAIL_ATTACHMENT_EMPTY", "EMAIL_ATTACHMENT_TOO_LARGE",
  "EMAIL_ATTACHMENT_CORRUPT", "EMAIL_ATTACHMENT_LIMIT", "EMAIL_ATTACHMENTS_TOO_LARGE", "EMAIL_ATTACHMENT_STORAGE_FAILED",
]);

export function emailError(error: unknown): string {
  if (error instanceof ApiRequestError) {
    if (error.code === "EMAIL_LENGTH_EXCEEDED") return error.message;
    // BAP-3: the API names the file and what is wrong with it.
    if (error.code && UPLOAD_ERRORS.has(error.code) && error.message) return error.message;
    if (error.code && ERROR_TEXT[error.code]) return ERROR_TEXT[error.code];
    if (error.status === 404) return "This email draft was not found.";
  }
  return error instanceof Error && error.message ? error.message : "The email request failed. Try again.";
}

export const isEmailConflict = (error: unknown) => error instanceof ApiRequestError && error.status === 409 && error.code === "EMAIL_DRAFT_CONFLICT";
export const isEmailHasEdits = (error: unknown) => error instanceof ApiRequestError && error.status === 409 && error.code === "EMAIL_DRAFT_HAS_EDITS";
export const isStaticsMissing = (error: unknown) =>
  error instanceof ApiRequestError && (error.code === "FOLLOWUP_STATICS_REQUIRED" || error.code === "FOLLOWUP_STATICS_INCOMPLETE");

const isString = (value: unknown): value is string => typeof value === "string";
const isLength = (value: unknown): value is EmailLength => EMAIL_LENGTHS.includes(value as EmailLength);

function parseText(value: unknown): EmailText {
  const text = value as { subject?: unknown; body?: unknown; word_count?: unknown; edited?: unknown } | undefined;
  if (!isString(text?.subject) || !isString(text.body) || typeof text.word_count !== "number") {
    throw new Error("The email draft is incomplete. Retry loading the draft.");
  }
  return { subject: text.subject, body: text.body, wordCount: text.word_count, edited: text.edited === true };
}

function parsePerson(value: unknown): EmailPerson {
  const person = value as { name?: unknown; email?: unknown };
  if (!isString(person?.email)) throw new Error("The email draft has an invalid recipient.");
  return { name: isString(person.name) && person.name ? person.name : null, email: person.email };
}

function parseWriter(value: unknown): EmailWriter | null {
  const writer = value as Record<string, unknown> | null | undefined;
  if (!writer || (writer.mode !== "ai" && writer.mode !== "fallback" && writer.mode !== "deterministic")) return null;
  const reason = writer.fallback_reason as Record<string, unknown> | null | undefined;
  return {
    mode: writer.mode, model: isString(writer.model) ? writer.model : null,
    // Only a draft the API reports as AI-written and verified is ever labelled as such.
    verified: writer.mode === "ai" && writer.verified === true,
    fallbackReason: writer.mode === "fallback" ? (reason && isString(reason.message) && reason.message ? reason.message : "The AI draft could not be used.") : null,
    checkedSentences: typeof writer.checked_sentences === "number" ? writer.checked_sentences : null,
  };
}

/** One line that says who wrote the generated text. A fallback is never worded as AI-written. */
export function writerLabel(writer: EmailWriter | null): string | null {
  if (!writer) return null;
  if (writer.mode === "ai" && writer.verified) {
    return `Written by the AI writer${writer.model ? ` (${writer.model})` : ""} from your confirmed findings${writer.checkedSentences ? ` · ${writer.checkedSentences} sentences passed the grounding check` : ""}.`;
  }
  if (writer.mode === "fallback") return `Rule-based writer (fallback) — the AI draft was not used: ${writer.fallbackReason}`;
  return "Rule-based writer — AI writing is not enabled in this environment.";
}

export function parsePostMeetingEmail(value: unknown, opportunityId: string): PostMeetingEmail | null {
  if (!value || typeof value !== "object") throw new Error("Invalid email draft response.");
  const envelope = value as Record<string, unknown>;
  if (envelope.opportunity_id !== resolveBackendOpportunityId(opportunityId) || envelope.journey_stage !== "deepening") {
    throw new Error("The email draft does not belong to this post-meeting opportunity.");
  }
  if (envelope.draft === null) return null;
  const draft = envelope.draft as Record<string, unknown> | undefined;
  const lengths = draft?.lengths as Record<string, unknown> | undefined;
  if (!draft || !isString(draft.id) || !isString(draft.updated_at) || !lengths || typeof draft.revision !== "number") {
    throw new Error("The email draft is incomplete. Retry loading the draft.");
  }
  if (draft.send_status !== "not_sent") throw new Error("The email draft reports an unexpected delivery state.");
  const source = draft.source as Record<string, unknown> | null | undefined;
  const recipients = draft.recipients as { to?: unknown[]; cc?: unknown[]; sender?: Record<string, unknown> } | null | undefined;
  const limits = (draft.word_limits ?? {}) as Record<string, unknown>;
  const count = (key: string) => (typeof source?.[key] === "number" ? (source[key] as number) : null);
  return {
    id: draft.id,
    revision: draft.revision,
    status: draft.status === "confirmed" ? "confirmed" : "draft",
    selectedLength: isLength(draft.selected_length) ? draft.selected_length : null,
    lengths: { short: parseText(lengths.short), medium: parseText(lengths.medium), extensive: parseText(lengths.extensive) },
    wordLimits: {
      short: typeof limits.short === "number" ? limits.short : 150,
      medium: typeof limits.medium === "number" ? limits.medium : 300,
      extensive: typeof limits.extensive === "number" ? limits.extensive : 500,
    },
    confirmedAt: isString(draft.confirmed_at) ? draft.confirmed_at : null,
    confirmedRevision: typeof draft.confirmed_revision === "number" ? draft.confirmed_revision : null,
    updatedAt: draft.updated_at,
    source: source && isString(source.presentation_id) && isString(source.presentation_version_id)
      ? {
          presentationId: source.presentation_id, presentationVersionId: source.presentation_version_id,
          versionNumber: typeof source.version_number === "number" ? source.version_number : 0,
          finalizedAt: isString(source.finalized_at) ? source.finalized_at : "",
          extractionMode: isString(source.extraction_execution_mode) ? source.extraction_execution_mode : null,
          confirmedFindings: count("confirmed_finding_count"), excludedFindings: count("excluded_finding_count"),
        }
      : null,
    sourceStatus: draft.source_status === "valid" || draft.source_status === "changed" ? draft.source_status : "not_applicable",
    reviewFlags: Array.isArray(draft.review_flags) ? draft.review_flags.filter(isString) : [],
    writer: parseWriter(draft.writer),
    recipients: recipients && Array.isArray(recipients.to) && recipients.sender && isString(recipients.sender.email)
      ? {
          to: recipients.to.map(parsePerson), cc: (recipients.cc ?? []).map(parsePerson),
          sender: { name: String(recipients.sender.name ?? ""), role: isString(recipients.sender.role) ? recipients.sender.role : null, email: recipients.sender.email },
        }
      : null,
    attachments: (Array.isArray(draft.attachments) ? draft.attachments : []).flatMap((item): EmailAttachment[] => {
      const file = item as Record<string, unknown>;
      if (!ATTACHMENT_FORMATS.includes(String(file.format)) || !isString(file.file_name)) return [];
      // An envelope from before BAP-7 names the V2 files by format only.
      const key = (ATTACHMENT_KEYS as readonly string[]).includes(String(file.key)) || EXTERNAL_KEY.test(String(file.key)) ? file.key as AttachmentKey
        : file.format === "pptx" || file.format === "pdf" ? file.format : null;
      if (!key) return [];
      const external = key.startsWith("external:");
      const artifact = external ? "external"
        : (["master_presentation_v2", "master_presentation_v1", "discovery_1", "discovery_2"] as const).find((name) => name === file.artifact) ?? "master_presentation_v2";
      return [{
        key, artifact, origin: external ? "external" : "generated", uploadedAt: isString(file.uploaded_at) ? file.uploaded_at : null,
        format: file.format as AttachmentFormat, fileName: file.file_name, selected: file.selected === true, available: file.available === true,
        sizeBytes: typeof file.size_bytes === "number" ? file.size_bytes : null,
        presentationId: isString(file.presentation_id) ? file.presentation_id : null,
        presentationVersionId: isString(file.presentation_version_id) ? file.presentation_version_id : null,
        artifactVersionId: isString(file.artifact_version_id) ? file.artifact_version_id : isString(file.presentation_version_id) ? file.presentation_version_id : null,
        versionNumber: typeof file.version_number === "number" ? file.version_number : null,
        downloadPath: isString(file.download_path) && file.download_path.startsWith("/") ? file.download_path : null,
      }];
    }),
  };
}

const draftsPath = (opportunityId: string, suffix = "") => postMeetingPath(opportunityId, `email-drafts${suffix}`);
const opportunityPath = (opportunityId: string) => `/opportunities/${encodeURIComponent(resolveBackendOpportunityId(opportunityId))}`;

export async function loadPostMeetingEmail(token: string, opportunityId: string, signal?: AbortSignal) {
  return parsePostMeetingEmail(await apiFetch(draftsPath(opportunityId, "?journey_stage=deepening"), token, { signal, cache: "no-store" }), opportunityId);
}

/** Generates all three lengths. ``overwriteEdits`` is the owner's explicit consent to replace saved work. */
export async function generatePostMeetingEmail(token: string, opportunityId: string, overwriteEdits = false) {
  const value = await apiFetch(draftsPath(opportunityId, "/generate"), token, {
    method: "POST", body: JSON.stringify({ journey_stage: "deepening", overwrite_edits: overwriteEdits }),
  });
  const draft = parsePostMeetingEmail(value, opportunityId);
  if (!draft) throw new Error("No email draft was returned. Retry preparation.");
  return draft;
}

export interface EmailEdit {
  selectedLength?: EmailLength;
  lengths?: Partial<Record<EmailLength, { subject: string; body: string }>>;
  attachments?: Partial<Record<AttachmentKey, boolean>>;
}

/** BAP-3: upload one additional attachment on the saved revision; the API selects it. */
export async function uploadEmailAttachment(token: string, opportunityId: string, draft: Pick<PostMeetingEmail, "id" | "revision">, file: File) {
  const body = new FormData();
  body.set("file", file);
  body.set("expected_revision", String(draft.revision));
  const saved = parsePostMeetingEmail(await apiFetch(draftsPath(opportunityId, `/${encodeURIComponent(draft.id)}/attachments`), token, { method: "POST", body }), opportunityId);
  if (!saved) throw new Error("The saved email draft was not returned.");
  return saved;
}

/** BAP-3: remove an uploaded file on the saved revision. */
export async function removeEmailAttachment(token: string, opportunityId: string, draft: Pick<PostMeetingEmail, "id" | "revision">, attachment: EmailAttachment) {
  if (attachment.origin !== "external" || !attachment.artifactVersionId) throw new Error("Only an uploaded file can be removed.");
  const path = draftsPath(opportunityId, `/${encodeURIComponent(draft.id)}/attachments/${encodeURIComponent(attachment.artifactVersionId)}?expected_revision=${draft.revision}`);
  const saved = parsePostMeetingEmail(await apiFetch(path, token, { method: "DELETE" }), opportunityId);
  if (!saved) throw new Error("The saved email draft was not returned.");
  return saved;
}

/** Saves against the revision the edit was made on; the API answers 409 if the draft moved on. */
export async function savePostMeetingEmail(token: string, opportunityId: string, draft: Pick<PostMeetingEmail, "id" | "revision">, edit: EmailEdit) {
  const value = await apiFetch(draftsPath(opportunityId, `/${encodeURIComponent(draft.id)}`), token, {
    method: "PATCH",
    body: JSON.stringify({ expected_revision: draft.revision, selected_length: edit.selectedLength, lengths: edit.lengths, attachments: edit.attachments }),
  });
  const saved = parsePostMeetingEmail(value, opportunityId);
  if (!saved) throw new Error("The saved email draft was not returned.");
  return saved;
}

/**
 * The original detailed review card (one tick per review flag and per checklist item) instead of
 * the Review & confirm dialog. Off unless the web build sets the switch to exactly "true".
 */
export const advancedReviewUi = (value: string | undefined = process.env.NEXT_PUBLIC_FOLLOWUP_ADVANCED_REVIEW_UI) => value?.trim() === "true";

export interface EmailReviewWarning { key: string; text: string; flag: boolean; blocking: boolean }

/**
 * Everything the owner has to see before approving one length of a saved draft: the review flags
 * stored with the draft, and what the current selection adds (a missing file, a changed package,
 * a message that announces an attachment that is not selected). ``blocking`` items cannot be
 * approved; the API refuses them as well.
 */
export function emailReviewWarnings(draft: PostMeetingEmail, length: EmailLength): EmailReviewWarning[] {
  const warnings: EmailReviewWarning[] = draft.reviewFlags.map((flag) => ({ key: flag, text: emailFlagText(flag), flag: true, blocking: false }));
  const selected = draft.attachments.filter((item) => item.selected);
  for (const item of selected.filter((file) => !file.available)) {
    warnings.push({ key: `attachment_unavailable_${item.key}`, text: `${item.fileName} is selected but not available. Deselect it or restore the approved file.`, flag: false, blocking: true });
  }
  if (draft.sourceStatus === "changed") {
    warnings.push({ key: "source_changed", text: "The finalized presentation package changed after this email was written. Regenerate the email from the current package.", flag: false, blocking: true });
  }
  if (draft.attachments.length && !selected.length) {
    // A message that announces a file it does not carry is a contradiction, not a point to tick:
    // the owner resolves it - selects the file or changes the sentence - before approving.
    const contradiction = announcesAttachment(draft.lengths[length].body);
    warnings.push(contradiction
      ? { key: "attachment_contradiction", flag: false, blocking: true, text: "The message says that something is attached, but no file is selected. Select the approved file or change that sentence, then save." }
      : { key: "no_attachment_selected", flag: false, blocking: false, text: "No file is selected: the email will be exported without an attachment." });
  }
  return warnings;
}

/**
 * The text says that something is attached ("attached", "attachment", "enclosed"). "No attachment"
 * and "not attached" say the opposite. The API applies the same rule when a draft is confirmed.
 */
export const announcesAttachment = (body: string) => /\b(?:attach\w*|enclos\w*)\b/i.test(body.replace(/\b(?:no|not|without|nothing|none|never)\b[^.!?\n]{0,30}?\battach\w*/gi, " "));

/** The lengths whose saved text announces an attachment although no file is selected: the owner's own edits. */
export const attachmentMismatch = (draft: PostMeetingEmail | null): EmailLength[] => (
  draft && draft.attachments.length && !draft.attachments.some((item) => item.selected)
    ? EMAIL_LENGTHS.filter((name) => announcesAttachment(draft.lengths[name].body))
    : []
);

/**
 * What the approval in the review dialog records - what the owner did there, and nothing else:
 * the review flags they ticked one by one, and the six review checks only when they confirmed the
 * written list of them. Anything not ticked is not sent, so the API refuses the confirmation.
 */
export const approvalFor = (draft: PostMeetingEmail, acknowledgedFlags: string[], checksConfirmed: boolean) => ({
  checks: checksConfirmed ? EMAIL_REVIEW_CHECKS.map(([key]) => key as string) : [],
  acknowledgedFlags: draft.reviewFlags.filter((flag) => acknowledgedFlags.includes(flag)),
});

/** The review flags of this draft that the owner has not ticked yet. */
export const openReviewFlags = (draft: PostMeetingEmail | null, acknowledged: string[]) => (draft?.reviewFlags ?? []).filter((flag) => !acknowledged.includes(flag));

/**
 * Records the owner's review of one saved revision. Confirming never sends anything.
 * ``acknowledgedFlags`` are the flags the owner ticked, one by one. Exactly these are sent; a
 * flag that was not ticked is not sent, and the API refuses the confirmation.
 */
export async function confirmPostMeetingEmail(
  token: string, opportunityId: string, draft: PostMeetingEmail, length: EmailLength, checks: string[], acknowledgedFlags: string[],
) {
  const value = await apiFetch(draftsPath(opportunityId, `/${encodeURIComponent(draft.id)}/confirm`), token, {
    method: "POST",
    body: JSON.stringify({
      selected_length: length, expected_revision: draft.revision, review_checks: checks,
      acknowledged_flags: draft.reviewFlags.filter((flag) => acknowledgedFlags.includes(flag)),
    }),
  });
  const confirmed = parsePostMeetingEmail(value, opportunityId);
  if (!confirmed) throw new Error("The confirmed email draft was not returned.");
  return confirmed;
}

/** The confirmed revision as an unsent .eml file, with the selected approved files inside. */
export async function exportPostMeetingEmail(token: string, opportunityId: string, draft: PostMeetingEmail, signal?: AbortSignal) {
  if (!emailExportable(draft)) throw new Error("Review and confirm the saved draft before exporting it.");
  const blob = await apiFetchBlob(draftsPath(opportunityId, `/${encodeURIComponent(draft.id)}/export?revision=${draft.revision}`), token, {
    signal, cache: "no-store", redirect: "error",
  });
  if (!blob.size) throw new Error("The exported email is empty.");
  return blob;
}

/** One approved file of the pinned version (presentation or Discovery) - never the latest one. */
export async function downloadEmailAttachment(token: string, attachment: EmailAttachment, signal?: AbortSignal) {
  if (!attachment.available) throw new Error("This approved file is not available.");
  const path = attachment.downloadPath
    ?? (attachment.presentationId && attachment.presentationVersionId
      ? `/presentations/${encodeURIComponent(attachment.presentationId)}/versions/${encodeURIComponent(attachment.presentationVersionId)}/download/${attachment.format}`
      : null);
  if (!path) throw new Error("This approved file is not available.");
  const blob = await apiFetchBlob(path, token, { signal, cache: "no-store", redirect: "error" });
  if (!blob.size) throw new Error("The approved file is empty.");
  return blob;
}

export function canPreparePostMeetingEmail(workflow: PostMeetingWorkflow | null) {
  return Boolean(workflow?.finalization && workflowCompleted(workflow, "owner_review") && workflowCompleted(workflow, "finalized"));
}

/** Words of the message without the greeting line and the signature: the figure the API limits. */
export function contentWordCount(body: string) {
  const withoutGreeting = body.replace(/\r\n?/g, "\n").replace(/^\s*[^\n]+,\s*\n/, "");
  const content = withoutGreeting.split(/\n\s*Best regards\s*\n/i)[0].trim();
  return content ? content.split(/\s+/).length : 0;
}

export type EmailWorkingCopy = Record<EmailLength, { subject: string; body: string }>;
export const workingCopy = (draft: PostMeetingEmail): EmailWorkingCopy => ({
  short: { subject: draft.lengths.short.subject, body: draft.lengths.short.body },
  medium: { subject: draft.lengths.medium.subject, body: draft.lengths.medium.body },
  extensive: { subject: draft.lengths.extensive.subject, body: draft.lengths.extensive.body },
});

/** The lengths whose text differs from what is saved. Switching lengths keeps every one of them. */
export function unsavedLengths(draft: PostMeetingEmail | null, copy: EmailWorkingCopy | null): EmailLength[] {
  if (!draft || !copy) return [];
  return EMAIL_LENGTHS.filter((name) => copy[name].subject !== draft.lengths[name].subject || copy[name].body !== draft.lengths[name].body);
}

export type EmailState = "none" | "unsaved" | "saved" | "confirmed";
export function emailState(draft: PostMeetingEmail | null, copy: EmailWorkingCopy | null): EmailState {
  if (!draft) return "none";
  if (unsavedLengths(draft, copy).length) return "unsaved";
  return emailExportable(draft) ? "confirmed" : "saved";
}
export const EMAIL_STATE_LABEL: Record<EmailState, string> = { none: "No draft", unsaved: "Unsaved changes", saved: "Changes saved", confirmed: "Confirmed · not sent" };

/** Only a confirmed draft whose confirmation covers the current revision may be exported. */
export function emailExportable(draft: PostMeetingEmail | null) {
  return Boolean(draft && draft.status === "confirmed" && draft.selectedLength && draft.confirmedRevision === draft.revision && draft.sourceStatus !== "changed");
}

export function formatFileSize(bytes: number | null) {
  if (bytes === null) return "";
  return bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

export const personLabel = (person: EmailPerson) => (person.name ? `${person.name} <${person.email}>` : person.email);

/** Plain text for the clipboard: the confirmed subject and message, nothing else. */
export function clipboardText(draft: PostMeetingEmail) {
  if (!emailExportable(draft) || !draft.selectedLength) throw new Error("Review and confirm the saved draft before copying it.");
  const text = draft.lengths[draft.selectedLength];
  return `Subject: ${text.subject}\n\n${text.body}`;
}

// ---- recipient, salutation and sender (project statics). The owner enters them; nothing is guessed.

export interface FollowupStaticsForm {
  projectName: string; clientShort: string; style: "informal" | "formal";
  recipientEmail: string; recipientFirstName: string; recipientLastName: string; recipientSalutation: string;
  senderName: string; senderRole: string; senderEmail: string;
}
interface StoredRecipient { email: string; first_name?: string | null; last_name?: string | null; salutation?: string | null; kind: "to" | "cc"; primary?: boolean }
interface StoredStatics {
  project_name: string; client_short: string; salutation_style: "informal" | "formal";
  standard_recipients: StoredRecipient[]; sender_profile: { name: string; role: string; email: string };
}
const EMAIL_ADDRESS = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const isPrimary = (item: StoredRecipient) => item.kind === "to" && item.primary === true;

export function staticsFormFrom(value: unknown, defaults: { companyName: string; contactPerson: string }): FollowupStaticsForm {
  const stored = value && typeof value === "object" ? (value as Partial<StoredStatics>) : null;
  const recipients = stored?.standard_recipients ?? [];
  const primary = recipients.find(isPrimary) ?? recipients[0];
  const [first = "", ...rest] = defaults.contactPerson.trim().split(/\s+/).filter(Boolean);
  return {
    projectName: stored?.project_name ?? "", clientShort: stored?.client_short ?? defaults.companyName,
    style: stored?.salutation_style === "formal" ? "formal" : "informal",
    recipientEmail: primary?.email ?? "", recipientFirstName: primary?.first_name ?? (stored ? "" : first),
    recipientLastName: primary?.last_name ?? (stored ? "" : rest.join(" ")), recipientSalutation: primary?.salutation ?? "",
    senderName: stored?.sender_profile?.name ?? "", senderRole: stored?.sender_profile?.role ?? "", senderEmail: stored?.sender_profile?.email ?? "",
  };
}

export function validateStaticsForm(form: FollowupStaticsForm): string | null {
  if (!form.projectName.trim() || !form.clientShort.trim()) return "Enter the project name and the client name.";
  if (!EMAIL_ADDRESS.test(form.recipientEmail.trim())) return "Enter the recipient's email address.";
  if (form.style === "informal" && !form.recipientFirstName.trim()) return "An informal greeting needs the recipient's first name.";
  if (form.style === "formal" && (!form.recipientSalutation.trim() || !form.recipientLastName.trim())) return "A formal greeting needs a salutation (for example Ms or Mr) and the last name.";
  if (!form.senderName.trim() || !form.senderRole.trim()) return "Enter the sender's name and role.";
  if (!EMAIL_ADDRESS.test(form.senderEmail.trim())) return "Enter the sender's email address.";
  return null;
}

/** Keeps further recipients (for example Cc) that were stored before; only the primary one is edited here. */
export function staticsPayload(form: FollowupStaticsForm, previous: unknown): StoredStatics {
  const stored = previous && typeof previous === "object" ? (previous as Partial<StoredStatics>) : null;
  const recipients = stored?.standard_recipients ?? [];
  const edited = recipients.find(isPrimary) ?? recipients[0];
  const optional = (text: string) => text.trim() || null;
  return {
    project_name: form.projectName.trim(), client_short: form.clientShort.trim(), salutation_style: form.style,
    standard_recipients: [
      { email: form.recipientEmail.trim(), first_name: optional(form.recipientFirstName), last_name: optional(form.recipientLastName), salutation: optional(form.recipientSalutation), kind: "to", primary: true },
      ...recipients.filter((item) => item !== edited),
    ],
    sender_profile: { name: form.senderName.trim(), role: form.senderRole.trim(), email: form.senderEmail.trim() },
  };
}

export async function loadFollowupStatics(token: string, opportunityId: string, signal?: AbortSignal): Promise<unknown> {
  const opportunity = await apiFetch<{ followup_statics?: unknown }>(opportunityPath(opportunityId), token, { signal, cache: "no-store" });
  return opportunity.followup_statics ?? null;
}

export async function saveFollowupStatics(token: string, opportunityId: string, form: FollowupStaticsForm, previous: unknown) {
  const payload = staticsPayload(form, previous);
  const saved = await apiFetch<{ followup_statics?: unknown }>(opportunityPath(opportunityId), token, {
    method: "PATCH", body: JSON.stringify({ followup_statics: payload }),
  });
  return saved.followup_statics ?? payload;
}
