import { apiFetch, apiFetchBlob } from "./api";
import { postMeetingPath } from "./postMeeting";

/**
 * BAP-7 Discovery 2: the post-meeting Discovery document. It carries forward the approved
 * Discovery 1 and adds what the first meeting changed; every item names its origin.
 */

export const DISCOVERY2_SECTION_KEYS = [
  "context", "requirements", "challenges", "priorities", "opportunities", "discussed_solutions", "decisions", "follow_ups", "open_questions",
] as const;
export type Discovery2SectionKey = (typeof DISCOVERY2_SECTION_KEYS)[number];
export type Discovery2Origin = "discovery1" | "meeting" | "owner";
export type Discovery2Source = "discovery1" | "transcript" | "personal_notes" | "both" | "owner";

export interface Discovery2Item {
  id: string; text: string; origin: Discovery2Origin; status: "carried_forward" | "new" | "updated";
  source: Discovery2Source; evidence: string | null; discovery1_ref: string | null; edited: boolean;
  /** Only for items from the owner's personal notes: shared with the client. Without it the item is internal. */
  share_with_client?: boolean;
}
export interface Discovery2Section { key: Discovery2SectionKey; title: string; items: Discovery2Item[] }
export interface Discovery2Document {
  /** BAP-13: the content language, inherited from the approved Discovery 1 (absent before BAP-13: English). */
  language?: string;
  status: "not_generated" | "generating" | "ready" | "failed";
  document_id: string | null; generated_at: string | null; title: string; client_name: string;
  error: { code: string; message: string } | null;
  generation: { mode: "fixture" | "live"; generator_version: string } | null;
  summary: { previous: string; meeting: string } | null;
  sources: {
    discovery1: { version_id: string; version_number: number | null; approved_at: string | null };
    transcript: { transcript_id: string; file_name: string | null; revision: string };
    personal_notes: { updated_at: string | null; sha256: string | null };
    meeting_review: { confirmed_at: string | null; confirmed_finding_count: number };
    basis_hash: string;
  } | null;
  sections: Discovery2Section[];
}
export interface Discovery2Version {
  id: string; version_number: number; status: "draft" | "approved"; content_hash: string; basis_hash: string;
  discovery1_version_id: string; generation_mode: "fixture" | "live" | null;
  created_at: string | null; updated_at: string | null; approved_at: string | null; approved_by: string | null;
}
export interface Discovery2State {
  status: "not_generated" | "generating" | "failed" | "draft" | "approved" | "stale";
  working_status: string; error: { code: string; message: string } | null; basis_hash: string | null; can_generate: boolean;
  latest_version: { version_id: string; version_number: number; status: string } | null;
  approved_version: { version_id: string; version_number: number; approved_at: string | null } | null;
  stale_reasons: ("BASIS_CHANGED" | "NEWER_DRAFT")[];
}
export interface PostMeetingDiscovery {
  opportunity_id: string; state: Discovery2State; document: Discovery2Document;
  current_version: Discovery2Version | null; versions: Discovery2Version[];
  input_blockers: string[]; finalized: boolean; ready_for_v2: boolean;
  /** BAP-13: items that read as another language than the document - kept as confirmed, never translated. */
  language_check?: { language: string; other_language_item_ids: string[] } | null;
}

export const ORIGIN_LABEL: Record<Discovery2Origin, string> = {
  discovery1: "From Discovery 1", meeting: "New from the meeting", owner: "Added by you",
};
export const SOURCE_LABEL: Record<Discovery2Source, string> = {
  discovery1: "Discovery 1", transcript: "Transcript", personal_notes: "Your notes", both: "Transcript and notes", owner: "Your wording",
};
export const STATE_LABEL: Record<Discovery2State["status"], string> = {
  not_generated: "Not generated", generating: "Generating", failed: "Generation failed", draft: "Draft · not approved",
  approved: "Approved", stale: "Out of date",
};
export const STALE_TEXT: Record<string, string> = {
  BASIS_CHANGED: "The meeting information changed after this Discovery 2 was generated. Generate it again from the current information.",
  NEWER_DRAFT: "You edited the approved Discovery 2. Approve the new draft before Master Presentation V2 is generated.",
};

const path = (opportunityId: string, suffix = "") => postMeetingPath(opportunityId, `post-meeting-discovery${suffix}`);

export function parsePostMeetingDiscovery(value: unknown): PostMeetingDiscovery {
  const record = value as Partial<PostMeetingDiscovery> | null;
  if (!record || typeof record !== "object" || !record.state || !record.document || !Array.isArray(record.versions) || !Array.isArray(record.document.sections)) {
    throw new Error("The Discovery 2 response is incomplete. Reload to retry.");
  }
  return record as PostMeetingDiscovery;
}

export async function loadPostMeetingDiscovery(token: string, opportunityId: string, signal?: AbortSignal) {
  return parsePostMeetingDiscovery(await apiFetch(path(opportunityId), token, { signal, cache: "no-store" }));
}

/**
 * BAP-12: with ``language`` the server refuses (409 LANGUAGE_MISMATCH, before anything is written)
 * when the approved Discovery 1 is in another language; without it, Discovery 2 inherits that language.
 */
export async function generatePostMeetingDiscovery(token: string, opportunityId: string, language?: "en" | "de") {
  return parsePostMeetingDiscovery(await apiFetch(path(opportunityId, "/generate"), token, {
    method: "POST",
    ...(language ? { body: JSON.stringify({ language }) } : {}),
  }));
}

/**
 * Audit 2026-10-27 (BUG-02): a point that rests only on the owner's personal notes is internal.
 * It stays here for review, but no customer document (Word export, Master Presentation V2) shows
 * it until the owner shares it with the client.
 */
export const isInternalOnly = (item: Pick<Discovery2Item, "source" | "share_with_client">) =>
  item.source === "personal_notes" && item.share_with_client !== true;

/** One item as edited. ``share_with_client`` is set only for items from the personal notes. */
export type Discovery2EditItem = { id?: string; text: string; share_with_client?: boolean };
/** The edited sections, each as its items in order; an item without ``id`` is new. */
export type Discovery2Edits = { key: Discovery2SectionKey; items: Discovery2EditItem[] }[];

export async function savePostMeetingDiscovery(token: string, opportunityId: string, current: Discovery2Version, sections: Discovery2Edits) {
  return parsePostMeetingDiscovery(await apiFetch(path(opportunityId), token, {
    method: "PATCH",
    body: JSON.stringify({ expected_version_id: current.id, expected_content_hash: current.content_hash, sections }),
  }));
}

export async function approvePostMeetingDiscovery(token: string, opportunityId: string, current: Discovery2Version) {
  return parsePostMeetingDiscovery(await apiFetch(path(opportunityId, "/approve"), token, {
    method: "POST",
    body: JSON.stringify({ expected_version_id: current.id, expected_content_hash: current.content_hash }),
  }));
}

export async function downloadDiscovery2Docx(token: string, opportunityId: string, version: Pick<Discovery2Version, "id">) {
  const blob = await apiFetchBlob(path(opportunityId, `/versions/${encodeURIComponent(version.id)}/download/docx`), token, { cache: "no-store", redirect: "error" });
  if (!blob.size) throw new Error("The Discovery 2 file is empty.");
  return blob;
}

/** The sections as editable text, in order; items from the personal notes with their share choice. */
export function editableSections(document: Discovery2Document): Record<Discovery2SectionKey, Discovery2EditItem[]> {
  return Object.fromEntries(document.sections.map((section) => [section.key, section.items.map((item) => (
    item.source === "personal_notes" ? { id: item.id, text: item.text, share_with_client: item.share_with_client === true } : { id: item.id, text: item.text }
  ))])) as Record<Discovery2SectionKey, Discovery2EditItem[]>;
}

/** Only the sections whose items changed are sent. */
export function changedSections(document: Discovery2Document, edits: Record<Discovery2SectionKey, Discovery2EditItem[]>): Discovery2Edits {
  const original = editableSections(document);
  return DISCOVERY2_SECTION_KEYS.filter((key) => JSON.stringify(original[key] ?? []) !== JSON.stringify(edits[key] ?? []))
    .map((key) => ({
      key,
      items: (edits[key] ?? []).map((item) => (item.id
        ? { id: item.id, text: item.text, ...(item.share_with_client === undefined ? {} : { share_with_client: item.share_with_client }) }
        : { text: item.text })),
    }));
}

/**
 * BAP-12: the language of the approved Discovery 2 that Master Presentation V2 is built from, when
 * the approved version is the current one (otherwise V2 is blocked by Discovery 2 anyway).
 */
export function approvedDiscovery2Language(data: PostMeetingDiscovery | null): { language: "en" | "de"; versionNumber: number } | null {
  const approved = data?.state.approved_version;
  if (!data || !approved || data.current_version?.id !== approved.version_id) return null;
  return { language: data.document.language === "de" ? "de" : "en", versionNumber: approved.version_number };
}

/** Discovery 2 must be approved and current before Master Presentation V2. */
export const DISCOVERY2_BLOCKERS = ["DISCOVERY2_MISSING", "DISCOVERY2_NOT_APPROVED", "DISCOVERY2_STALE"];
export const needsDiscovery2 = (blockers: string[]) => blockers.some((code) => DISCOVERY2_BLOCKERS.includes(code));
