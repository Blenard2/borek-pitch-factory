"use client";

import React, { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { useAuth } from "@/components/AuthProvider";
import { WorkflowArtifactTabs } from "@/components/WorkflowArtifactTabs";
import { usePreviewJourney } from "@/components/PreviewJourneyProvider";
import { useDocumentLanguage } from "@/components/DocumentLanguageSelector";
import { ApiRequestError } from "@/lib/api";
import {
  approveLiveDiscovery,
  discoveryContentLabel,
  emptyLiveDiscovery,
  generateLiveDiscovery,
  loadLiveDiscovery,
} from "@/lib/liveDiscovery";
import {
  canApproveDiscovery,
  canDownloadDiscoveryPdf,
  continueDiscoveryGeneration,
  createFixtureDiscoveryWorkspaceAdapter,
  discoveryWorkspaceErrorMessage,
  isDiscoveryComplete,
  type DiscoveryContentValue,
  type DiscoveryWorkspacePage,
  type DiscoveryWorkspaceVersion,
} from "@/lib/discoveryWorkspace";
import { SHEET_H, SHEET_W, buildContext, printDiscoveryPdf, renderDiscoverySheet } from "@/lib/discoveryWhitePaper";

interface DiscoveryWorkspaceProps {
  initialVersion: DiscoveryWorkspaceVersion;
}

function DiscoveryContent({ value }: { value: DiscoveryContentValue }): React.ReactNode {
  if (value === null) return <span>Not supplied</span>;
  if (Array.isArray(value)) {
    return value.length ? <ul>{value.map((item, index) => <li key={index}><DiscoveryContent value={item} /></li>)}</ul> : <span>None supplied</span>;
  }
  if (typeof value === "object") {
    return <dl style={{ margin: "0 0 12px" }}>{Object.entries(value).map(([key, item]) => (
      <React.Fragment key={key}><dt><strong>{discoveryContentLabel(key)}</strong></dt><dd style={{ margin: "0 0 8px" }}><DiscoveryContent value={item} /></dd></React.Fragment>
    ))}</dl>;
  }
  return <span style={{ whiteSpace: "pre-wrap" }}>{String(value)}</span>;
}

function DiscoverySheetPreview({ version, page, client }: { version: DiscoveryWorkspaceVersion; page: DiscoveryWorkspacePage; client?: string }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(0.7);
  useEffect(() => {
    const node = boxRef.current;
    if (!node) return;
    const update = () => setScale(node.clientWidth / SHEET_W);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  const index = version.pages.indexOf(page);
  const html = renderDiscoverySheet(page, buildContext(version, client, "/whitepaper"), index + 1);
  return (
    <div ref={boxRef} className="discovery-sheet-frame" style={{ aspectRatio: `${SHEET_W} / ${SHEET_H}` }}>
      <div className="discovery-sheet-scaler" style={{ width: SHEET_W, height: SHEET_H, transform: `scale(${scale})` }} dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}

const STATUS_LABELS = {
  waiting: "Waiting",
  generating: "Generating",
  ready: "Ready",
  failed: "Failed",
} as const;

export function DiscoveryWorkspace({ initialVersion }: DiscoveryWorkspaceProps) {
  const router = useRouter();
  const { accessToken, previewMode } = useAuth();
  const live = Boolean(accessToken) && !previewMode;
  const { getOpportunity, updateDiscovery, approveDiscovery } = usePreviewJourney();
  const adapterRef = useRef(createFixtureDiscoveryWorkspaceAdapter(initialVersion));
  const [fixtureVersion, setVersion] = useState(initialVersion);
  const scope = `${initialVersion.opportunity_id}:${live}:${accessToken ?? ""}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const requestRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const [liveView, setLiveView] = useState<{
    scope: string;
    version: DiscoveryWorkspaceVersion;
    state: "loading" | "loaded" | "error";
  } | null>(null);
  const version = live
    ? (liveView?.scope === scope ? liveView.version : emptyLiveDiscovery(initialVersion.opportunity_id))
    : fixtureVersion;
  const liveState = liveView?.scope === scope ? liveView.state : "loading";
  const [selectedId, setSelectedId] = useState(initialVersion.pages[0].id);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(initialVersion.pages[0].title);
  const [body, setBody] = useState(initialVersion.pages[0].body);
  const [busy, setBusy] = useState(false);
  const [liveAction, setLiveAction] = useState<"load" | "generate" | "approve" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
  const selected = version.pages.find((page) => page.id === selectedId) ?? version.pages[0];
  const preview = getOpportunity(initialVersion.opportunity_id);
  const documentLanguage = useDocumentLanguage(initialVersion.opportunity_id).language;
  const persistedVersion = preview?.discovery;
  const readyCount = version.pages.filter((page) => page.state === "ready").length;
  const generatingPage = version.pages.find((page) => page.state === "generating");
  const failedCount = version.pages.filter((page) => page.state === "failed").length;
  const pageNumber = String(version.pages.indexOf(selected) + 1).padStart(2, "0");

  useEffect(() => {
    if (live) return;
    const loaded = persistedVersion ?? initialVersion;
    adapterRef.current = createFixtureDiscoveryWorkspaceAdapter(loaded);
    setVersion(loaded);
  }, [persistedVersion, initialVersion, live]);

  useEffect(() => {
    setEditing(false);
    setError(null);
    setNotice(null);
    setBusy(false);
    setLiveAction(null);
    if (live) void runLive("load");
    return () => {
      requestRef.current += 1;
      abortRef.current?.abort();
    };
  }, [scope]);

  useEffect(() => {
    if (!live || busy || liveState !== "loaded" || version.live?.status !== "generating") return;
    const timer = setTimeout(() => void runLive("load"), 1500);
    return () => clearTimeout(timer);
  }, [scope, busy, liveState, version]);

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  async function runLive(action: "load" | "generate" | "approve") {
    if (!live || !accessToken) return;
    const request = ++requestRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const current = () => request === requestRef.current && scopeRef.current === scope && !controller.signal.aborted;
    let receivedReadyPaper = false;
    const publish = (next: DiscoveryWorkspaceVersion) => {
      if (current()) {
        receivedReadyPaper ||= next.live?.status === "ready";
        setLiveView({ scope, version: next, state: "loaded" });
      }
    };
    setBusy(true);
    setLiveAction(action);
    setError(null);
    setNotice(null);
    try {
      const options = { signal: controller.signal, onPaper: publish };
      const next = action === "generate"
        ? await generateLiveDiscovery(accessToken, initialVersion.opportunity_id, preview?.client.values, options, documentLanguage)
        : action === "approve"
          ? await approveLiveDiscovery(accessToken, initialVersion.opportunity_id)
          : await loadLiveDiscovery(accessToken, initialVersion.opportunity_id, options);
      if (!current()) return;
      publish(next);
      if (action === "approve") {
        setNotice(`${next.version_id} approved by the server.`);
        router.push(`/opportunities/${encodeURIComponent(next.opportunity_id)}/presentations`);
      }
    } catch (liveError) {
      if (!current()) return;
      // A failed POST can still leave completed pages and a failed page persisted by the server.
      if (action === "generate" && !receivedReadyPaper) {
        try {
          publish(await loadLiveDiscovery(accessToken, initialVersion.opportunity_id, { signal: controller.signal, onPaper: publish }));
        } catch { /* Preserve any already received pages and the original request error. */ }
      }
      if (!current()) return;
      setLiveView((previous) => ({
        scope,
        version: previous?.scope === scope ? previous.version : emptyLiveDiscovery(initialVersion.opportunity_id),
        state: "error",
      }));
      setError(liveError instanceof ApiRequestError ? liveError.message : discoveryWorkspaceErrorMessage(liveError));
    } finally {
      if (current()) {
        setBusy(false);
        setLiveAction(null);
      }
    }
  }

  function selectPage(page: DiscoveryWorkspacePage) {
    setSelectedId(page.id);
    setTitle(page.title);
    setBody(page.body);
    setEditing(false);
    if (!live) setError(null);
    setNotice(null);
  }

  async function savePage(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (live) return;
    setBusy(true);
    setError(null);
    try {
      const saved = await adapterRef.current.savePage({
        opportunity_id: version.opportunity_id,
        version_id: version.version_id,
        page_id: selected.id,
        expected_revision: version.revision,
        title,
        body,
      });
      setVersion(saved);
      updateDiscovery(saved);
      setEditing(false);
      setNotice(`${selected.label} saved in ${saved.version_id}.`);
    } catch (saveError) {
      setError(discoveryWorkspaceErrorMessage(saveError));
    } finally {
      setBusy(false);
    }
  }

  async function retryPage() {
    if (live) return;
    setBusy(true);
    setError(null);
    try {
      const retried = await adapterRef.current.retryPage({
        opportunity_id: version.opportunity_id,
        version_id: version.version_id,
        page_id: selected.id,
        expected_revision: version.revision,
      });
      const nextPage = retried.pages.find((page) => page.id === selected.id)!;
      setVersion(retried);
      updateDiscovery(retried);
      setTitle(nextPage.title);
      setBody(nextPage.body);
      setNotice(`${nextPage.label} is ready. Other completed pages were preserved.`);
    } catch (retryError) {
      setError(discoveryWorkspaceErrorMessage(retryError));
    } finally {
      setBusy(false);
    }
  }

  async function advanceGeneration() {
    if (live) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const advanced = isDiscoveryComplete(version)
        ? await adapterRef.current.advanceGeneration({
            opportunity_id: version.opportunity_id,
            version_id: version.version_id,
            expected_revision: version.revision,
          })
        : await continueDiscoveryGeneration(adapterRef.current, version);
      setVersion(advanced);
      updateDiscovery(advanced);
    } catch (advanceError) {
      setError(advanceError instanceof ApiRequestError ? advanceError.message : discoveryWorkspaceErrorMessage(advanceError));
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (live) {
      if (canApproveDiscovery(version)) await runLive("approve");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const approved = await adapterRef.current.approve({
        opportunity_id: version.opportunity_id,
        version_id: version.version_id,
        expected_revision: version.revision,
      });
      setVersion(approved);
      approveDiscovery(approved);
      setEditing(false);
      setNotice(`${approved.version_id} approved and locked.`);
      router.push(`/opportunities/${encodeURIComponent(approved.opportunity_id)}/presentations`);
    } catch (approvalError) {
      setError(approvalError instanceof ApiRequestError ? approvalError.message : discoveryWorkspaceErrorMessage(approvalError));
    } finally {
      setBusy(false);
    }
  }

  async function createSuccessor() {
    if (live) return;
    setBusy(true);
    setError(null);
    try {
      const successor = await adapterRef.current.createSuccessor({
        opportunity_id: version.opportunity_id,
        approved_version_id: version.version_id,
      });
      setVersion(successor);
      updateDiscovery(successor);
      setNotice(`${successor.version_id} created. The approved version remains unchanged.`);
    } catch (successorError) {
      setError(discoveryWorkspaceErrorMessage(successorError));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="workflow-artifact-workspace artifact-preview-workspace discovery-document-workspace">
      <WorkflowArtifactTabs opportunityId={version.opportunity_id} active="discovery" />
      <progress className="discovery-progress" max={version.pages.length} value={readyCount} aria-label={`${readyCount} of ${version.pages.length} Discovery pages ready`} />
      <div id="artifact-panel-discovery" role="tabpanel" aria-labelledby="artifact-tab-discovery">
      <h1 id="discovery-workspace-title" className="sr-only">Discovery Document</h1>
      {error ? <p ref={errorRef} className="client-information-error" role="alert" tabIndex={-1}>{error}</p> : null}
      {notice ? <p className="client-information-notice" role="status" aria-live="polite">{notice}</p> : null}
      {live ? <div className="discovery-generation-card">
        <p role="status">{liveAction === "generate" ? "Generating Discovery on the server. Completed pages appear as they are persisted." : liveState === "loading" ? "Loading Discovery from the server..." : liveState === "error" ? "Discovery could not be fully refreshed. Any pages shown are the last server response; refresh before continuing." : version.live?.status === "not_generated" ? "No Discovery paper is available yet. Generate it explicitly to begin." : "Live Discovery content and status come from the server."}</p>
        <button className="btn btn-secondary" type="button" disabled={busy} onClick={() => void runLive("load")}>Refresh Discovery</button>
      </div> : null}

      <div className="workflow-artifact-grid">
        <aside className="workflow-page-list" aria-label="Discovery pages">
          <div className="discovery-list-heading"><span>Pages</span><span>Status</span></div>
          <ol>
            {version.pages.map((page, index) => (
              <li key={page.id} className={page.id === selected.id ? "is-selected" : undefined}>
                <button type="button" disabled={(!live && busy) || editing} onClick={() => selectPage(page)} aria-current={page.id === selected.id ? "page" : undefined}>
                  <span className={`discovery-thumbnail is-${page.state}`} aria-hidden="true"><i /><i /><i /><i /></span>
                  <span className="discovery-page-label">
                    <strong><span className="workflow-page-number">{String(index + 1).padStart(2, "0")}</span>{page.label}</strong>
                    <small className={`is-${page.state}`}>{STATUS_LABELS[page.state]}</small>
                  </span>
                  <span className={`discovery-status-mark is-${page.state}`} aria-hidden="true">
                    {page.state === "ready" ? <svg viewBox="0 0 16 16" fill="none"><path d="m4 8 3 3 5-6" stroke="currentColor" strokeWidth="1.5" /></svg> : page.state === "failed" ? "!" : <i />}
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </aside>

        <article className="workflow-preview-panel">
          <div className="discovery-page-toolbar">
            <p className="workflow-panel-label">Page {pageNumber} · {selected.label}</p>
            {selected.state === "ready" && version.document_state === "draft" && !editing ? (
              <button className="btn btn-secondary" type="button" disabled={busy || live} onClick={() => { setTitle(selected.title); setBody(selected.body); setEditing(true); }}>Edit page</button>
            ) : null}
          </div>
          {live && selected.state === "ready" && version.document_state === "draft" ? <p className="discovery-gate-copy">Live editing is unavailable in this text editor: the API accepts schema-specific content objects and preserves page titles. No fixture edits will be applied.</p> : null}

          <div className="discovery-preview-content">
          {editing && !live ? (
            <form className="discovery-page-editor" onSubmit={(event) => void savePage(event)}>
              <label>Page title<input value={title} onChange={(event) => setTitle(event.target.value)} required /></label>
              <label>Page content<textarea value={body} onChange={(event) => setBody(event.target.value)} rows={12} required /></label>
              <div>
                <button className="btn btn-secondary" type="button" disabled={busy} onClick={() => { setEditing(false); setTitle(selected.title); setBody(selected.body); }}>Cancel</button>
                <button className="btn btn-primary" type="submit" disabled={busy}>{busy ? "Saving..." : "Save page"}</button>
              </div>
            </form>
          ) : selected.state === "ready" ? (
            <DiscoverySheetPreview version={version} page={selected} client={live ? version.live?.client_name : preview?.client.values.company_name} />
          ) : selected.state === "failed" ? (
            <div className="discovery-page-state is-failed" role="alert">
              <strong>Page generation failed</strong>
              <p>{selected.failure_message}</p>
              <button className="btn btn-secondary" type="button" disabled={busy || live} onClick={() => void retryPage()}>{busy && !live ? "Retrying..." : "Retry this page"}</button>
            </div>
          ) : (
            <div className={`discovery-page-state is-${selected.state}`} role="status">
              <strong>{STATUS_LABELS[selected.state]}</strong>
              <p>{selected.state === "generating" ? "This page is being prepared. Ready pages remain available for review." : live ? "The server has not supplied content for this page." : "This page will start after the preceding generation work completes."}</p>
            </div>
          )}
          <p className="discovery-preview-caption">{live ? "Completed pages remain readable while server progress is refreshed." : "Generated pages become available here immediately."}</p>
          {selected.state === "ready" && selected.source_references.length > 0 ? (
                <div className="discovery-source-list" aria-label="Source references">
                  {selected.source_references.map((reference) => (
                    <details key={reference.id}><summary>{reference.label}</summary><p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{reference.detail}</p></details>
                  ))}
                </div>
          ) : null}

          <div className="discovery-generation-card">
            <div role="status" aria-live="polite">
              <strong>{generatingPage ? `Generating page ${String(version.pages.indexOf(generatingPage) + 1).padStart(2, "0")}` : failedCount ? "Generation needs attention" : isDiscoveryComplete(version) ? "Discovery pages ready" : "Waiting for generation"}</strong>
              <p>{generatingPage?.label ?? `${readyCount} of ${version.pages.length} pages ready`}{failedCount > 0 ? live ? ` · ${failedCount} failed. The backend has no page retry or resume endpoint.` : ` · ${failedCount} failed; select a failed page to retry.` : ""}</p>
            </div>
            {live && version.live?.status === "not_generated" ? (
              <button className="btn btn-secondary" type="button" disabled={busy || liveState !== "loaded"} onClick={() => void runLive("generate")}>{liveAction === "generate" ? "Generating..." : "Generate Discovery"}</button>
            ) : live && version.live?.status === "failed" ? (
              <p className="discovery-gate-copy">Use "Generate new analysis" above to replace this failed document.</p>
            ) : !live && !isDiscoveryComplete(version) && version.document_state === "draft" ? (
              <button className="btn btn-secondary" type="button" disabled={busy || editing} onClick={() => void advanceGeneration()}>
                {busy ? "Generating..." : "Continue generation"}
              </button>
            ) : null}
          </div>
          <footer className="discovery-download-card">
            <strong>{isDiscoveryComplete(version) ? "All pages are ready for review." : live ? "Completed pages are available for review." : "Download will be available when all pages are ready."}</strong>
            <p>This document was stored in the earlier Discovery format. The PDF uses the BOREK White Paper design: cover, contents, the stored pages and a closing page. In the print dialog choose "Save as PDF".</p>
            <div className="discovery-version-actions">
            {!live && version.document_state === "draft" && isDiscoveryComplete(version) && !canDownloadDiscoveryPdf(version) ? (
              <button className="btn btn-secondary" type="button" disabled={busy || editing} onClick={() => void advanceGeneration()}>Prepare PDF preview manifest</button>
            ) : null}
            <button className="btn btn-secondary" type="button" disabled={!isDiscoveryComplete(version)} onClick={() => printDiscoveryPdf(version, live ? version.live?.client_name : preview?.client.values.company_name)}>Download PDF</button>
            {version.document_state === "approved" ? (
              <button className="btn btn-primary" type="button" disabled={busy || live} onClick={() => void createSuccessor()}>Create successor draft</button>
            ) : (
              <button className="btn btn-primary" type="button" disabled={busy || editing || (live && liveState !== "loaded") || !canApproveDiscovery(version)} onClick={() => void approve()}>Approve {version.version_id}</button>
            )}
          {live && version.document_state === "approved" ? <p className="discovery-gate-copy">Successor creation is unavailable: there is no clone/successor endpoint. Whole-paper regeneration is not a successor edit.</p> : null}
          {!canApproveDiscovery(version) && version.document_state === "draft" ? <p className="discovery-gate-copy">{live ? "Approval requires every stored page to be ready and matching server draft version metadata." : "Approval remains blocked until every page and its exact-version PDF are ready."}</p> : null}
            </div>
            <small className="discovery-version-label">{version.version_id} · {live ? `Server version ${version.live?.version_number ?? "not available"}` : `Revision ${version.revision}`} · {version.document_state === "approved" ? "Approved and locked" : "Draft"}</small>
          </footer>
          </div>
        </article>
      </div>
      </div>
    </section>
  );
}
