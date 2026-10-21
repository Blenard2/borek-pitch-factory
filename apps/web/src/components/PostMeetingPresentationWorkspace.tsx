"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/components/AuthProvider";
import { usePostMeeting } from "@/components/PostMeetingShell";
import { OwnerCheckpointPanel } from "@/components/OwnerCheckpointPanel";
import { PostMeetingDocuments } from "@/components/PostMeetingDocuments";
import { postMeetingDeckLabel } from "@/lib/masterPresentationV2";
import { downloadPresentationVersion, loadEarlierVersions, versionLabel, type EarlierVersion } from "@/lib/presentationLive";
import {
  downloadPostMeetingPresentation, loadExistingPostMeetingPresentation, postMeetingPresentationError,
  requestPostMeetingSlidePreview, type PostMeetingPresentation, type LivePreviewState,
} from "@/lib/postMeetingPresentation";
import styles from "./post-meeting.module.css";

export function PostMeetingPresentationWorkspace({ opportunityId }: { opportunityId: string }) {
  const { accessToken, previewMode } = useAuth();
  return <PresentationSession key={`${opportunityId}:${previewMode}:${accessToken}`} opportunityId={opportunityId}
    accessToken={accessToken} live={!previewMode} />;
}

function PresentationSession({ opportunityId, accessToken, live }: {
  opportunityId: string; accessToken: string | null; live: boolean;
}) {
  const { workflow, refreshWorkflow, companyName } = usePostMeeting();
  const [deck, setDeck] = useState<PostMeetingPresentation | null>(null);
  const [phase, setPhase] = useState<"loading" | "failed" | "idle" | "ready">(live ? "loading" : "idle");
  const [serverStatus, setServerStatus] = useState("missing");
  const [refresh, setRefresh] = useState(0);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [preview, setPreview] = useState<{ key: string; value: LivePreviewState } | null>(null);
  const [previewStates, setPreviewStates] = useState<Record<number, "ready" | "generating" | "failed">>({});
  const [error, setError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<"pptx" | "pdf" | null>(null);
  const downloadOperation = useRef<AbortController | null>(null);
  const previewOperation = useRef<{ cancel(): void } | null>(null);
  const slides = deck?.slides ?? [];
  const selected = slides[selectedIndex] ?? slides[0];
  const deckKey = deck ? `${deck.presentationId}:${deck.presentationVersionId}` : "";
  const previewKey = `${deckKey}:${selected?.index}:${previewAttempt}`;
  const previewState = preview?.key === previewKey ? preview.value : { state: "loading" as const };
  const readyCount = slides.filter((slide) => previewStates[slide.index] === "ready").length;
  const pageNumber = selected ? String(selected.index + 1).padStart(2, "0") : "";
  const root = `/opportunities/${encodeURIComponent(opportunityId)}`;
  // Master Presentation V2 is a version of the same presentation as V1; earlier pitches have a standalone PPT #2.
  const master = deck?.source?.product_version === "V2" || workflow?.documents.ppt2?.product_version === "V2";
  const product = postMeetingDeckLabel(master ? "V2" : null);
  const [others, setOthers] = useState<{ key: string; versions: EarlierVersion[] }>({ key: "", versions: [] });
  const statusText = !live ? `No generated ${product} in layout preview` : phase === "loading" ? `Loading ${product}`
    : phase === "failed" ? `${product} could not be loaded` : deck ? `${product} loaded for review`
    : serverStatus === "missing" ? `No generated ${product} yet` : `${product} status: ${serverStatus}`;

  useEffect(() => {
    if (!live) return;
    const controller = new AbortController();
    setPhase("loading");
    setDeck(null);
    setPreview(null);
    setPreviewStates({});
    setError(null);
    if (!accessToken) {
      setPhase("failed");
      setError("Sign in to load the presentation.");
    } else {
      void loadExistingPostMeetingPresentation(accessToken, opportunityId, controller.signal).then((result) => {
        if (controller.signal.aborted) return;
        setDeck(result.deck);
        setServerStatus(result.status);
        setSelectedIndex(0);
        setPhase(result.deck ? "ready" : "idle");
      }).catch((cause) => {
        if (controller.signal.aborted) return;
        setPhase("failed");
        setError(postMeetingPresentationError(cause));
      });
    }
    return () => {
      controller.abort();
      downloadOperation.current?.abort();
      previewOperation.current?.cancel();
    };
  }, [accessToken, live, opportunityId, refresh]);

  useEffect(() => {
    if (phase !== "ready" || !accessToken || !deck || !selected) return;
    const request = requestPostMeetingSlidePreview(accessToken, opportunityId, deck, selected, (value) => {
      setPreview({ key: previewKey, value });
      // Only image onLoad marks a preview ready, after the browser decodes it.
      setPreviewStates((states) => ({ ...states, [selected.index]: value.state === "failed" ? "failed" : "generating" }));
    });
    previewOperation.current = request;
    return () => {
      request.cancel();
      if (previewOperation.current === request) previewOperation.current = null;
      setPreviewStates((states) => {
        if (states[selected.index] !== "generating") return states;
        const next = { ...states };
        delete next[selected.index];
        return next;
      });
    };
  }, [accessToken, deck, opportunityId, phase, previewKey, selected]);

  useEffect(() => {
    if (!live || !accessToken || !deck?.source) return;
    const controller = new AbortController();
    // The list is an addition to the deck: if it cannot be read, the deck stays usable.
    void loadEarlierVersions(accessToken, deck.presentationId, controller.signal, deck.presentationVersionId)
      .then((versions) => setOthers({ key: deckKey, versions }))
      .catch(() => { if (!controller.signal.aborted) setOthers({ key: deckKey, versions: [] }); });
    return () => controller.abort();
  }, [accessToken, deck, deckKey, live]);

  async function downloadOther(version: EarlierVersion, format: "pptx" | "pdf") {
    if (!accessToken || !deck) return;
    setError(null);
    try {
      const blob = await downloadPresentationVersion(accessToken, deck.presentationId, version.versionId, format);
      const url = URL.createObjectURL(blob);
      try {
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = `master-presentation-${version.source?.product_version ?? "version"}-revision-${version.versionNumber}-${opportunityId}.${format}`.toLowerCase();
        anchor.click();
      } finally { URL.revokeObjectURL(url); }
    } catch (cause) { setError(postMeetingPresentationError(cause)); }
  }

  function reload() {
    previewOperation.current?.cancel();
    setDeck(null);
    setPhase("loading");
    setRefresh((value) => value + 1);
    refreshWorkflow();
  }

  async function download(format: "pptx" | "pdf") {
    if (!accessToken || !deck || phase !== "ready" || downloadOperation.current) return;
    const controller = new AbortController();
    downloadOperation.current = controller;
    setDownloading(format);
    setError(null);
    try {
      const blob = await downloadPostMeetingPresentation(accessToken, opportunityId, deck, format, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      try {
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = master ? `master-presentation-v2-revision-${deck.versionNumber}-${opportunityId}.${format}` : `ppt-2-${opportunityId}.${format}`;
        anchor.click();
      } finally { URL.revokeObjectURL(url); }
    } catch (cause) {
      if (!controller.signal.aborted) setError(postMeetingPresentationError(cause));
    } finally {
      if (downloadOperation.current === controller) downloadOperation.current = null;
      if (!controller.signal.aborted) setDownloading(null);
    }
  }

  // Keep the pre-meeting workspace's visual structure without its fixture or PPT1 logic.
  return <section className="workflow-artifact-workspace artifact-preview-workspace presentation-document-workspace">
    <header className={styles.heading}>
      <span className={styles.eyebrow}>Post-meeting{companyName ? ` / ${companyName}` : ""}</span>
      <h1>Updating your pitch</h1>
      <p>{product} / Post-meeting presentation</p>
    </header>
    <nav className={styles.tabs} aria-label="Post-meeting documents">
      <Link href={`${root}/meeting`}>Meeting inputs</Link>
      <Link href={`${root}/post-meeting-presentation`} aria-current="page">Presentation / {product}</Link>
      <Link href={`${root}/follow-up`}>Follow-up email</Link>
    </nav>
    <progress className="discovery-progress" max={slides.length || 1} value={readyCount} aria-label={`${readyCount} of ${slides.length} ${product} slide previews loaded`} />
    {error ? <p className={styles.error} role="alert">{error}</p> : null}
    {!live ? <p className={styles.notice}>No generated post-meeting presentation is available in preview mode. Sign in to load real slide images and presentation files. Pre-meeting Discovery content is not a preview of it.</p> : null}
    <div className="workflow-artifact-grid">
      <aside className="workflow-page-list" aria-label={`${product} slides`}>
        <div className="discovery-list-heading"><span>Slides</span><span>{deck?.source ? `${deck.source.canonical_slide_count} + ${deck.source.appendix_slide_count} appendix` : "View only"}</span></div>
        {!slides.length ? <p className="discovery-preview-caption">{phase === "loading" ? "Loading slide list..." : "No slides loaded."}</p> : null}
        <ol>{slides.map((slide, index) => {
          const state = previewStates[slide.index] ?? (slide.previewPath ? "waiting" : "failed");
          return <li key={slide.id} className={selected?.id === slide.id ? "is-selected" : undefined}>
            <button type="button" onClick={() => setSelectedIndex(index)} aria-current={selected?.id === slide.id ? "page" : undefined}>
              <span className={`discovery-thumbnail presentation-thumbnail is-${state}`} aria-hidden="true"><i /><i /><i /><i /></span>
              <span className="discovery-page-label"><strong><span className="workflow-page-number">{String(slide.index + 1).padStart(2, "0")}</span>{slide.label}</strong>
                <small className={`is-${state}`}>{state === "ready" ? "Preview loaded" : state === "generating" ? "Loading preview" : state === "failed" ? "Preview unavailable" : "Preview not loaded"}</small></span>
              <span className={`discovery-status-mark is-${state}`} aria-hidden="true">{state === "ready" ? <svg viewBox="0 0 16 16" fill="none"><path d="m4 8 3 3 5-6" stroke="currentColor" strokeWidth="1.5" /></svg> : state === "failed" ? "!" : <i />}</span>
            </button>
          </li>;
        })}</ol>
      </aside>
      <article className="workflow-preview-panel" aria-label={`Selected ${product} slide`}>
        <div className="discovery-page-toolbar"><p className="workflow-panel-label">{selected ? `Slide ${pageNumber} / ${selected.label}` : "No slides"}</p><span className="presentation-view-only">{product} / View only</span></div>
        <div className="discovery-preview-content">
          <div className="presentation-slide-canvas presentation-slide-state">
            {deck && selected ? previewState.state === "ready" ? (
              <img key={previewKey} src={previewState.url} alt={`${product} slide ${pageNumber}: ${selected.label}`} className="presentation-live-preview"
                style={{ width: "100%", maxWidth: "100%", height: "auto", objectFit: "contain" }}
                onLoad={() => setPreviewStates((states) => ({ ...states, [selected.index]: "ready" }))}
                onError={() => {
                  previewOperation.current?.cancel();
                  setPreview({ key: previewKey, value: { state: "failed", message: "This slide image could not be displayed. Retry the preview." } });
                  setPreviewStates((states) => ({ ...states, [selected.index]: "failed" }));
                }} />
            ) : <>
              <strong>{previewState.state === "loading" ? "Loading slide preview" : "Slide preview unavailable"}</strong>
              <p role="status">{previewState.state === "failed" ? previewState.message : "Fetching the selected slide image securely."}</p>
              {previewState.state === "failed" ? <button className="btn btn-secondary" type="button" disabled={Boolean(downloading)} onClick={() => selected.previewPath ? setPreviewAttempt((value) => value + 1) : reload()}>Retry preview</button> : null}
            </> : <>
              <strong>{deck ? "No slides returned" : statusText}</strong>
              <p>Confirm the meeting findings and generate Master Presentation V2 on the Meeting inputs page, then reload here. Only backend-rendered slide images appear in this workspace.</p>
            </>}
          </div>
          <p className="discovery-preview-caption">{master ? "Previews and downloads are those of this exact version. Master Presentation V1 stays available as an earlier version of the same presentation." : "Previews and downloads are those of this exact version. Changes detected before or after a request are rejected. PPT #1 remains a separate pre-meeting artifact."}</p>
          {deck?.source ? <p className="discovery-preview-caption" data-testid="master-presentation-v2-summary">
            Master Presentation {deck.source.product_version} ({deck.source.product_stage === "post_meeting" ? "post-meeting" : deck.source.product_stage}) · revision {deck.source.revision} · {deck.source.canonical_slide_count} slides of the canonical Borek deck, unchanged · {deck.source.appendix_slide_count} appendix slides from the confirmed findings of the first meeting.
          </p> : null}
          <div className="discovery-generation-card">
            <div role="status" aria-live="polite" aria-atomic="true"><strong>{statusText}</strong><p>{readyCount} of {slides.length} slide previews loaded</p></div>
            <button className="btn btn-secondary" type="button" disabled={!live || !accessToken || phase === "loading" || Boolean(downloading)} onClick={reload}>Reload deck</button>
          </div>
          <footer className="discovery-download-card">
            <strong>{deck ? `${product} loaded; previews load individually.` : `Downloads require a generated ${product}.`}</strong>
            <p>Download real editable PPTX or PDF files when advertised by the backend. Missing previews do not imply missing downloads.</p>
            <div className="discovery-version-actions">
              <button className="btn btn-secondary" type="button" disabled={!deck?.downloads.pptx || phase !== "ready" || Boolean(downloading)} onClick={() => void download("pptx")}>{downloading === "pptx" ? "Downloading PPTX..." : "Download PPTX"}</button>
              <button className="btn btn-secondary" type="button" disabled={!deck?.downloads.pdf || phase !== "ready" || Boolean(downloading)} onClick={() => void download("pdf")}>{downloading === "pdf" ? "Downloading PDF..." : "Download PDF"}</button>
            </div>
            <small className="discovery-version-label">{product} version: {deck?.presentationVersionId ?? "Not loaded"}{deck ? ` / Revision ${deck.versionNumber}` : ""}. {deck?.source ? `Source: approved Discovery ${deck.source.approved_discovery_version_id} · follows V1 version ${deck.source.base_presentation_version_id ?? "unknown"}.` : "Source lineage is not exposed by the deck API."}</small>
            {live && deck?.source && others.key === deckKey && others.versions.length ? <ul className="discovery-version-label" data-testid="other-presentation-versions">
              {others.versions.map((version) => <li key={version.versionId}>
                Other version · {versionLabel(version)}{" "}
                <button className="btn btn-secondary" type="button" onClick={() => void downloadOther(version, "pptx")}>PPTX</button>{" "}
                <button className="btn btn-secondary" type="button" onClick={() => void downloadOther(version, "pdf")}>PDF</button>
              </li>)}
            </ul> : null}
            {live && workflow?.documents.approved_discovery ? <small className="discovery-version-label">Latest approved Discovery: {workflow.documents.approved_discovery.version_id} (not confirmation of this deck&apos;s source).</small> : null}
          </footer>
        </div>
      </article>
    </div>
    {/* BAP-11: the saved documents, loaded independently of the deck above. */}
    {live && accessToken ? <PostMeetingDocuments opportunityId={opportunityId} accessToken={accessToken} master={master} clientName={companyName}
      ppt1={workflow?.documents.ppt1} ppt2={workflow?.documents.ppt2} /> : null}
    {live && master && deck ? <OwnerCheckpointPanel opportunityId={opportunityId} compact /> : null}
  </section>;
}
