"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/components/AuthProvider";
import { PostMeetingPhaseNav, usePostMeeting } from "@/components/PostMeetingShell";
import { blockerText, postMeetingError } from "@/lib/postMeeting";
import {
  ORIGIN_LABEL, SOURCE_LABEL, STALE_TEXT, STATE_LABEL, approvePostMeetingDiscovery, changedSections, downloadDiscovery2Docx, editableSections,
  generatePostMeetingDiscovery, loadPostMeetingDiscovery, savePostMeetingDiscovery,
  type Discovery2SectionKey, type PostMeetingDiscovery,
} from "@/lib/postMeetingDiscovery";
import { DocumentLanguageBadge, DocumentLanguageWarning, useDocumentLanguage } from "@/components/DocumentLanguageSelector";
import {
  documentLanguageErrorMessage, failedLookup, loadApprovedDiscovery1Language, lookupFrom, normalizeDocumentLanguage, otherLanguageReviewText,
  sourceLanguageGate, type SourceLanguageLookup,
} from "@/lib/documentLanguage";
import styles from "./post-meeting.module.css";

type Busy = null | "loading" | "generating" | "saving" | "approving" | "downloading";
type Edits = Record<Discovery2SectionKey, { id?: string; text: string }[]>;

const when = (value: string | null | undefined) => (value ? new Date(value).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "");

function saveBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = window.document.createElement("a");
    anchor.href = url; anchor.download = fileName; anchor.click();
  } finally { URL.revokeObjectURL(url); }
}

/** BAP-7: generate, review, edit, approve and download Discovery 2 before Master Presentation V2. */
export function PostMeetingDiscoveryPanel({ opportunityId }: { opportunityId: string }) {
  const { accessToken, previewMode } = useAuth();
  const { refreshWorkflow } = usePostMeeting();
  const live = Boolean(accessToken) && !previewMode;
  const root = `/opportunities/${encodeURIComponent(opportunityId)}`;
  const [data, setData] = useState<PostMeetingDiscovery | null>(null);
  const [edits, setEdits] = useState<Edits | null>(null);
  const [busy, setBusy] = useState<Busy>(live ? "loading" : null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  // BAP-12: Discovery 2 inherits the language of the approved Discovery 1.
  const selectedLanguage = useDocumentLanguage(opportunityId).language;
  // PR #14 review: fail-closed - while the lookup runs or after it failed, Discovery 2 waits.
  const [discovery1, setDiscovery1] = useState<SourceLanguageLookup>({ status: "loading" });
  const [languageCheck, setLanguageCheck] = useState(0);
  useEffect(() => {
    if (!accessToken || !live) return;
    const controller = new AbortController();
    setDiscovery1({ status: "loading" });
    void loadApprovedDiscovery1Language(accessToken, opportunityId, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setDiscovery1(lookupFrom(value)); })
      .catch((cause) => { if (!controller.signal.aborted) setDiscovery1(failedLookup(cause)); });
    return () => controller.abort();
  }, [accessToken, live, opportunityId, reload, languageCheck]);
  const languageGate = sourceLanguageGate(selectedLanguage, discovery1, { artifact: "Discovery 2", source: "Discovery 1" });

  const adopt = useCallback((next: PostMeetingDiscovery) => {
    setData(next);
    setEdits(next.current_version ? editableSections(next.document) : null);
  }, []);

  useEffect(() => {
    if (!accessToken || !live) return;
    const controller = new AbortController();
    setBusy("loading"); setError(null);
    void loadPostMeetingDiscovery(accessToken, opportunityId, controller.signal)
      .then((value) => { if (!controller.signal.aborted) { adopt(value); setBusy(null); } })
      .catch((cause) => { if (!controller.signal.aborted) { setError(postMeetingError(cause)); setBusy(null); } });
    return () => controller.abort();
  }, [accessToken, live, opportunityId, reload, adopt]);

  async function run(kind: Exclude<Busy, null | "loading">, action: (token: string) => Promise<void>) {
    if (!accessToken || busy) return;
    setBusy(kind); setError(null); setNotice(null);
    try { await action(accessToken); }
    catch (cause) { if (alive.current) setError(documentLanguageErrorMessage(cause) ?? postMeetingError(cause)); }
    finally { if (alive.current) setBusy(null); }
  }

  const document = data?.document ?? null;
  const current = data?.current_version ?? null;
  const state = data?.state ?? null;
  const changes = document && edits && current ? changedSections(document, edits) : [];
  const dirty = changes.length > 0;
  const readOnly = Boolean(data?.finalized) || busy !== null;
  const canApprove = Boolean(current && current.status === "draft" && state?.status === "draft" && !dirty && !data?.finalized);

  const generate = () => run("generating", async (token) => {
    if (languageGate.blocked) return;
    // Named explicitly: the server refuses before writing anything if Discovery 1 is in another language.
    const next = await generatePostMeetingDiscovery(token, opportunityId, discovery1.status === "verified" ? discovery1.language : selectedLanguage);
    if (!alive.current) return;
    adopt(next); refreshWorkflow();
    setNotice(`Discovery 2 version ${next.current_version?.version_number ?? ""} was generated. Review it, edit where needed and approve it.`);
  });
  const save = () => run("saving", async (token) => {
    if (!current || !edits) return;
    const next = await savePostMeetingDiscovery(token, opportunityId, current, changes);
    if (!alive.current) return;
    adopt(next);
    setNotice(current.status === "approved" ? `Saved as a new draft, version ${next.current_version?.version_number}. The approved version is unchanged.` : "Changes saved.");
  });
  const approve = () => run("approving", async (token) => {
    if (!current) return;
    const next = await approvePostMeetingDiscovery(token, opportunityId, current);
    if (!alive.current) return;
    adopt(next); refreshWorkflow();
    setNotice(`Discovery 2 version ${current.version_number} is approved. Master Presentation V2 is generated from it.`);
  });
  const download = (versionId: string, versionNumber: number) => run("downloading", async (token) => {
    saveBlob(await downloadDiscovery2Docx(token, opportunityId, { id: versionId }), `Discovery 2 - v${versionNumber}.docx`);
  });

  function setItem(key: Discovery2SectionKey, index: number, text: string) {
    setEdits((value) => value && { ...value, [key]: value[key].map((item, position) => (position === index ? { ...item, text } : item)) });
  }
  function removeItem(key: Discovery2SectionKey, index: number) {
    setEdits((value) => value && { ...value, [key]: value[key].filter((_item, position) => position !== index) });
  }
  function addItem(key: Discovery2SectionKey) {
    setEdits((value) => value && { ...value, [key]: [...value[key], { text: "" }] });
  }

  const documentLanguage = normalizeDocumentLanguage(document?.language);
  const otherLanguageIds = new Set(data?.language_check?.other_language_item_ids ?? []);
  const otherLanguageText = otherLanguageReviewText(otherLanguageIds.size, documentLanguage, "Discovery 2");
  const generationLabel = document?.generation?.mode === "live" ? "Generated with the AI model" : document?.generation ? "Generated by the deterministic test generator — no AI model was used" : null;

  return <section aria-labelledby="discovery2-title">
    <PostMeetingPhaseNav opportunityId={opportunityId} active="discovery2" />
    <div className={styles.card}>
      <header className={styles.discovery2Header}>
        <div>
          <h1 id="discovery2-title">Discovery 2</h1>
          <p>The Discovery after the first meeting: what Discovery 1 established, carried forward, and what the meeting added or changed. Master Presentation V2 is generated from the approved Discovery 2.</p>
        </div>
        {state ? <p className={styles.discovery2Status} data-testid="discovery2-status" data-state={state.status}>
          <strong>{STATE_LABEL[state.status]}</strong>
          {current ? <small>Version {current.version_number} · {current.status === "approved" ? `approved ${when(current.approved_at)}` : "draft"}</small> : null}
          {generationLabel ? <small data-testid="discovery2-mode">{generationLabel}</small> : null}
          {current ? <DocumentLanguageBadge language={documentLanguage} /> : discovery1.status === "verified" ? <DocumentLanguageBadge language={discovery1.language} prefix="Will be generated in" /> : null}
        </p> : null}
      </header>

      {!live ? <p className={styles.notice}>Discovery 2 is generated from the stored meeting information. Sign in to a live session to generate, edit and approve it.</p> : null}
      {error ? <div className={styles.error} role="alert" data-testid="discovery2-error">{error} <button className="btn btn-secondary" onClick={() => setReload((value) => value + 1)}>Reload</button></div> : null}
      {notice ? <p className={styles.notice} role="status">{notice}</p> : null}
      {live && languageGate.message && (!current || state?.status === "stale" || state?.status === "failed") ? <DocumentLanguageWarning>
        {languageGate.message}
        {languageGate.retry ? <> <button className="btn btn-secondary" type="button" data-testid="discovery2-language-retry" onClick={() => setLanguageCheck((value) => value + 1)}>Check again</button></> : null}
      </DocumentLanguageWarning> : null}
      {current && otherLanguageText ? <div data-testid="discovery2-language-check"><DocumentLanguageWarning>{otherLanguageText}</DocumentLanguageWarning></div> : null}
      {busy === "loading" ? <p role="status">Loading Discovery 2...</p> : null}
      {busy === "generating" ? <p role="status" className={styles.progressLine} data-testid="discovery2-progress">Generating Discovery 2 from Discovery 1, the transcript, your notes and the confirmed findings...</p> : null}
      {state?.error && state.status === "failed" ? <p className={styles.error} role="alert" data-testid="discovery2-failed">{state.error.message}</p> : null}
      {state?.stale_reasons.length ? <ul className={styles.blockers} data-testid="discovery2-stale">{state.stale_reasons.map((code) => <li key={code}>{STALE_TEXT[code] ?? code}</li>)}</ul> : null}

      {data?.input_blockers.length ? <div className={styles.blockers} data-testid="discovery2-inputs">
        <small>Before Discovery 2 can be generated:</small>
        <ul>{data.input_blockers.map((code) => <li key={code}>{blockerText(code)}</li>)}</ul>
        <Link className="btn btn-secondary" href={`${root}/meeting`}>Back to meeting input</Link>
      </div> : null}

      <div className={styles.actions} data-testid="discovery2-actions">
        {state && (!current || state.status === "stale" || state.status === "failed")
          ? <button className="btn btn-primary" data-testid="discovery2-generate" disabled={!live || !state.can_generate || busy !== null || languageGate.blocked} onClick={() => void generate()}>
            {busy === "generating" ? "Generating..." : current ? "Generate Discovery 2 again" : "Generate Discovery 2"}</button>
          : null}
        {current ? <>
          <button className="btn btn-secondary" data-testid="discovery2-save" disabled={!dirty || readOnly} onClick={() => void save()}>{busy === "saving" ? "Saving..." : "Save changes"}</button>
          {dirty ? <button className="btn btn-secondary" disabled={readOnly} onClick={() => document && setEdits(editableSections(document))}>Discard changes</button> : null}
          <button className="btn btn-primary" data-testid="discovery2-approve" disabled={!canApprove || busy !== null} onClick={() => void approve()}>{busy === "approving" ? "Approving..." : "Approve Discovery 2"}</button>
          <button className="btn btn-secondary" data-testid="discovery2-download" disabled={busy !== null} onClick={() => void download(current.id, current.version_number)}>Download Word (.docx)</button>
        </> : null}
        {data?.ready_for_v2 ? <Link className="btn btn-primary" data-testid="discovery2-continue" href={`${root}/post-meeting-presentation`}>Continue to Master Presentation V2</Link> : null}
      </div>
      {dirty ? <p><small>Unsaved changes. Save them before approving; {current?.status === "approved" ? "an approved version is never changed - your edit becomes a new draft version." : "the draft is updated in place."}</small></p> : null}
    </div>

    {document && current && edits ? <article className={styles.card} data-testid="discovery2-document">
      <h2>{document.title}</h2>
      {document.summary ? <div className={styles.discovery2Summary}>
        <p><strong>Before the meeting (Discovery 1):</strong> {document.summary.previous}</p>
        <p><strong>After the meeting:</strong> {document.summary.meeting}</p>
      </div> : null}
      {document.sections.map((section) => <section key={section.key} className={styles.discovery2Section} aria-labelledby={`d2-${section.key}`} data-testid={`discovery2-section-${section.key}`}>
        <h3 id={`d2-${section.key}`}>{section.title}</h3>
        <ol>
          {edits[section.key].map((entry, index) => {
            const item = entry.id ? section.items.find((candidate) => candidate.id === entry.id) : undefined;
            const origin = item?.origin ?? "owner";
            return <li key={entry.id ?? `new-${index}`} data-origin={origin}>
              <span className={`${styles.sourceBadge} ${origin === "discovery1" ? styles.discovery2Previous : origin === "owner" ? styles.sourceBadgeNotes : ""}`}>
                {ORIGIN_LABEL[origin]}{item && item.origin === "meeting" ? ` · ${SOURCE_LABEL[item.source]}${item.status === "updated" ? " · updates Discovery 1" : ""}` : ""}{item?.edited ? " · edited" : ""}
                {entry.id && otherLanguageIds.has(entry.id) ? " · other language - review" : ""}
              </span>
              <textarea aria-label={`${section.title} item ${index + 1}`} value={entry.text} rows={Math.min(6, Math.max(2, Math.ceil(entry.text.length / 90)))}
                readOnly={readOnly} maxLength={4000} onChange={(event) => setItem(section.key, index, event.target.value)} />
              {item?.evidence && item.evidence !== entry.text ? <blockquote className={styles.discovery2Evidence}><small>Evidence: “{item.evidence}”</small></blockquote> : null}
              <button type="button" className={styles.emailLink} disabled={readOnly} onClick={() => removeItem(section.key, index)}>Remove</button>
            </li>;
          })}
        </ol>
        {!edits[section.key].length ? <p><small>Nothing recorded.</small></p> : null}
        <button type="button" className="btn btn-secondary" disabled={readOnly} onClick={() => addItem(section.key)}>Add item</button>
      </section>)}
      {document.sources ? <footer className={styles.discovery2Sources} data-testid="discovery2-sources">
        <h3>Sources</h3>
        <ul>
          <li>Discovery 1 · version {document.sources.discovery1.version_number} (approved {when(document.sources.discovery1.approved_at)})</li>
          <li>Transcript · {document.sources.transcript.file_name ?? document.sources.transcript.transcript_id}</li>
          <li>Personal notes · {document.sources.personal_notes.updated_at ? `saved ${when(document.sources.personal_notes.updated_at)}` : "none"}</li>
          <li>Confirmed findings · {document.sources.meeting_review.confirmed_finding_count} confirmed {when(document.sources.meeting_review.confirmed_at)}</li>
          <li>Generated {when(document.generated_at)}</li>
        </ul>
      </footer> : null}
    </article> : null}

    {data?.versions.length ? <aside className={styles.card} aria-label="Discovery 2 versions" data-testid="discovery2-versions">
      <h2>Version history</h2>
      <ul className={styles.discovery2Versions}>
        {[...data.versions].reverse().map((version) => <li key={version.id}>
          <span>Version {version.version_number} · {version.status === "approved" ? `approved ${when(version.approved_at)}` : `draft · ${when(version.updated_at)}`}{version.generation_mode === "live" ? " · AI model" : " · test generator"}</span>
          <button type="button" className={styles.emailLink} disabled={busy !== null} onClick={() => void download(version.id, version.version_number)}>Download .docx</button>
        </li>)}
      </ul>
      <small>Approved versions never change. Master Presentation V2 uses the latest version once it is approved.</small>
    </aside> : null}
  </section>;
}
