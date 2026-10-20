"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { useAuth } from "@/components/AuthProvider";
import { WorkflowArtifactTabs } from "@/components/WorkflowArtifactTabs";
import { usePreviewJourney } from "@/components/PreviewJourneyProvider";
import { ApiRequestError } from "@/lib/api";
import {
  approveAnalysis,
  chapterGroups,
  editableSection,
  fieldLabel,
  generateAnalysis,
  isOpportunityAnalysis,
  loadDiscoveryPayload,
  pagePosition,
  parseDiscoveryAnalysis,
  saveAnalysisSection,
  stageProgress,
  viewOf,
  websiteResearchState,
  type AnalysisPage,
  type AnalysisView,
  type DiscoveryAnalysis,
  type KnownFact,
  sourceLink,
} from "@/lib/discoveryAnalysis";
import { sampleAnalysis } from "@/lib/discoveryAnalysisSample";
import { SHEET_H, SHEET_W, findOverflowingPages, printAnalysisPdf, renderAnalysisSheet } from "@/lib/discoveryAnalysisSheets";
import { DISCOVERY_PAGE_CATALOG } from "@/lib/discoveryFirst";
import { createDiscoveryWorkspaceFixture, createFixtureDiscoveryWorkspaceAdapter } from "@/lib/discoveryWorkspace";

type Obj = Record<string, unknown>;

interface DiscoveryAnalysisWorkspaceProps {
  opportunityId: string;
  /** Viewer for a paper stored before the rewrite (schema 1.0, fixed page set). */
  legacy: React.ReactNode;
}

const CHOICES: Record<string, readonly string[]> = {
  payback_type: ["quick_win", "strategic"],
  basis: ["industry_benchmark", "working_hypothesis"],
};
const STAGE_MARK: Record<string, string> = { ready: "Done", generating: "Running", failed: "Failed", waiting: "Waiting", skipped: "Not requested" };

function emptyPaper(opportunityId: string): DiscoveryAnalysis {
  return {
    schema_version: "2.0",
    opportunity_id: opportunityId,
    document_id: null,
    latest_approved_version_id: null,
    status: "not_generated",
    generated_at: null,
    language: "en",
    intake_context: { client_name: "", meeting_purpose: "" },
    generation: { mode: "fixture", specificity: "generic", research_mode: "user_context_only", optional_parts: {}, stages: [] },
    analysis: null,
    page_manifest: [],
  };
}

function SheetPreview({ page }: { page: AnalysisPage }) {
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
  return (
    <div ref={boxRef} className="discovery-sheet-frame" style={{ aspectRatio: `${SHEET_W} / ${SHEET_H}` }}>
      <div className="discovery-sheet-scaler" style={{ width: SHEET_W, height: SHEET_H, transform: `scale(${scale})` }} dangerouslySetInnerHTML={{ __html: renderAnalysisSheet(page, "/whitepaper") }} />
    </div>
  );
}

/** Edits one logical section. Ids are fixed; every text is a plain field with the stored value. */
function FieldEditor({ name, value, onChange }: { name: string; value: unknown; onChange: (next: unknown) => void }) {
  if (name === "id") return null;
  if (typeof value === "string") {
    if (CHOICES[name]) {
      return <label>{fieldLabel(name)}<select value={value} onChange={(event) => onChange(event.target.value)}>{CHOICES[name].map((choice) => <option key={choice} value={choice}>{fieldLabel(choice)}</option>)}</select></label>;
    }
    return <label>{fieldLabel(name)}<textarea value={value} rows={Math.min(8, Math.max(2, Math.ceil(value.length / 70)))} onChange={(event) => onChange(event.target.value)} /></label>;
  }
  if (Array.isArray(value)) {
    return <fieldset className="discovery-analysis-fieldset"><legend>{fieldLabel(name)}</legend>{value.map((item, index) => (
      <FieldEditor key={index} name={typeof item === "string" ? `${name} ${index + 1}` : `${index + 1}`} value={item} onChange={(next) => onChange(value.map((old, at) => (at === index ? next : old)))} />
    ))}</fieldset>;
  }
  if (value && typeof value === "object") {
    return <fieldset className="discovery-analysis-fieldset"><legend>{fieldLabel(name)}</legend>{Object.entries(value as Obj).map(([key, item]) => (
      <FieldEditor key={key} name={key} value={item} onChange={(next) => onChange({ ...(value as Obj), [key]: next })} />
    ))}</fieldset>;
  }
  return null;
}

export function DiscoveryAnalysisWorkspace({ opportunityId, legacy }: DiscoveryAnalysisWorkspaceProps) {
  const router = useRouter();
  const { accessToken, previewMode } = useAuth();
  const live = Boolean(accessToken) && !previewMode;
  const { getOpportunity, approveDiscovery } = usePreviewJourney();
  const preview = getOpportunity(opportunityId);
  const scope = `${opportunityId}:${live}:${accessToken ?? ""}`;
  const requestRef = useRef(0);
  const [view, setView] = useState<AnalysisView | null>(null);
  const [isLegacy, setIsLegacy] = useState(false);
  const [loadState, setLoadState] = useState<"loading" | "loaded" | "error">("loading");
  const [busy, setBusy] = useState<"generate" | "approve" | "save" | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editTarget, setEditTarget] = useState<string | null>(null);
  const [draft, setDraft] = useState<Obj | null>(null);
  const [overflowing, setOverflowing] = useState<string[]>([]);
  const [previewApproved, setPreviewApproved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);

  const paper = view?.paper ?? emptyPaper(opportunityId);
  const manifest = paper.page_manifest;
  const selected = manifest.find((page) => page.id === selectedId) ?? manifest[0] ?? null;
  const groups = useMemo(() => chapterGroups(manifest), [manifest]);
  const progress = stageProgress(paper.generation.stages);
  const approved = live ? view?.version?.status === "approved" : previewApproved || preview?.discovery.document_state === "approved";
  const research = (paper.analysis?.research ?? null) as { known_facts?: KnownFact[]; unknown_facts?: string[]; web_research?: { note?: string } } | null;
  const title = String(manifest[0]?.content.title ?? "AI opportunity analysis");
  const canApprove = paper.status === "ready" && !approved && overflowing.length === 0 && (!live || Boolean(view?.version));

  async function refresh(token: string, isCurrent: () => boolean): Promise<DiscoveryAnalysis | null> {
    const payload = await loadDiscoveryPayload(token, opportunityId);
    if (!isCurrent()) return null;
    if (payload !== null && !isOpportunityAnalysis(payload)) {
      setIsLegacy(true);
      return null;
    }
    const next = payload === null ? { paper: emptyPaper(opportunityId), version: null } : await viewOf(token, opportunityId, payload);
    if (!isCurrent()) return null;
    setIsLegacy(false);
    setView(next);
    setLoadState("loaded");
    return next.paper;
  }

  function fail(cause: unknown) {
    setError(cause instanceof ApiRequestError || cause instanceof Error ? cause.message : "The request failed.");
  }

  useEffect(() => {
    const request = ++requestRef.current;
    const isCurrent = () => request === requestRef.current;
    setError(null);
    setNotice(null);
    setEditTarget(null);
    setIsLegacy(false);
    if (!live || !accessToken) {
      // Without a server session the workspace shows the bundled sample analysis, read-only.
      setView({ paper: parseDiscoveryAnalysis(sampleAnalysis(opportunityId)), version: null });
      setLoadState("loaded");
      return;
    }
    setView(null);
    setLoadState("loading");
    refresh(accessToken, isCurrent).catch((cause) => {
      if (!isCurrent()) return;
      setLoadState("error");
      fail(cause);
    });
    return () => { requestRef.current += 1; };
  }, [scope]);

  // The server persists a snapshot after every stage; poll while a generation is running.
  useEffect(() => {
    if (!live || !accessToken || (paper.status !== "generating" && busy !== "generate")) return;
    const request = requestRef.current;
    const timer = setInterval(() => {
      refresh(accessToken, () => request === requestRef.current).catch(() => { /* the POST reports the failure */ });
    }, 1000);
    return () => clearInterval(timer);
  }, [scope, paper.status, busy]);

  // Master pages hide overflow. Measure the real layout so nothing is ever clipped unnoticed.
  useEffect(() => {
    let cancelled = false;
    if (paper.status !== "ready" || manifest.length === 0) {
      setOverflowing([]);
      return;
    }
    findOverflowingPages(manifest, "/whitepaper").then((ids) => { if (!cancelled) setOverflowing(ids); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [manifest, paper.status]);

  useEffect(() => { if (error) errorRef.current?.focus(); }, [error]);

  async function generate() {
    if (!live || !accessToken) return;
    const request = requestRef.current;
    setBusy("generate");
    setError(null);
    setNotice(null);
    setEditTarget(null);
    try {
      await generateAnalysis(accessToken, opportunityId, preview?.client.values);
    } catch (cause) {
      fail(cause);
    } finally {
      // Success or failure, the stored paper is the truth: stages, pages or the failed stage.
      try { await refresh(accessToken, () => request === requestRef.current); } catch { /* keep the request error */ }
      setBusy(null);
    }
  }

  async function approve() {
    if (!canApprove) return;
    setBusy("approve");
    setError(null);
    try {
      if (live && accessToken) {
        const version = await approveAnalysis(accessToken, opportunityId);
        await refresh(accessToken, () => true);
        setNotice(`Version ${version.version_number} approved. It can no longer be changed.`);
      } else {
        // Preview mode keeps its own journey state; the approval there is a local record only.
        const adapter = createFixtureDiscoveryWorkspaceAdapter(createDiscoveryWorkspaceFixture(opportunityId, DISCOVERY_PAGE_CATALOG.map(() => "ready")));
        approveDiscovery(await adapter.approve({ opportunity_id: opportunityId, version_id: "discovery-v1", expected_revision: 1 }));
        setPreviewApproved(true);
      }
      router.push(`/opportunities/${encodeURIComponent(opportunityId)}/presentations`);
    } catch (cause) {
      fail(cause);
    } finally {
      setBusy(null);
    }
  }

  function startEdit(target: string) {
    const section = editableSection(paper.analysis, target);
    if (!section) return;
    setEditTarget(target);
    setDraft(section.value);
    setError(null);
    setNotice(null);
  }

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!live || !accessToken || !editTarget || !draft || !paper.document_id) return;
    setBusy("save");
    setError(null);
    try {
      const payload = await saveAnalysisSection(accessToken, opportunityId, paper.document_id, editTarget, draft);
      setView(await viewOf(accessToken, opportunityId, payload));
      setEditTarget(null);
      setNotice(approved ? "Saved as a new draft. The approved version is unchanged." : "Saved. Every page that shows this section was updated.");
    } catch (cause) {
      fail(cause);
    } finally {
      setBusy(null);
    }
  }

  if (isLegacy) {
    return (
      <>
        <div className="discovery-generation-card discovery-analysis-legacy">
          <div role="status">
            <strong>Earlier Discovery format</strong>
            <p>This document was created before the AI Opportunity Analysis. It stays readable and approvable as it is.</p>
          </div>
          <button className="btn btn-secondary" type="button" disabled={busy !== null} onClick={() => {
            if (window.confirm("Generate a new analysis? It becomes a new draft. An approved version stays unchanged.")) void generate();
          }}>{busy === "generate" ? "Generating..." : "Generate new analysis"}</button>
        </div>
        {error ? <p className="client-information-error" role="alert">{error}</p> : null}
        {legacy}
      </>
    );
  }

  const editing = editTarget ? editableSection(paper.analysis, editTarget) : null;
  // BAP-10: how the website research went, shown above the analysis it was used for.
  const researchState = live && paper.status === "ready" ? websiteResearchState(paper) : null;
  const backgroundPage = manifest.find((page) => page.id.startsWith("company-background-"));
  const clientInformationHref = `/opportunities/${encodeURIComponent(opportunityId)}/client-information`;
  const retryResearch = () => {
    if (window.confirm("Analyse the website again? This generates a new draft analysis; an approved version stays unchanged.")) void generate();
  };
  const statusLine = loadState === "loading" ? "Loading Discovery from the server..."
    : paper.status === "generating" || busy === "generate" ? `Generating: ${progress.current?.label ?? "starting"} (${progress.done} of ${progress.total} steps done)`
    : paper.status === "failed" ? `Generation stopped at "${progress.failed?.label ?? "an earlier step"}". Nothing partial was kept; restart the generation.`
    : paper.status === "not_generated" ? "No analysis exists yet. Generation works with whatever client information is available."
    : `${manifest.length} pages · ${approved ? "approved and locked" : "draft"}`;

  return (
    <section className="workflow-artifact-workspace artifact-preview-workspace discovery-document-workspace">
      <WorkflowArtifactTabs opportunityId={opportunityId} active="discovery" />
      {paper.status === "ready"
        ? <progress className="discovery-progress" max={manifest.length} value={selected?.number ?? 0} aria-label={selected ? pagePosition(manifest, selected.id) : `${manifest.length} pages`} />
        : <progress className="discovery-progress" max={Math.max(progress.total, 1)} value={progress.done} aria-label={`${progress.done} of ${progress.total} generation steps done`} />}
      <div id="artifact-panel-discovery" role="tabpanel" aria-labelledby="artifact-tab-discovery">
        <h1 id="discovery-workspace-title" className="sr-only">AI Opportunity Analysis</h1>
        {error ? <p ref={errorRef} className="client-information-error" role="alert" tabIndex={-1}>{error}</p> : null}
        {notice ? <p className="client-information-notice" role="status" aria-live="polite">{notice}</p> : null}
        {overflowing.length ? (
          <p className="client-information-error" role="alert">
            Text does not fit on {overflowing.length === 1 ? "this page" : "these pages"}: {overflowing.map((id) => manifest.find((page) => page.id === id)).filter(Boolean).map((page) => `${page!.number} (${page!.nav_label})`).join(", ")}. Shorten the section before approving; nothing is cut off silently.
          </p>
        ) : null}
        <div className="discovery-generation-card">
          <div role="status" aria-live="polite">
            <strong>{live ? "AI Opportunity Analysis" : "Sample analysis (preview mode)"}</strong>
            <p data-testid="discovery-status">{live ? statusLine : `${manifest.length} pages · illustrative library content, no server session`}</p>
          </div>
          {live && paper.status === "not_generated" && loadState === "loaded" ? (
            <button className="btn btn-primary" type="button" disabled={busy !== null} onClick={() => void generate()}>{busy === "generate" ? "Generating..." : "Generate analysis"}</button>
          ) : live && (paper.status === "failed" || paper.status === "ready") ? (
            <button className="btn btn-secondary" type="button" disabled={busy !== null || Boolean(editTarget)} onClick={() => {
              if (paper.status === "failed" || window.confirm("Generate a new analysis? It becomes a new draft. An approved version stays unchanged.")) void generate();
            }}>{busy === "generate" ? "Generating..." : paper.status === "failed" ? "Restart generation" : "Regenerate"}</button>
          ) : null}
        </div>

        {researchState ? (
          <section
            className={`discovery-research-status is-${researchState.kind}`}
            aria-labelledby="discovery-research-status-title"
            data-testid="website-research-status"
            data-state={researchState.kind}
            role={researchState.kind === "failed" ? "alert" : "status"}
          >
            <h2 id="discovery-research-status-title">{researchState.title}</h2>
            {researchState.message ? <p>{researchState.message}</p> : null}
            {researchState.reasons.length ? <ul>{researchState.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : null}
            <div className="discovery-research-status-actions">
              {researchState.kind === "complete" && backgroundPage ? (
                <button type="button" className="btn btn-secondary" disabled={Boolean(editTarget)} onClick={() => { setSelectedId(backgroundPage.id); setNotice(null); }}>
                  View sources (page {backgroundPage.number})
                </button>
              ) : null}
              {researchState.canRetry ? (
                <button type="button" className="btn btn-secondary" disabled={busy !== null || Boolean(editTarget)} onClick={retryResearch}>
                  {busy === "generate" ? "Generating..." : "Retry website analysis"}
                </button>
              ) : null}
              {researchState.kind !== "complete" ? (
                <Link className="btn btn-secondary" href={clientInformationHref}>Add company information</Link>
              ) : null}
            </div>
          </section>
        ) : null}

        {paper.status !== "ready" ? (
          <div className="discovery-page-state" role="status">
            <strong>{paper.status === "failed" ? "Generation failed" : paper.status === "generating" || busy === "generate" ? "Generating the analysis" : "Not generated yet"}</strong>
            {paper.generation.stages.length ? (
              <ol className="discovery-analysis-stages">
                {paper.generation.stages.map((stage) => (
                  <li key={stage.key} className={`is-${stage.status}`}><span>{stage.label}</span><small>{STAGE_MARK[stage.status]}</small></li>
                ))}
              </ol>
            ) : <p>The analysis covers the opportunity overview, a deep dive per opportunity, processes outside the systems and the target picture. Its length follows the content.</p>}
          </div>
        ) : (
          <div className="workflow-artifact-grid">
            <aside className="workflow-page-list" aria-label="Analysis pages">
              <div className="discovery-list-heading"><span>Pages</span><span>{selected ? `${selected.number} / ${manifest.length}` : manifest.length}</span></div>
              {groups.map((group) => {
                // Only the chapter being read is expanded, so the list stays short at any page count.
                const open = group.pages.some((page) => page.id === selected?.id);
                return (
                <div key={group.key} className="discovery-analysis-group">
                  <button type="button" className="discovery-analysis-group-label" aria-expanded={open} disabled={Boolean(editTarget)} onClick={() => { setSelectedId(group.pages[0].id); setNotice(null); }}>
                    <span>{group.label}</span><small>{group.pages.length === 1 ? `p. ${group.pages[0].number}` : `p. ${group.pages[0].number}–${group.pages[group.pages.length - 1].number}`}</small>
                  </button>
                  <ol hidden={!open}>
                    {group.pages.map((page) => (
                      <li key={page.id} className={page.id === selected?.id ? "is-selected" : undefined}>
                        <button type="button" disabled={Boolean(editTarget)} onClick={() => { setSelectedId(page.id); setNotice(null); }} aria-current={page.id === selected?.id ? "page" : undefined}>
                          <span className="discovery-page-label">
                            <strong><span className="workflow-page-number">{String(page.number).padStart(2, "0")}</span>{page.nav_label}</strong>
                            {overflowing.includes(page.id) ? <small className="is-failed">Text does not fit</small> : null}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ol>
                </div>
                );
              })}
            </aside>

            <article className="workflow-preview-panel">
              {selected ? (
                <>
                  <div className="discovery-page-toolbar">
                    <p className="workflow-panel-label">{pagePosition(manifest, selected.id)} · {selected.nav_label}</p>
                    <div className="discovery-version-actions">
                      <button className="btn btn-secondary" type="button" disabled={selected.number === 1 || Boolean(editTarget)} onClick={() => setSelectedId(manifest[selected.number - 2].id)}>Previous</button>
                      <button className="btn btn-secondary" type="button" disabled={selected.number === manifest.length || Boolean(editTarget)} onClick={() => setSelectedId(manifest[selected.number].id)}>Next</button>
                    </div>
                  </div>
                  <div className="discovery-preview-content">
                    {editing && draft ? (
                      <form className="discovery-page-editor" onSubmit={(event) => void save(event)}>
                        <strong>{editing.label}</strong>
                        <p className="discovery-gate-copy">You edit the content once; every page that shows it follows. Lengths are checked against the page design when you save.</p>
                        {Object.entries(draft).map(([key, value]) => (
                          <FieldEditor key={key} name={key} value={value} onChange={(next) => setDraft({ ...draft, [key]: next })} />
                        ))}
                        <div>
                          <button className="btn btn-secondary" type="button" disabled={busy !== null} onClick={() => setEditTarget(null)}>Cancel</button>
                          <button className="btn btn-primary" type="submit" disabled={busy !== null}>{busy === "save" ? "Saving..." : "Save section"}</button>
                        </div>
                      </form>
                    ) : (
                      <>
                        <SheetPreview page={selected} />
                        {live && selected.edit_targets.length ? (
                          <div className="discovery-analysis-edit">
                            <span>Edit:</span>
                            {selected.edit_targets.map((target) => {
                              const section = editableSection(paper.analysis, target);
                              return section ? <button key={target} className="btn btn-secondary" type="button" disabled={busy !== null} onClick={() => startEdit(target)}>{section.label}</button> : null;
                            })}
                          </div>
                        ) : null}
                      </>
                    )}

                    {research ? (
                      <details className="discovery-analysis-research">
                        <summary>What this analysis knows about the client</summary>
                        <p>{research.web_research?.note}</p>
                        {research.known_facts?.length ? (
                          <dl>{research.known_facts.map((fact) => (
                            <React.Fragment key={`${fact.label}:${fact.value}`}><dt>{fact.label} <small>({fact.origin === "USER_INPUT" ? "entered by you" : "cited research"})</small></dt><dd>{fact.value}{fact.origin === "SOURCE_FACT" && sourceLink(fact.source_refs?.[0]) ? <> <a href={sourceLink(fact.source_refs?.[0])!} target="_blank" rel="noopener noreferrer nofollow">source</a></> : null}</dd></React.Fragment>
                          ))}</dl>
                        ) : <p>No client information was entered. The analysis is a generic discussion basis.</p>}
                        {research.unknown_facts?.length ? <p><strong>Unknown, not assumed:</strong> {research.unknown_facts.join(" · ")}</p> : null}
                      </details>
                    ) : null}

                    <footer className="discovery-download-card">
                      <strong>{approved ? "This version is approved and locked." : "Review the pages, then approve the analysis."}</strong>
                      <p>The PDF contains all {manifest.length} pages in the BOREK White Paper design. In the print dialog choose "Save as PDF".</p>
                      <div className="discovery-version-actions">
                        <button className="btn btn-secondary" type="button" onClick={() => printAnalysisPdf(manifest, title)}>Download PDF</button>
                        {approved ? (
                          <button className="btn btn-primary" type="button" onClick={() => router.push(`/opportunities/${encodeURIComponent(opportunityId)}/presentations`)}>Continue to presentation</button>
                        ) : (
                          <button className="btn btn-primary" type="button" disabled={!canApprove || busy !== null || Boolean(editTarget)} onClick={() => void approve()}>{busy === "approve" ? "Approving..." : "Approve analysis"}</button>
                        )}
                      </div>
                      <small className="discovery-version-label">
                        {live ? `Server version ${view?.version?.version_number ?? "not available"}` : "Preview sample"} · {approved ? "Approved and locked" : "Draft"}
                        {live && approved ? " · Editing or regenerating creates a new draft" : ""}
                      </small>
                    </footer>
                  </div>
                </>
              ) : null}
            </article>
          </div>
        )}
      </div>
    </section>
  );
}
