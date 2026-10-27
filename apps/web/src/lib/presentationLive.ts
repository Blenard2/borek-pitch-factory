import {
  ApiRequestError, apiFetch, apiFetchBlob, getWorkflowStatus,
  type WorkflowStatusResponse,
} from "./api";
import { deckLanguageSummary } from "./documentLanguage";
import type { FirstPitchResult } from "./ppt1Generation";

export interface DeckResponse {
  presentation_id: string;
  presentation_name: string;
  version_number: number;
  status: string;
  source?: DeckSource | null;
  slides: { slide_id: string; slide_index: number; layout_id: string; preview_url: string | null }[];
  pptx_download_url: string;
  pdf_download_url: string;
}

/** What a Master Presentation version was built from (absent for other decks). */
export interface DeckSource {
  kind: string;
  /** Product stage, e.g. "V1" / "pre_meeting". Not the database version number. */
  product_version: string;
  product_stage: string;
  /** Technical revision of this presentation (presentation_versions.version_number). */
  revision: number;
  master_id: string;
  master_version: string;
  canonical_slide_count: number;
  appendix_slide_count: number;
  approved_discovery_version_id: string;
  discovery_schema_version: string;
  /** Master Presentation V2 only: the V1 version of the same presentation it follows. */
  base_presentation_version_id?: string | null;
  /** BAP-13: language of the client-specific appendix (the approved Discovery's). */
  language?: string;
  /** BAP-13: language of the canonical Borek master slides - they are never translated. */
  master_language?: string;
  /** BAP-13 (V2): confirmed findings that read as another language than the appendix. */
  other_language_findings?: number;
}

export interface SlideResponse {
  id: string;
  presentation_version_id: string;
  slide_index: number;
  layout_id: string;
  slide_spec: { title?: unknown };
}

export interface LiveSlide {
  id: string;
  index: number;
  label: string;
  previewPath: string | null;
  /** True for the client-specific slides added to the canonical Borek deck. */
  appendix?: boolean;
}

/** Layout id of the canonical master slides in a Master Presentation. */
export const CANONICAL_LAYOUT = "CANONICAL";

/**
 * Whether a slide of a Master Presentation is an appendix slide. The closing canonical slide
 * ("Let's talk") follows the appendix (BAP-4), so this is read from the slide's layout, never
 * from its position. Without a layout the position is the only hint left.
 */
export function isAppendixSlide(source: DeckSource | null | undefined, index: number, layoutId: string | null | undefined): boolean {
  if (!source) return false;
  return layoutId ? layoutId !== CANONICAL_LAYOUT : index >= source.canonical_slide_count;
}

export interface LivePresentation extends FirstPitchResult {
  name: string;
  versionNumber: number;
  slides: LiveSlide[];
  source: DeckSource | null;
}

type LiveWorkflow = WorkflowStatusResponse & {
  documents?: { approved_discovery?: { version_id: string } | null };
};

function identityMismatch(): never {
  throw new ApiRequestError(
    "The latest presentation version changed or does not match the pre-meeting presentation. Reload the deck to continue. Historical previews are not supported by this API.",
    409, "PPT1_IDENTITY_MISMATCH",
  );
}

/**
 * Every read is pinned to one version. A Master Presentation holds its V1 and V2 versions under
 * one presentation id, so "the latest version" is not necessarily the one a workspace shows.
 */
function versionPath(identity: Pick<FirstPitchResult, "presentationId" | "presentationVersionId">) {
  if (!/^[a-zA-Z0-9_-]+$/.test(identity.presentationId) || !/^[a-zA-Z0-9_-]+$/.test(identity.presentationVersionId)) identityMismatch();
  return `/presentations/${identity.presentationId}/versions/${identity.presentationVersionId}`;
}

function checkSlides(slides: SlideResponse[], identity: FirstPitchResult) {
  if (slides.some((slide) => slide.presentation_version_id !== identity.presentationVersionId)) identityMismatch();
}

export function adaptLivePresentation(
  identity: FirstPitchResult, deck: DeckResponse, metadata: SlideResponse[],
): LivePresentation {
  if (deck.presentation_id !== identity.presentationId) identityMismatch();
  checkSlides(metadata, identity);
  if (deck.status !== "ready") throw new Error("The presentation files are not ready yet.");
  // /deck may list only the rendered pages. Do not hide unrendered /slides rows.
  const source = deck.source ?? null;
  const indices = new Set([...deck.slides, ...metadata].map((slide) => slide.slide_index));
  if ([...indices].some((index) => !Number.isInteger(index) || index < 0)) {
    throw new Error("The presentation returned an invalid slide index.");
  }
  const slides = [...indices].sort((a, b) => a - b).map((index) => {
    const preview = deck.slides.find((slide) => slide.slide_index === index);
    const slide = metadata.find((slide) => slide.slide_index === index);
    if (preview && slide && preview.slide_id !== slide.id) identityMismatch();
    const title = slide?.slide_spec.title;
    const label = typeof title === "string" && title.trim() ? title.trim()
      : `Slide ${index + 1} - ${slide?.layout_id ?? preview?.layout_id ?? "Untitled"}`;
    const expectedPath = `${versionPath(identity)}/preview/slides/${index}.png`;
    return {
      id: slide?.id ?? preview!.slide_id,
      index,
      label,
      // Only send credentials to the expected same-API slide endpoint.
      previewPath: preview?.preview_url === expectedPath ? expectedPath : null,
      appendix: isAppendixSlide(source, index, slide?.layout_id ?? preview?.layout_id),
    };
  });
  return { ...identity, name: deck.presentation_name, versionNumber: deck.version_number, slides, source };
}

export async function assertCurrentLivePresentation(
  token: string, opportunityId: string, identity: FirstPitchResult, signal?: AbortSignal,
) {
  signal?.throwIfAborted();
  const [workflow, slides] = await Promise.all([
    getWorkflowStatus(token, opportunityId),
    apiFetch<SlideResponse[]>(`${versionPath(identity)}/slides`, token, { signal, cache: "no-store" }),
  ]);
  signal?.throwIfAborted();
  const current = workflow.documents?.ppt1;
  if (current?.presentation_id !== identity.presentationId ||
      current.latest_ready_version_id !== identity.presentationVersionId) identityMismatch();
  checkSlides(slides, identity);
}

export async function loadLivePresentation(
  token: string, opportunityId: string, identity: FirstPitchResult, signal?: AbortSignal,
): Promise<LivePresentation> {
  signal?.throwIfAborted();
  const [deck, slides] = await Promise.all([
    apiFetch<DeckResponse>(`${versionPath(identity)}/deck`, token, { signal, cache: "no-store" }),
    apiFetch<SlideResponse[]>(`${versionPath(identity)}/slides`, token, { signal, cache: "no-store" }),
  ]);
  signal?.throwIfAborted();
  const result = adaptLivePresentation(identity, deck, slides);
  await assertCurrentLivePresentation(token, opportunityId, identity, signal);
  signal?.throwIfAborted();
  return result;
}

export async function loadExistingFirstPitch(token: string, opportunityId: string, signal?: AbortSignal) {
  signal?.throwIfAborted();
  const workflow = await getWorkflowStatus(token, opportunityId) as LiveWorkflow;
  signal?.throwIfAborted();
  const ppt1 = workflow.documents?.ppt1;
  let deck: LivePresentation | null = null;
  let loadError: string | null = null;
  try {
    if (ppt1?.presentation_id && ppt1.latest_ready_version_id) {
      deck = await loadLivePresentation(token, opportunityId, {
        presentationId: ppt1.presentation_id,
        presentationVersionId: ppt1.latest_ready_version_id,
        jobId: null,
      }, signal);
    }
  } catch (error) {
    signal?.throwIfAborted();
    loadError = livePresentationError(error);
  }
  return { deck, loadError, status: ppt1?.status ?? "missing", approvedSourceId: workflow.documents?.approved_discovery?.version_id ?? null };
}

/** One ready version as listed by GET /presentations/{id}/versions. */
export interface PresentationVersionSummary {
  presentation_version_id: string;
  version_number: number;
  status: string;
  is_latest: boolean;
  created_at?: string | null;
  source?: DeckSource | null;
  pptx_download_url: string;
  pdf_download_url: string;
}

export interface EarlierVersion {
  versionId: string;
  versionNumber: number;
  createdAt: string | null;
  source: DeckSource | null;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function versionDownloadPath(presentationId: string, versionId: string, format: "pptx" | "pdf") {
  return `/presentations/${presentationId}/versions/${versionId}/download/${format}`;
}

/**
 * The other ready versions of the presentation, newest first: every version except the one on
 * screen (or except the latest, when no version is named). Rows with unexpected URLs are dropped.
 */
export function adaptEarlierVersions(presentationId: string, rows: PresentationVersionSummary[], currentVersionId?: string): EarlierVersion[] {
  return rows
    .filter((row) => (currentVersionId ? row.presentation_version_id !== currentVersionId : !row.is_latest) &&
      row.status === "ready" && UUID.test(row.presentation_version_id) &&
      // Only send credentials to the expected same-API version endpoints.
      row.pptx_download_url === versionDownloadPath(presentationId, row.presentation_version_id, "pptx") &&
      row.pdf_download_url === versionDownloadPath(presentationId, row.presentation_version_id, "pdf"))
    .map((row) => ({
      versionId: row.presentation_version_id, versionNumber: row.version_number,
      createdAt: row.created_at ?? null, source: row.source ?? null,
    }))
    .sort((a, b) => b.versionNumber - a.versionNumber);
}

export async function loadEarlierVersions(token: string, presentationId: string, signal?: AbortSignal, currentVersionId?: string) {
  const rows = await apiFetch<PresentationVersionSummary[]>(
    `/presentations/${presentationId}/versions`, token, { signal, cache: "no-store" },
  );
  signal?.throwIfAborted();
  return adaptEarlierVersions(presentationId, rows, currentVersionId);
}

/** "Master Presentation V2 · revision 3". The product version comes from the server, never from the number. */
export function versionLabel(version: Pick<EarlierVersion, "versionNumber" | "source">) {
  const product = version.source?.product_version;
  // BAP-12: a version that reports its language names it, so historical English decks stay recognisable.
  const language = typeof version.source?.language === "string" ? ` · ${deckLanguageSummary(version.source)?.label}` : "";
  return `${product === "V1" || product === "V2" ? `Master Presentation ${product} · ` : ""}revision ${version.versionNumber}${language}`;
}

/** A version is immutable, so its files need no latest-version check. */
export async function downloadPresentationVersion(
  token: string, presentationId: string, versionId: string, format: "pptx" | "pdf", signal?: AbortSignal,
) {
  if (!UUID.test(presentationId) || !UUID.test(versionId)) throw new Error("The presentation version is not valid.");
  const blob = await apiFetchBlob(versionDownloadPath(presentationId, versionId, format), token, { signal, cache: "no-store" });
  signal?.throwIfAborted();
  return blob;
}

export function livePresentationError(error: unknown): string {
  if (error instanceof ApiRequestError && (error.status === 401 || error.status === 403)) {
    return "Presentation access was denied. Sign in with an authorized account, then retry.";
  }
  if (error instanceof ApiRequestError && error.code === "SLIDE_PREVIEW_NOT_FOUND") {
    return "The backend has not rendered this slide preview. Retry after previews are available.";
  }
  return error instanceof Error ? error.message : "The presentation could not be loaded. Please retry.";
}

export type LivePreviewState =
  | { state: "loading" }
  | { state: "ready"; url: string }
  | { state: "failed"; message: string };

export function requestLiveSlidePreview(
  token: string, opportunityId: string, deck: LivePresentation, slide: LiveSlide,
  onChange: (state: LivePreviewState) => void,
) {
  const controller = new AbortController();
  const { signal } = controller;
  let objectUrl: string | null = null;
  onChange({ state: "loading" });
  const done = (async () => {
    try {
      if (!slide.previewPath) throw new Error("The backend has not advertised a preview for this slide. Reload the deck to check again.");
      await assertCurrentLivePresentation(token, opportunityId, deck, signal);
      const blob = await apiFetchBlob(slide.previewPath, token, { signal, cache: "no-store" });
      signal.throwIfAborted();
      if (!blob.size || !blob.type.startsWith("image/")) throw new Error("The slide preview response is not an image.");
      // Latest-only endpoints cannot pin a version atomically; reject observable version changes.
      await assertCurrentLivePresentation(token, opportunityId, deck, signal);
      signal.throwIfAborted();
      objectUrl = URL.createObjectURL(blob);
      onChange({ state: "ready", url: objectUrl });
    } catch (error) {
      if (!signal.aborted) onChange({ state: "failed", message: livePresentationError(error) });
    }
  })();
  return {
    done,
    cancel() {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    },
  };
}

export async function downloadLivePresentation(
  token: string, opportunityId: string, deck: LivePresentation, format: "pptx" | "pdf", signal?: AbortSignal,
) {
  await assertCurrentLivePresentation(token, opportunityId, deck, signal);
  const blob = await apiFetchBlob(`${versionPath(deck)}/download/${format}`, token, { signal, cache: "no-store" });
  await assertCurrentLivePresentation(token, opportunityId, deck, signal);
  signal?.throwIfAborted();
  return blob;
}
