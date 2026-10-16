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
}
export interface Discovery2Section { key: Discovery2SectionKey; title: string; items: Discovery2Item[] }
export interface Discovery2Document {
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

export async function generatePostMeetingDiscovery(token: string, opportunityId: string) {
  return parsePostMeetingDiscovery(await apiFetch(path(opportunityId, "/generate"), token, { method: "POST" }));
}

/** The edited sections, each as its items in order; an item without ``id`` is new. */
export type Discovery2Edits = { key: Discovery2SectionKey; items: { id?: string; text: string }[] }[];

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

/** The sections as editable text, in order. */
export function editableSections(document: Discovery2Document): Record<Discovery2SectionKey, { id?: string; text: string }[]> {
  return Object.fromEntries(document.sections.map((section) => [section.key, section.items.map((item) => ({ id: item.id, text: item.text }))])) as Record<Discovery2SectionKey, { id?: string; text: string }[]>;
}

/** Only the sections whose items changed are sent. */
export function changedSections(document: Discovery2Document, edits: Record<Discovery2SectionKey, { id?: string; text: string }[]>): Discovery2Edits {
  const original = editableSections(document);
  return DISCOVERY2_SECTION_KEYS.filter((key) => JSON.stringify(original[key] ?? []) !== JSON.stringify(edits[key] ?? []))
    .map((key) => ({ key, items: (edits[key] ?? []).map((item) => (item.id ? { id: item.id, text: item.text } : { text: item.text })) }));
}

/** Discovery 2 must be approved and current before Master Presentation V2. */
export const DISCOVERY2_BLOCKERS = ["DISCOVERY2_MISSING", "DISCOVERY2_NOT_APPROVED", "DISCOVERY2_STALE"];
export const needsDiscovery2 = (blockers: string[]) => blockers.some((code) => DISCOVERY2_BLOCKERS.includes(code));
