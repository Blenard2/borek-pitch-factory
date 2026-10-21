import { apiFetch, apiFetchBlob, resolveBackendOpportunityId } from "./api";
import type { AnalysisPage } from "./discoveryAnalysis";
import { printAnalysisPdf } from "./discoveryAnalysisSheets";
import { printDiscoveryPdf } from "./discoveryWhitePaper";
import { adaptLiveDiscovery } from "./liveDiscovery";
import { adaptEarlierVersions, downloadPresentationVersion, type EarlierVersion } from "./presentationLive";

/**
 * BAP-11: the saved documents of a client after the first meeting, in one list.
 *
 * The Pre-Meeting Discovery Document is the approved Discovery version Presentation V1 was built
 * from, as the backend resolves it; the Post-Meeting Discovery Document is the approved Discovery
 * 2. Every file comes from a saved version: Word from the version's export, PDF printed from the
 * version's stored pages, presentations from their version files. Nothing is generated.
 */

export const PRE_MEETING_LABEL = "Pre-Meeting Discovery Document";
export const POST_MEETING_LABEL = "Post-Meeting Discovery Document";

export type PreMeetingDiscovery =
  | {
      state: "available"; label: string; resolution: string; version_id: string; version_number: number | null;
      document_id: string; approved_at: string | null; format: "analysis" | "legacy"; docx_available: boolean;
      version_path: string; docx_download_path: string | null; presentation_version_id: string | null;
    }
  | { state: "unavailable"; label: string; reason: string; message: string };

export type PostMeetingDiscovery =
  | { state: "available"; label: string; version_id: string; version_number: number | null; approved_at: string | null; docx_download_path: string }
  | { state: "unavailable"; label: string; reason: string; message: string };

export interface DiscoveryDocuments {
  opportunity_id: string;
  pre_meeting: PreMeetingDiscovery;
  post_meeting: PostMeetingDiscovery;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function base(opportunityId: string) {
  return `/opportunities/${resolveBackendOpportunityId(opportunityId)}`;
}

/** Only paths of this client's own version endpoints are ever requested with credentials. */
function expected(path: unknown, opportunityId: string, pattern: RegExp): string | null {
  const value = typeof path === "string" ? path : "";
  return value.startsWith(`${base(opportunityId)}/`) && pattern.test(value.slice(base(opportunityId).length)) ? value : null;
}

const VERSION_PATH = /^\/discovery-paper\/versions\/[0-9a-f-]{36}$/i;
const DOCX_PATH = /^\/discovery-paper\/versions\/[0-9a-f-]{36}\/download\/docx$/i;
const DISCOVERY2_DOCX_PATH = /^\/post-meeting-discovery\/versions\/[0-9a-f-]{36}\/download\/docx$/i;

export async function loadDiscoveryDocuments(token: string, opportunityId: string, signal?: AbortSignal): Promise<DiscoveryDocuments> {
  const payload = await apiFetch<DiscoveryDocuments>(`${base(opportunityId)}/pre-meeting-discovery`, token, { signal, cache: "no-store" });
  signal?.throwIfAborted();
  return adaptDiscoveryDocuments(payload, opportunityId);
}

/** The response as the page may use it: a document whose paths are not this client's is unavailable. */
export function adaptDiscoveryDocuments(payload: DiscoveryDocuments, opportunityId: string): DiscoveryDocuments {
  if (payload.opportunity_id !== resolveBackendOpportunityId(opportunityId)) throw new Error("The documents belong to another client.");
  const broken = (label: string) => ({ state: "unavailable" as const, label, reason: "INVALID_RESPONSE", message: "This document could not be read. Reload the page." });
  let pre = payload.pre_meeting;
  if (pre?.state === "available") {
    const ok = UUID.test(pre.version_id) && expected(pre.version_path, opportunityId, VERSION_PATH) &&
      (pre.docx_available ? expected(pre.docx_download_path, opportunityId, DOCX_PATH) : pre.docx_download_path === null);
    pre = ok ? { ...pre, label: PRE_MEETING_LABEL } : broken(PRE_MEETING_LABEL);
  } else {
    pre = { ...(pre ?? broken(PRE_MEETING_LABEL)), label: PRE_MEETING_LABEL } as PreMeetingDiscovery;
  }
  let post = payload.post_meeting;
  if (post?.state === "available") {
    post = UUID.test(post.version_id) && expected(post.docx_download_path, opportunityId, DISCOVERY2_DOCX_PATH)
      ? { ...post, label: POST_MEETING_LABEL } : broken(POST_MEETING_LABEL);
  } else {
    post = { ...(post ?? broken(POST_MEETING_LABEL)), label: POST_MEETING_LABEL } as PostMeetingDiscovery;
  }
  return { opportunity_id: payload.opportunity_id, pre_meeting: pre, post_meeting: post };
}

/** The Word file of the saved Pre-Meeting Discovery version (existing export). */
export async function downloadPreMeetingDocx(token: string, document: Extract<PreMeetingDiscovery, { state: "available" }>): Promise<Blob> {
  if (!document.docx_available || !document.docx_download_path) throw new Error("This Discovery version has no Word export.");
  const blob = await apiFetchBlob(document.docx_download_path, token, { cache: "no-store", redirect: "error" });
  if (!blob.size) throw new Error("The Discovery file is empty.");
  return blob;
}

/** The Word file of the approved Post-Meeting Discovery version (existing Discovery 2 export). */
export async function downloadPostMeetingDocx(token: string, document: Extract<PostMeetingDiscovery, { state: "available" }>): Promise<Blob> {
  const blob = await apiFetchBlob(document.docx_download_path, token, { cache: "no-store", redirect: "error" });
  if (!blob.size) throw new Error("The Discovery file is empty.");
  return blob;
}

/**
 * PDF of the saved Pre-Meeting Discovery version, printed from its stored pages (the current
 * format) or through the earlier seven-page print path. The saved version is read, never rebuilt.
 */
export async function printPreMeetingDiscovery(
  token: string, opportunityId: string, document: Extract<PreMeetingDiscovery, { state: "available" }>, clientName: string,
): Promise<void> {
  const row = await apiFetch<{ id: string; status: string; paper_json: Record<string, unknown> }>(document.version_path, token, { cache: "no-store" });
  if (row.id !== document.version_id || row.status !== "approved") throw new Error("The saved Discovery version could not be read.");
  const paper = row.paper_json ?? {};
  if (paper.opportunity_id !== resolveBackendOpportunityId(opportunityId)) throw new Error("The Discovery version belongs to another client.");
  if (document.format === "analysis") {
    const pages = Array.isArray(paper.page_manifest) ? (paper.page_manifest as AnalysisPage[]) : [];
    if (!pages.length) throw new Error("The saved Discovery version has no pages.");
    const title = String(pages[0]?.content?.title ?? PRE_MEETING_LABEL);
    printAnalysisPdf(pages, `${title} - ${PRE_MEETING_LABEL}`);
    return;
  }
  printDiscoveryPdf(adaptLiveDiscovery(row, opportunityId, resolveBackendOpportunityId(opportunityId)), clientName);
}

/** Every ready version of a presentation, the latest included, with number and date. */
export async function loadPresentationVersions(token: string, presentationId: string, signal?: AbortSignal): Promise<EarlierVersion[]> {
  if (!UUID.test(presentationId)) return [];
  const rows = await apiFetch<Parameters<typeof adaptEarlierVersions>[1]>(`/presentations/${presentationId}/versions`, token, { signal, cache: "no-store" });
  signal?.throwIfAborted();
  return adaptEarlierVersions(presentationId, rows.map((row) => ({ ...row, is_latest: false })));
}

export { downloadPresentationVersion };

/** "17 October 2026" for an ISO timestamp; empty when unknown. */
export function documentDate(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
}
