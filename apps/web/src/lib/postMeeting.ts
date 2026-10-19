import { ApiRequestError, apiFetch, generateMeetingExtraction, resolveBackendOpportunityId, savePersonalNotes, type WorkflowDeckLine } from "./api";

export const EXTRACTION_FIELDS = [
  ["requirements", "Requirements"], ["challenges", "Challenges"], ["priorities", "Priorities"],
  ["opportunities", "Opportunities"], ["discussed_solutions", "Discussed solutions"],
  ["decisions", "Decisions"], ["follow_ups", "Follow-ups"],
] as const;

export interface PostMeetingWorkflow {
  opportunity_id: string;
  current_status: string;
  steps: { key: string; state: "completed" | "current" | "pending" }[];
  documents: {
    approved_discovery: { version_id: string; version_number: number } | null;
    ppt1: WorkflowDeckLine | null;
    ppt2: WorkflowDeckLine | null;
  };
  finalization: Record<string, unknown> | null;
}

export interface MeetingTranscript {
  id: string; file_name: string; processing_status: string; created_at: string;
}
export interface PersonalNotes { text: string | null; updated_at: string | null }
export type MeetingExtraction = {
  transcript_id: string; generated_at: string; personal_notes_updated_at: string | null;
} & Record<(typeof EXTRACTION_FIELDS)[number][0], string[]>;
export interface MeetingInputs {
  transcripts: MeetingTranscript[]; notes: PersonalNotes; extraction: MeetingExtraction | null;
}

export function postMeetingPath(opportunityId: string, suffix: string) {
  return `/opportunities/${encodeURIComponent(resolveBackendOpportunityId(opportunityId))}/${suffix}`;
}

/** The checkpoint list is hidden on the meeting input and the follow-up email step (BAP-8); other post-meeting pages keep it. */
export function showsWorkflowCheckpoints(pathname: string) {
  return !/\/(meeting|follow-up)\/?$/.test(pathname);
}

export async function loadPostMeetingWorkflow(token: string, opportunityId: string, signal?: AbortSignal) {
  const value = await apiFetch<PostMeetingWorkflow>(postMeetingPath(opportunityId, "workflow-status"), token, { signal, cache: "no-store" });
  if (value.opportunity_id !== resolveBackendOpportunityId(opportunityId) || !Array.isArray(value.steps) || !value.documents) {
    throw new Error("The workflow response is incomplete or belongs to another opportunity. Reload to retry.");
  }
  return value;
}

export function parseMeetingExtraction(value: unknown): MeetingExtraction | null {
  if (!value || typeof value !== "object") throw new Error("Invalid meeting extraction response.");
  const record = value as Record<string, unknown>;
  if (record.status === "not_generated" && record.extraction === null) return null;
  if (typeof record.transcript_id !== "string" || typeof record.generated_at !== "string" ||
    !(record.personal_notes_updated_at === null || typeof record.personal_notes_updated_at === "string") ||
    EXTRACTION_FIELDS.some(([key]) => !Array.isArray(record[key]) || !(record[key] as unknown[]).every((item) => typeof item === "string"))) {
    throw new Error("The meeting extraction is incomplete. Retry extraction before generating PPT #2.");
  }
  return value as MeetingExtraction;
}

export async function loadMeetingInputs(token: string, opportunityId: string, signal?: AbortSignal): Promise<MeetingInputs> {
  const read = <T>(suffix: string) => apiFetch<T>(postMeetingPath(opportunityId, suffix), token, { signal, cache: "no-store" });
  const [transcripts, notes, extraction] = await Promise.all([
    read<MeetingTranscript[]>("transcripts"), read<PersonalNotes>("personal-notes"),
    read<unknown>("meeting-extraction"),
  ]);
  return { transcripts, notes, extraction: parseMeetingExtraction(extraction) };
}

export function extractionIsCurrent(extraction: MeetingExtraction | null, transcriptId: string, notes: PersonalNotes) {
  return Boolean(extraction && extraction.transcript_id === transcriptId &&
    extraction.personal_notes_updated_at === (notes.text?.trim() ? notes.updated_at : null));
}

export class MeetingNotesConflict extends Error {
  constructor(readonly savedNotes: PersonalNotes) {
    super("Additional notes changed in another session. Your text is retained; choose which notes to use before continuing.");
  }
}

export async function prepareMeetingEvidence(
  token: string, opportunityId: string, transcriptId: string, notes: string, expectedNotes: PersonalNotes,
  options: { signal?: AbortSignal; onProgress?: (message: string) => void; onInputs?: (inputs: MeetingInputs) => void } = {},
) {
  const { signal, onProgress, onInputs } = options;
  const [inputs, workflow] = await Promise.all([
    loadMeetingInputs(token, opportunityId, signal), loadPostMeetingWorkflow(token, opportunityId, signal),
  ]);
  signal?.throwIfAborted();
  if (!workflowCompleted(workflow, "first_meeting_completed") || workflow.finalization || !workflow.documents.approved_discovery) {
    throw new Error("This pitch is not eligible for document generation. Check meeting completion and the approved Discovery document.");
  }
  if (!inputs.transcripts.some((item) => item.id === transcriptId)) throw new Error("Upload a transcript before generating documents.");
  if (notes.length > 20000) throw new Error("Additional notes must be 20,000 characters or fewer.");
  if (inputs.notes.updated_at !== expectedNotes.updated_at || inputs.notes.text !== expectedNotes.text) throw new MeetingNotesConflict(inputs.notes);
  if (notes.trim() !== (inputs.notes.text ?? "")) {
    onProgress?.("Saving additional notes");
    await savePersonalNotes(token, opportunityId, notes);
    signal?.throwIfAborted();
    inputs.notes = await apiFetch<PersonalNotes>(postMeetingPath(opportunityId, "personal-notes"), token, { signal, cache: "no-store" });
    signal?.throwIfAborted();
    if ((inputs.notes.text ?? "") !== notes.trim()) throw new MeetingNotesConflict(inputs.notes);
  }
  onInputs?.({ ...inputs });
  if (!extractionIsCurrent(inputs.extraction, transcriptId, inputs.notes)) {
    onProgress?.("Processing transcript");
    inputs.extraction = parseMeetingExtraction(await generateMeetingExtraction(token, opportunityId, transcriptId));
    signal?.throwIfAborted();
    if (!extractionIsCurrent(inputs.extraction, transcriptId, inputs.notes)) {
      throw new Error("The transcript has not finished processing for these notes. Retry generation.");
    }
    onInputs?.({ ...inputs });
  }
  return { inputs, workflow };
}

export function workflowCompleted(workflow: PostMeetingWorkflow | null, key: string) {
  return workflow?.steps.some((step) => step.key === key && step.state === "completed") ?? false;
}

/** Same limit as the API (MAX_TRANSCRIPT_BYTES); a transcript needs no time stamps, names or labels. */
export const MAX_TRANSCRIPT_BYTES = 10 * 1024 * 1024;

export function validateTranscript(file: { name: string; size: number }): string | null {
  if (!/\.(txt|vtt|srt|docx)$/i.test(file.name)) return "Choose a TXT, VTT, SRT, or DOCX transcript.";
  if (!file.size) return "The transcript is empty. Choose a file containing meeting text.";
  if (file.size > MAX_TRANSCRIPT_BYTES) return "The transcript file is larger than 10 MB. Upload the transcript text only, or split a very long meeting into parts.";
  return null;
}

export function postMeetingError(error: unknown) {
  return error instanceof Error ? error.message : "The request failed. Your unsaved input is retained; please retry.";
}

// ---------------------------------------------------------------------------------------------
// Post Meeting review: sources, findings, confirmation and readiness as the API reports them.

export type FindingSource = "transcript" | "personal_notes" | "both" | "unverified";
export type ExtractionCategory = (typeof EXTRACTION_FIELDS)[number][0];
export interface ReviewFinding { text: string; source: FindingSource }
export interface ReviewedFinding extends ReviewFinding { status: "confirmed" | "excluded" }
export interface ReviewTranscript {
  id: string; file_name: string; processing_status: string; created_at: string | null;
  turn_count: number; analysed: boolean;
}
export interface PostMeetingReview {
  opportunity_id: string;
  /** Identity of every source shown in this review; sent back when confirming. */
  review_fingerprint: string;
  /** How the API runs an analysis: a configured model ("live") or the deterministic test extractor. */
  execution_mode: "fixture" | "live";
  first_meeting_completed: boolean;
  finalized: boolean;
  transcripts: ReviewTranscript[];
  personal_notes: { status: "available" | "missing"; text: string | null; updated_at: string | null };
  extraction: {
    status: "missing" | "current" | "stale"; stale_reasons: string[];
    transcript_id: string | null; transcript_file_name: string | null; generated_at: string | null;
    execution_mode: "fixture" | "live" | null;
    categories: Record<ExtractionCategory, ReviewFinding[]>; item_count: number;
  };
  selected_use_cases: {
    status: "available" | "partial" | "empty"; use_case_ids: string[];
    use_cases: { fact_id: string; status: string; statement: string | null }[];
  };
  approved_discovery: { status: "available" | "missing"; version_id: string | null; version_number: number | null };
  master_presentation: { status: "ready" | "missing" | "legacy"; presentation_id: string | null; version_id: string | null; product_version: string | null };
  confirmation: {
    status: "none" | "current" | "stale"; stale_reasons: string[]; confirmed_at: string | null;
    items: Record<ExtractionCategory, ReviewedFinding[]> | null; confirmed_count: number; excluded_count: number;
  };
  /** BAP-7: Discovery 2, generated from the confirmed meeting and approved before V2. */
  discovery2?: {
    status: "not_generated" | "generating" | "failed" | "draft" | "approved" | "stale"; can_generate: boolean;
    latest_version: { version_id: string; version_number: number; status: string } | null;
    approved_version: { version_id: string; version_number: number } | null; stale_reasons: string[];
  };
  readiness: { ready_for_v2: boolean; blockers: string[] };
  v2_sources: { presentation_id: string; source_hash: string; discovery2_version_id?: string } | null;
}

export const FINDING_SOURCE_LABEL: Record<FindingSource, string> = {
  transcript: "Transcript", personal_notes: "Personal notes", both: "Transcript and notes", unverified: "Source not verified",
};

const STALE_REASON_TEXT: Record<string, string> = {
  TRANSCRIPT_REMOVED: "The analysed transcript was removed.",
  TRANSCRIPT_CHANGED: "The transcript changed after the analysis.",
  TRANSCRIPT_REVISION_NOT_RECORDED: "This analysis was made before transcript revisions were recorded.",
  NOTES_CHANGED: "The personal notes changed after the analysis.",
  EXTRACTION_REPLACED: "The meeting was analysed again after the confirmation.",
  APPROVED_DISCOVERY_CHANGED: "A different Discovery version was approved after the confirmation.",
  USE_CASES_CHANGED: "The selected Borek use cases changed after the confirmation.",
};

const BLOCKER_TEXT: Record<string, string> = {
  DISCOVERY_NOT_APPROVED: "Approve the Discovery document.",
  MASTER_PRESENTATION_V1_NOT_READY: "Generate Master Presentation V1.",
  FIRST_MEETING_NOT_COMPLETED: "Mark the first meeting as completed.",
  TRANSCRIPT_MISSING: "Upload the meeting transcript.",
  MEETING_EXTRACTION_MISSING: "Analyse the meeting.",
  MEETING_EXTRACTION_STALE: "Analyse the meeting again: a source changed.",
  MEETING_REVIEW_NOT_CONFIRMED: "Review the findings and confirm them.",
  MEETING_REVIEW_STALE: "Confirm the findings again: a source changed.",
  MEETING_FINDINGS_EMPTY: "The analysis found no findings. Master Presentation V2 needs at least one confirmed finding.",
  MEETING_FINDINGS_NONE_CONFIRMED: "No finding is confirmed. Include at least one finding and confirm again.",
  DISCOVERY2_MISSING: "Generate Discovery 2 from the confirmed meeting information.",
  DISCOVERY2_NOT_APPROVED: "Review and approve Discovery 2.",
  DISCOVERY2_STALE: "Generate Discovery 2 again: the meeting information changed after it was generated.",
};

export const staleReasonText = (code: string) => STALE_REASON_TEXT[code] ?? "A source changed.";
export const blockerText = (code: string) => BLOCKER_TEXT[code] ?? "A required input is missing.";

export function parsePostMeetingReview(value: unknown, opportunityId: string): PostMeetingReview {
  const record = value as Partial<PostMeetingReview> | null;
  const categories = record?.extraction?.categories as Record<string, unknown> | undefined;
  if (!record || typeof record !== "object" || record.opportunity_id !== resolveBackendOpportunityId(opportunityId) ||
    typeof record.review_fingerprint !== "string" || !/^[0-9a-f]{64}$/.test(record.review_fingerprint) ||
    !Array.isArray(record.transcripts) || !record.personal_notes || !record.extraction || !record.confirmation ||
    !record.readiness || !Array.isArray(record.readiness.blockers) || !record.selected_use_cases ||
    !record.approved_discovery || !record.master_presentation || !categories ||
    EXTRACTION_FIELDS.some(([key]) => !Array.isArray(categories[key]) ||
      !(categories[key] as ReviewFinding[]).every((item) => typeof item?.text === "string" && typeof item?.source === "string"))) {
    throw new Error("The meeting review response is incomplete or belongs to another opportunity. Reload to retry.");
  }
  return record as PostMeetingReview;
}

export async function loadPostMeetingReview(token: string, opportunityId: string, signal?: AbortSignal) {
  const value = await apiFetch<unknown>(postMeetingPath(opportunityId, "post-meeting-review"), token, { signal, cache: "no-store" });
  return parsePostMeetingReview(value, opportunityId);
}

/** Saves the notes unless another session changed them since `expected` was loaded. */
export async function savePersonalNotesChecked(
  token: string, opportunityId: string, text: string, expected: PersonalNotes, signal?: AbortSignal,
): Promise<PersonalNotes> {
  if (text.length > 20000) throw new Error("Personal notes must be 20,000 characters or fewer.");
  const path = postMeetingPath(opportunityId, "personal-notes");
  const current = await apiFetch<PersonalNotes>(path, token, { signal, cache: "no-store" });
  signal?.throwIfAborted();
  if (current.updated_at !== expected.updated_at || (current.text ?? null) !== (expected.text ?? null)) throw new MeetingNotesConflict(current);
  await savePersonalNotes(token, opportunityId, text);
  signal?.throwIfAborted();
  const saved = await apiFetch<PersonalNotes>(path, token, { signal, cache: "no-store" });
  if ((saved.text ?? "") !== text.trim()) throw new MeetingNotesConflict(saved);
  return saved;
}

/** True when the API refused a confirmation because a source changed after the review was loaded. */
export function isStaleReviewError(error: unknown) {
  return error instanceof ApiRequestError && error.status === 409 && error.code === "MEETING_REVIEW_STALE";
}

/** Confirms exactly the sources the owner is looking at; the API refuses anything that changed since. */
export async function confirmMeetingReview(
  token: string, opportunityId: string, review: PostMeetingReview, excluded: Partial<Record<ExtractionCategory, string[]>>,
  signal?: AbortSignal,
) {
  if (review.extraction.status !== "current" || !review.extraction.transcript_id || !review.extraction.generated_at) {
    throw new Error("Analyse the meeting again before confirming: a source changed.");
  }
  const value = await apiFetch<unknown>(postMeetingPath(opportunityId, "post-meeting-review/confirm"), token, {
    method: "POST", signal,
    body: JSON.stringify({
      transcript_id: review.extraction.transcript_id, extraction_generated_at: review.extraction.generated_at,
      // The sources exactly as they were shown: the API refuses the confirmation if any of them changed since.
      review_fingerprint: review.review_fingerprint,
      excluded: Object.fromEntries(Object.entries(excluded).filter(([, items]) => items && items.length)),
    }),
  });
  return parsePostMeetingReview(value, opportunityId);
}

export type FindingsState = "no-transcript" | "not-analysed" | "other-transcript" | "notes-unsaved" | "stale" | "current";

/** What the findings on screen represent, given the owner's current selection and unsaved edits. */
export function findingsState(review: PostMeetingReview, selectedTranscriptId: string, notesDirty: boolean): FindingsState {
  if (!review.transcripts.length || !selectedTranscriptId) return "no-transcript";
  if (review.extraction.status === "missing") return "not-analysed";
  if (review.extraction.transcript_id !== selectedTranscriptId) return "other-transcript";
  if (notesDirty) return "notes-unsaved";
  return review.extraction.status === "current" ? "current" : "stale";
}

/**
 * Findings the owner excluded in the stored confirmation that the current analysis still contains.
 * Also used when the confirmation is out of date, so an earlier exclusion is offered again instead
 * of being dropped; it only counts once the owner confirms again.
 */
export function excludedFromConfirmation(review: PostMeetingReview): Partial<Record<ExtractionCategory, string[]>> {
  const items = review.confirmation.items;
  if (!items) return {};
  return Object.fromEntries(EXTRACTION_FIELDS.map(([key]) => {
    const current = review.extraction.categories[key].map((item) => item.text);
    return [key, items[key].filter((item) => item.status === "excluded" && current.includes(item.text)).map((item) => item.text)];
  }));
}

/** ``v2State`` is what the server reports for Master Presentation V2, once it has been read. */
export function reviewHeadline(review: PostMeetingReview, v2State?: string | null): string {
  if (review.finalized) return "Package finalized";
  if (review.readiness.ready_for_v2 && v2State === "ready") return "Master Presentation V2 is ready";
  if (review.readiness.ready_for_v2 && v2State === "generating") return "Generating Master Presentation V2";
  if (review.readiness.ready_for_v2 && v2State === "outdated") return "Master Presentation V2 needs a new revision";
  if (review.readiness.ready_for_v2) return "Ready for Master Presentation V2";
  if ((review.readiness.blockers ?? []).some((code) => code.startsWith("DISCOVERY2_"))) return "Next: Discovery 2";
  if (review.confirmation.status === "stale") return "Confirmation is out of date";
  if (review.extraction.status === "stale") return "Analysis is out of date";
  if (review.extraction.status === "current" && review.extraction.item_count === 0) return "No findings to confirm";
  if (review.extraction.status === "current") return "Review the findings";
  return review.transcripts.length ? "Analyse the meeting" : "Add your transcript";
}
