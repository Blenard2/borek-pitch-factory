import { ApiRequestError, apiFetch, apiFetchBlob, resolveBackendOpportunityId, type WorkflowStatusResponse } from "./api";
import { isAppendixSlide, type DeckResponse, type DeckSource, type SlideResponse, type LiveSlide, type LivePreviewState } from "./presentationLive";

export type { LivePreviewState } from "./presentationLive";

export interface PostMeetingPresentation {
  presentationId: string;
  presentationVersionId: string;
  name: string;
  versionNumber: number;
  slides: LiveSlide[];
  downloads: Record<"pptx" | "pdf", string | null>;
  /** Master Presentation V2 only: master, approved Discovery and the V1 version it follows. */
  source: DeckSource | null;
}

type Identity = Pick<PostMeetingPresentation, "presentationId" | "presentationVersionId">;
type Workflow = WorkflowStatusResponse & { opportunity_id?: string };

function identityMismatch(): never {
  throw new ApiRequestError(
    "The latest presentation version changed or does not match this opportunity's PPT #2. Reload the deck to continue. Historical previews are not supported by this API.",
    409, "PPT2_IDENTITY_MISMATCH",
  );
}

// Pinned to one version: Master Presentation V2 shares its presentation id with V1, so the
// unversioned "latest" routes could serve a different version than the one under review.
function presentationPath(identity: Identity) {
  if (!/^[a-zA-Z0-9_-]+$/.test(identity.presentationId) || !/^[a-zA-Z0-9_-]+$/.test(identity.presentationVersionId ?? "")) identityMismatch();
  return `/presentations/${identity.presentationId}/versions/${identity.presentationVersionId}`;
}

async function readWorkflow(token: string, opportunityId: string, signal?: AbortSignal) {
  signal?.throwIfAborted();
  const id = resolveBackendOpportunityId(opportunityId);
  // Same contract as getWorkflowStatus, with cancellation and no cached latest identity.
  const workflow = await apiFetch<Workflow>(`/opportunities/${encodeURIComponent(id)}/workflow-status`, token, { signal, cache: "no-store" });
  signal?.throwIfAborted();
  if (workflow.opportunity_id !== undefined && workflow.opportunity_id !== id) identityMismatch();
  const current = workflow.documents?.ppt2;
  if (current && current.journey_stage !== "post_meeting") identityMismatch();
  return current;
}

function checkSlides(slides: SlideResponse[], identity: Identity) {
  if (!Array.isArray(slides)) throw new Error("PPT #2 returned invalid slide metadata.");
  const ids = new Set<string>();
  const indices = new Set<number>();
  for (const slide of slides) {
    if (slide.presentation_version_id !== identity.presentationVersionId) identityMismatch();
    if (!slide.id || !Number.isInteger(slide.slide_index) || slide.slide_index < 0 ||
        ids.has(slide.id) || indices.has(slide.slide_index) || typeof slide.layout_id !== "string") {
      throw new Error("PPT #2 returned invalid or duplicate slide metadata.");
    }
    ids.add(slide.id);
    indices.add(slide.slide_index);
  }
}

// Adapted from presentationLive; this module never resolves or loads the PPT1 family.
export function adaptPostMeetingPresentation(identity: Identity, deck: DeckResponse, metadata: SlideResponse[]): PostMeetingPresentation {
  const prefix = presentationPath(identity);
  if (deck.presentation_id !== identity.presentationId) identityMismatch();
  checkSlides(metadata, identity);
  if (deck.status !== "ready") throw new Error("PPT #2 artifacts are not ready yet. Review meeting inputs or reload the deck.");
  if (!Array.isArray(deck.slides) || !Number.isInteger(deck.version_number) || deck.version_number < 1 || typeof deck.presentation_name !== "string") {
    throw new Error("PPT #2 returned invalid deck metadata.");
  }
  const indices = new Set<number>();
  for (const preview of deck.slides) {
    const slide = metadata.find((entry) => entry.slide_index === preview.slide_index);
    if (!slide || slide.id !== preview.slide_id || slide.layout_id !== preview.layout_id || indices.has(preview.slide_index)) identityMismatch();
    indices.add(preview.slide_index);
  }
  // Metadata includes unrendered slides; never substitute Discovery pages or hide missing images.
  const slides = [...metadata].sort((a, b) => a.slide_index - b.slide_index).map((slide) => {
    const preview = deck.slides.find((entry) => entry.slide_index === slide.slide_index);
    const expected = `${prefix}/preview/slides/${slide.slide_index}.png`;
    const title = slide.slide_spec?.title;
    return {
      id: slide.id, index: slide.slide_index,
      label: typeof title === "string" && title.trim() ? title.trim() : `Slide ${slide.slide_index + 1} - ${slide.layout_id}`,
      previewPath: preview?.preview_url === expected ? expected : null,
      appendix: isAppendixSlide(deck.source, slide.slide_index, slide.layout_id),
    };
  });
  return {
    ...identity, name: deck.presentation_name, versionNumber: deck.version_number, slides, source: deck.source ?? null,
    downloads: {
      pptx: deck.pptx_download_url === `${prefix}/download/pptx` ? deck.pptx_download_url : null,
      pdf: deck.pdf_download_url === `${prefix}/download/pdf` ? deck.pdf_download_url : null,
    },
  };
}

async function assertCurrentPresentation(token: string, opportunityId: string, identity: Identity, signal?: AbortSignal) {
  const prefix = presentationPath(identity);
  signal?.throwIfAborted();
  const [current, metadata] = await Promise.all([
    readWorkflow(token, opportunityId, signal),
    apiFetch<SlideResponse[]>(`${prefix}/slides`, token, { signal, cache: "no-store" }),
  ]);
  signal?.throwIfAborted();
  if (current?.presentation_id !== identity.presentationId || current.latest_ready_version_id !== identity.presentationVersionId) identityMismatch();
  checkSlides(metadata, identity);
  return metadata;
}

export async function loadExistingPostMeetingPresentation(token: string, opportunityId: string, signal?: AbortSignal) {
  const current = await readWorkflow(token, opportunityId, signal);
  if (!current?.latest_ready_version_id || !current.presentation_id) return { deck: null, status: current?.status ?? "missing" };
  const identity = { presentationId: current.presentation_id, presentationVersionId: current.latest_ready_version_id };
  const prefix = presentationPath(identity);
  const [deck, metadata] = await Promise.all([
    apiFetch<DeckResponse>(`${prefix}/deck`, token, { signal, cache: "no-store" }),
    apiFetch<SlideResponse[]>(`${prefix}/slides`, token, { signal, cache: "no-store" }),
  ]);
  signal?.throwIfAborted();
  const result = adaptPostMeetingPresentation(identity, deck, metadata);
  const latestMetadata = await assertCurrentPresentation(token, opportunityId, identity, signal);
  adaptPostMeetingPresentation(identity, deck, latestMetadata);
  return { deck: result, status: current.status };
}

export function postMeetingPresentationError(error: unknown): string {
  if (error instanceof ApiRequestError && (error.status === 401 || error.status === 403)) {
    return "Presentation access was denied. Sign in with an authorized account, then retry.";
  }
  if (error instanceof ApiRequestError && error.code === "SLIDE_PREVIEW_NOT_FOUND") {
    return "The backend has not rendered this slide preview. Retry after previews are available.";
  }
  return error instanceof Error ? error.message : "PPT #2 could not be loaded. Please retry.";
}

export function requestPostMeetingSlidePreview(
  token: string, opportunityId: string, deck: PostMeetingPresentation, slide: LiveSlide,
  onChange: (state: LivePreviewState) => void,
) {
  const controller = new AbortController();
  const { signal } = controller;
  let objectUrl: string | null = null;
  onChange({ state: "loading" });
  const done = (async () => {
    try {
      const expected = `${presentationPath(deck)}/preview/slides/${slide.index}.png`;
      if (!Number.isInteger(slide.index) || slide.index < 0 || slide.previewPath !== expected ||
          !deck.slides.some((entry) => entry.id === slide.id && entry.index === slide.index && entry.previewPath === expected)) {
        throw new Error("The backend has not advertised a safe preview for this slide. Reload the deck to check again.");
      }
      const before = await assertCurrentPresentation(token, opportunityId, deck, signal);
      if (!before.some((entry) => entry.id === slide.id && entry.slide_index === slide.index)) identityMismatch();
      const blob = await apiFetchBlob(expected, token, { signal, cache: "no-store", redirect: "error" });
      signal.throwIfAborted();
      if (!blob.size || !blob.type.startsWith("image/")) throw new Error("The slide preview response is not an image.");
      // Latest-only endpoints cannot pin a historical version atomically.
      const after = await assertCurrentPresentation(token, opportunityId, deck, signal);
      if (!after.some((entry) => entry.id === slide.id && entry.slide_index === slide.index)) identityMismatch();
      signal.throwIfAborted();
      objectUrl = URL.createObjectURL(blob);
      onChange({ state: "ready", url: objectUrl });
    } catch (error) {
      if (!signal.aborted) onChange({ state: "failed", message: postMeetingPresentationError(error) });
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

export async function downloadPostMeetingPresentation(
  token: string, opportunityId: string, deck: PostMeetingPresentation, format: "pptx" | "pdf", signal?: AbortSignal,
) {
  if (format !== "pptx" && format !== "pdf") throw new Error("Unsupported presentation download format.");
  const expected = `${presentationPath(deck)}/download/${format}`;
  if (deck.downloads[format] !== expected) throw new Error(`The backend has not advertised a safe ${format.toUpperCase()} download. Reload the deck.`);
  await assertCurrentPresentation(token, opportunityId, deck, signal);
  const blob = await apiFetchBlob(expected, token, { signal, cache: "no-store", redirect: "error" });
  signal?.throwIfAborted();
  const mime = format === "pdf" ? "application/pdf" : "application/vnd.openxmlformats-officedocument.presentationml.presentation";
  if (!blob.size || (blob.type !== mime && blob.type !== "application/octet-stream")) throw new Error(`The ${format.toUpperCase()} response is not a presentation file.`);
  await assertCurrentPresentation(token, opportunityId, deck, signal);
  signal?.throwIfAborted();
  return blob;
}
