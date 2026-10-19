"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/components/AuthProvider";
import { FollowUpAdvancedReview } from "@/components/FollowUpAdvancedReview";
import { FollowUpReviewDialog } from "@/components/FollowUpReviewDialog";
import { PostMeetingPhaseNav, usePostMeeting } from "@/components/PostMeetingShell";
import { isMasterJourney } from "@/lib/discoveryFirst";
import { loadPostMeetingWorkflow, postMeetingError } from "@/lib/postMeeting";
import {
  ATTACHMENT_ARTIFACT_LABEL, EMAIL_LENGTHS, EMAIL_LENGTH_LABEL, EMAIL_REVIEW_CHECKS, EMAIL_STATE_LABEL, advancedReviewUi, approvalFor, attachmentMismatch, canPreparePostMeetingEmail, clipboardText,
  confirmPostMeetingEmail, contentWordCount, downloadEmailAttachment, emailError, emailExportable, emailState,
  exportPostMeetingEmail, formatFileSize, generatePostMeetingEmail, isEmailConflict, isEmailHasEdits, isStaticsMissing,
  loadFollowupStatics, loadPostMeetingEmail, openReviewFlags, personLabel, saveFollowupStatics, savePostMeetingEmail, staticsFormFrom,
  unsavedLengths, validateStaticsForm, workingCopy, writerLabel,
  type EmailAttachment, type EmailLength, type EmailWorkingCopy, type FollowupStaticsForm, type PostMeetingEmail,
} from "@/lib/postMeetingEmail";
import { downloadPostMeetingPresentation, loadExistingPostMeetingPresentation, type PostMeetingPresentation } from "@/lib/postMeetingPresentation";
import styles from "./post-meeting.module.css";

type Busy = null | "loading" | "generating" | "saving" | "confirming" | "exporting" | "statics";

function saveBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = window.document.createElement("a");
    anchor.href = url; anchor.download = fileName; anchor.click();
  } finally { URL.revokeObjectURL(url); }
}

/** Earlier opportunities with a standalone PPT #2: the current deck, as before. */
function StandalonePresentationCard({ opportunityId, token, reviewHref }: { opportunityId: string; token: string | null; reviewHref: string }) {
  const [deck, setDeck] = useState<PostMeetingPresentation | null>(null);
  const [phase, setPhase] = useState<"loading" | "ready" | "none" | "failed">("loading");
  const [message, setMessage] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  useEffect(() => {
    if (!token) { setPhase("none"); return; }
    const controller = new AbortController();
    void loadExistingPostMeetingPresentation(token, opportunityId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setDeck(result.deck); setPhase(result.deck ? "ready" : "none");
    }).catch((cause) => { if (!controller.signal.aborted) { setMessage(postMeetingError(cause)); setPhase("failed"); } });
    return () => controller.abort();
  }, [token, opportunityId]);
  async function download() {
    if (!token || !deck?.downloads.pptx || downloading) return;
    setDownloading(true); setMessage(null);
    try { saveBlob(await downloadPostMeetingPresentation(token, opportunityId, deck, "pptx"), `ppt-2-${opportunityId}.pptx`); }
    catch (cause) { setMessage(postMeetingError(cause)); }
    finally { setDownloading(false); }
  }
  return <div className={styles.packageDocument}>
    <strong>{deck?.name || "Updated presentation"}</strong>
    <small>{deck ? `${deck.slides.length} slides · PPT #2 · Version ${deck.versionNumber}` : phase === "loading" ? "Loading presentation..." : phase === "failed" ? "Presentation unavailable" : "No presentation generated yet"}</small>
    <div className={styles.packageDocumentActions}>
      <Link className="btn btn-secondary" href={reviewHref}>Review</Link>
      <button className="btn btn-primary" type="button" disabled={!deck?.downloads.pptx || downloading} onClick={() => void download()}>{downloading ? "Downloading..." : "Download"}</button>
    </div>
    {message ? <p className={styles.packageError} role="alert">{message}</p> : null}
  </div>;
}

const dateTime = (value: string | null) => (value ? new Date(value).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "");
// The original on-page review card instead of the Review & confirm dialog (docs/followup-email-review-ui.md).
const ADVANCED_REVIEW = advancedReviewUi();

export function FollowUpDraftPanel({ opportunityId }: { opportunityId: string }) {
  const { accessToken, previewMode } = useAuth();
  const { workflow, companyName, contactPerson } = usePostMeeting();
  const live = Boolean(accessToken) && !previewMode;
  const root = `/opportunities/${encodeURIComponent(opportunityId)}`;
  const master = isMasterJourney(workflow?.documents);
  const eligible = canPreparePostMeetingEmail(workflow);

  const [draft, setDraft] = useState<PostMeetingEmail | null>(null);
  const [copy, setCopy] = useState<EmailWorkingCopy | null>(null);
  const [length, setLength] = useState<EmailLength>("medium");
  const [busy, setBusy] = useState<Busy>(live ? "loading" : null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);
  // The Review & confirm dialog: closed, open for approval, or showing a confirmed draft again.
  const [review, setReview] = useState<null | "approve" | "record">(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [checks, setChecks] = useState<string[]>([]);
  // The review flags the owner ticked, one by one. A new or changed draft starts with none.
  const [acknowledged, setAcknowledged] = useState<string[]>([]);
  const [statics, setStatics] = useState<unknown>(null);
  const [form, setForm] = useState<FollowupStaticsForm | null>(null);
  const [editStatics, setEditStatics] = useState(false);
  const [staticsError, setStaticsError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const lock = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const dirty = unsavedLengths(draft, copy);
  const state = emailState(draft, copy);
  const exportable = emailExportable(draft) && dirty.length === 0;
  const working = busy !== null;

  /**
   * A loaded or saved draft replaces the saved state. ``keep`` names the lengths whose typed text
   * stays in the editor; every other length shows what is saved, including another editor's change.
   */
  const adopt = useCallback((next: PostMeetingEmail | null, keep: EmailLength[] | null = null) => {
    setDraft(next);
    setCopy((current) => {
      if (!next) return null;
      const saved = workingCopy(next);
      if (!keep || !current) return saved;
      for (const name of keep) saved[name] = current[name];
      return saved;
    });
    setChecks([]);
    setAcknowledged([]);
    if (next?.selectedLength && !keep) setLength(next.selectedLength);
  }, []);

  useEffect(() => {
    if (!accessToken || !live) return;
    const controller = new AbortController();
    setBusy("loading"); setError(null);
    void Promise.all([
      loadPostMeetingEmail(accessToken, opportunityId, controller.signal),
      loadFollowupStatics(accessToken, opportunityId, controller.signal),
    ]).then(([email, stored]) => {
      if (controller.signal.aborted) return;
      adopt(email); setStatics(stored);
      setForm(staticsFormFrom(stored, { companyName, contactPerson }));
      setEditStatics(stored === null);
      setLoaded(true); setConflict(false); setBusy(null);
    }).catch((cause) => { if (!controller.signal.aborted) { setError(emailError(cause)); setBusy(null); } });
    return () => controller.abort();
  }, [accessToken, live, opportunityId, reload, adopt, companyName, contactPerson]);

  useEffect(() => {
    if (!dirty.length) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty.length]);

  /** One request at a time; a 409 from another editor opens the reload decision instead of an error. */
  async function run(kind: Exclude<Busy, null | "loading">, action: (token: string) => Promise<void>) {
    if (!accessToken || !live || lock.current) return;
    lock.current = true; setBusy(kind); setError(null); setNotice(null);
    try { await action(accessToken); }
    catch (cause) {
      if (!alive.current) return;
      if (isEmailConflict(cause)) setConflict(true);
      else if (isEmailHasEdits(cause)) setConfirmRegenerate(true);
      else { if (isStaticsMissing(cause)) setEditStatics(true); setError(emailError(cause)); }
    } finally { lock.current = false; if (alive.current) setBusy(null); }
  }

  const generate = (overwrite: boolean) => run("generating", async (token) => {
    const latest = await loadPostMeetingWorkflow(token, opportunityId);
    if (!canPreparePostMeetingEmail(latest)) throw new Error("The documents are no longer finalized. Return to owner review before preparing an email.");
    const next = await generatePostMeetingEmail(token, opportunityId, overwrite);
    if (!alive.current) return;
    adopt(next); setConfirmRegenerate(false); setConflict(false);
    setNotice("A new draft was generated from the finalized presentation package.");
  });

  function requestGenerate() {
    // Saved edits, unsaved text or a confirmation would be replaced: ask first.
    const hasWork = Boolean(draft) && (dirty.length > 0 || draft!.status === "confirmed" || EMAIL_LENGTHS.some((name) => draft!.lengths[name].edited));
    if (hasWork) setConfirmRegenerate(true); else void generate(false);
  }

  const save = () => run("saving", async (token) => {
    if (!draft || !copy) return;
    const lengths = Object.fromEntries(dirty.map((name) => [name, copy[name]]));
    const saved = await savePostMeetingEmail(token, opportunityId, draft, { selectedLength: length, lengths });
    if (alive.current) { adopt(saved); setNotice("Changes saved."); }
  });

  const toggleAttachment = (attachment: EmailAttachment) => run("saving", async (token) => {
    if (!draft) return;
    const saved = await savePostMeetingEmail(token, opportunityId, draft, { attachments: { [attachment.key]: !attachment.selected } });
    if (!alive.current) return;
    // The API keeps generated text true to the selection; a text the owner edited is theirs and stays.
    const rewritten = EMAIL_LENGTHS.filter((name) => saved.lengths[name].body !== draft.lengths[name].body);
    const attached = saved.attachments.some((item) => item.selected);
    adopt(saved, dirty);
    if (rewritten.length) setNotice(`The sentence about the attached presentation was ${attached ? "added to" : "removed from"} the ${rewritten.map((name) => EMAIL_LENGTH_LABEL[name].toLowerCase()).join(", ")} ${rewritten.length === 1 ? "text" : "texts"}. Texts you edited were not changed.`);
  });

  const confirm = () => run("confirming", async (token) => {
    if (!draft) return;
    const confirmed = await confirmPostMeetingEmail(token, opportunityId, draft, length, checks, acknowledged);
    if (alive.current) { adopt(confirmed); setNotice("Draft confirmed. Nothing was sent."); }
  });

  /**
   * The approval given in the review dialog. It is sent only from the dialog's own button and
   * carries what the owner ticked there: each acknowledged flag, and the six checks if confirmed.
   */
  const approve = (acknowledgedFlags: string[], checksConfirmed: boolean) => run("confirming", async (token) => {
    if (!draft) return;
    setReviewError(null);
    const approval = approvalFor(draft, acknowledgedFlags, checksConfirmed);
    try {
      const confirmed = await confirmPostMeetingEmail(token, opportunityId, draft, length, approval.checks, approval.acknowledgedFlags);
      if (alive.current) { adopt(confirmed); setReview(null); setNotice("Email confirmed. Nothing was sent."); }
    } catch (cause) {
      if (!alive.current) return;
      // Changed elsewhere: the decision is made on the page. Anything else is shown in the dialog.
      if (isEmailConflict(cause)) { setReview(null); throw cause; }
      setReviewError(emailError(cause));
    }
  });

  const exportEmail = () => run("exporting", async (token) => {
    if (!draft) return;
    saveBlob(await exportPostMeetingEmail(token, opportunityId, draft), `${(form?.projectName || "follow-up").replace(/[^\w .()-]+/g, "-")} - follow-up email.eml`);
    if (alive.current) setNotice("The email file was downloaded. Open it in your mail program to send it yourself.");
  });

  const download = (attachment: EmailAttachment) => run("exporting", async (token) => {
    saveBlob(await downloadEmailAttachment(token, attachment), attachment.fileName);
  });

  async function copyText() {
    if (!draft) return;
    try { await navigator.clipboard.writeText(clipboardText(draft)); setNotice("Subject and message copied."); setError(null); }
    catch (cause) { setError(emailError(cause)); }
  }

  const saveStatics = () => run("statics", async (token) => {
    if (!form) return;
    const problem = validateStaticsForm(form);
    setStaticsError(problem);
    if (problem) return;
    const stored = await saveFollowupStatics(token, opportunityId, form, statics);
    if (!alive.current) return;
    setStatics(stored); setForm(staticsFormFrom(stored, { companyName, contactPerson })); setEditStatics(false);
    const refreshed = await loadPostMeetingEmail(token, opportunityId);
    if (!alive.current) return;
    adopt(refreshed, dirty);
    setNotice(refreshed ? "Recipient and sender saved. Greeting and signature of the existing drafts are text: regenerate to apply new names." : "Recipient and sender saved.");
  });

  /** Conflict decision: take the saved version, or keep the typed text on top of it and save again. */
  const resolveConflict = (keepMine: boolean) => run("saving", async (token) => {
    const latest = await loadPostMeetingEmail(token, opportunityId);
    if (!alive.current) return;
    adopt(latest, keepMine && latest !== null ? dirty : null); setConflict(false);
    setNotice(keepMine ? "The saved version was loaded underneath your text. Review it, then save again." : "The saved version was loaded.");
  });

  const text = copy?.[length];
  const words = text ? contentWordCount(text.body) : 0;
  const limit = draft?.wordLimits[length] ?? 0;
  const overLimit = Boolean(draft) && words > limit;
  const allChecked = EMAIL_REVIEW_CHECKS.every(([key]) => checks.includes(key));
  const needsReview = master || Boolean(draft?.source);
  const openFlags = openReviewFlags(draft, acknowledged);
  const selectedFiles = draft?.attachments.filter((item) => item.selected) ?? [];
  const blockedFile = selectedFiles.find((item) => !item.available);
  const canConfirm = Boolean(draft) && !working && dirty.length === 0 && !overLimit && !conflict && draft!.sourceStatus !== "changed"
    && !blockedFile && (!needsReview || (allChecked && openFlags.length === 0)) && !exportable;
  // The dialog reviews the saved text; what blocks the approval itself is shown inside it.
  const canReview = Boolean(draft) && !working && dirty.length === 0 && !overLimit && !conflict && !exportable;
  const reviewLength = exportable && draft?.selectedLength ? draft.selectedLength : length;
  const mismatch = attachmentMismatch(draft);
  const set = (patch: Partial<FollowupStaticsForm>) => setForm((current) => (current ? { ...current, ...patch } : current));
  const pill = { none: "Not prepared", unsaved: "Unsaved", saved: "Editable", confirmed: "Confirmed" }[state];

  // Until the workflow is known the page cannot tell a Master Presentation from an earlier
  // standalone PPT #2, so it shows neither journey's navigation, documents or gates.
  if (live && !workflow) {
    return <section aria-labelledby="follow-up-title" aria-busy="true">
      <header className={styles.heading}><span className={styles.eyebrow}>Post-meeting</span><h1 id="follow-up-title">Review follow-up package</h1><p>{companyName}</p></header>
      <p className={styles.notice} role="status">Loading the follow-up package...</p>
    </section>;
  }

  return <section aria-labelledby="follow-up-title" data-testid="follow-up-email">
    <header className={styles.heading}><span className={styles.eyebrow}>Post-meeting</span><h1 id="follow-up-title">Review follow-up package</h1><p>{companyName} · Presentation updated from the first meeting</p></header>
    <PostMeetingPhaseNav opportunityId={opportunityId} active="review" />
    {!live ? <p className={styles.notice} role="status">Preview mode: email drafts are stored by the backend and are not available without a signed-in session.</p> : null}
    {error ? <p className={styles.error} role="alert" data-testid="email-error">{error}</p> : null}
    {notice ? <p className={styles.notice} role="status" data-testid="email-notice">{notice}</p> : null}
    {conflict ? <div className={`${styles.notice} ${styles.emailDecision}`} role="alert" data-testid="email-conflict">
      <strong>This draft was changed elsewhere after you loaded it.</strong>
      <p>Nothing of yours was saved, and nothing was overwritten. Choose which text to continue with.</p>
      <div className={styles.actions}>
        <button className="btn btn-primary" disabled={working} onClick={() => void resolveConflict(false)}>Load the saved version</button>
        <button className="btn btn-secondary" disabled={working} onClick={() => void resolveConflict(true)}>Keep my text and review</button>
      </div>
    </div> : null}
    {confirmRegenerate ? <div className={`${styles.notice} ${styles.emailDecision}`} role="alertdialog" aria-label="Regenerate the email draft" data-testid="email-regenerate-warning">
      <strong>Regenerating replaces all three lengths.</strong>
      <p>Your edits{draft?.status === "confirmed" ? " and the confirmation" : ""} will be replaced by a new draft from the finalized package. This cannot be undone.</p>
      <div className={styles.actions}>
        <button className="btn btn-primary" disabled={working} onClick={() => void generate(true)}>Replace with a new draft</button>
        <button className="btn btn-secondary" disabled={working} onClick={() => setConfirmRegenerate(false)}>Keep my draft</button>
      </div>
    </div> : null}

    <div className={`${styles.columns} ${styles.followUpColumns}`}>
      <div className={styles.stack}>
        <article className={`${styles.card} ${styles.email}`} aria-busy={working}>
          <div className={styles.emailHeader}>
            <span className={styles.eyebrow}>Follow-up email</span>
            <span className={`${styles.emailPill} ${state === "confirmed" ? styles.emailPillDone : state === "unsaved" ? styles.emailPillWarn : ""}`} data-testid="email-state-pill">{pill}</span>
          </div>

          <dl className={styles.emailFields}>
            <div><dt>To</dt><dd data-testid="email-to">{draft?.recipients ? draft.recipients.to.map(personLabel).join(", ") : form?.recipientEmail ? `${[form.recipientFirstName, form.recipientLastName].filter(Boolean).join(" ")} <${form.recipientEmail}>`.trim() : "No recipient entered yet"}{companyName ? ` · ${companyName}` : ""}</dd></div>
            {draft?.recipients?.cc.length ? <div><dt>Cc</dt><dd>{draft.recipients.cc.map(personLabel).join(", ")}</dd></div> : null}
            <div><dt>From</dt><dd data-testid="email-from">{draft?.recipients ? `${draft.recipients.sender.name} <${draft.recipients.sender.email}>` : form?.senderEmail ? `${form.senderName} <${form.senderEmail}>` : "No sender entered yet"}
              {live && loaded && !editStatics ? <button type="button" className={styles.emailLink} disabled={working} onClick={() => setEditStatics(true)}>Edit recipient and sender</button> : null}</dd></div>
          </dl>

          {editStatics && form ? <fieldset className={styles.emailStatics} disabled={working} data-testid="email-statics">
            <legend>Recipient and sender</legend>
            <p><small>Entered by you and used for the greeting, the signature and the exported file. Nothing here is guessed.</small></p>
            <div className={styles.emailStaticsGrid}>
              <label>Project name<input value={form.projectName} onChange={(event) => set({ projectName: event.target.value })} /></label>
              <label>Client (short name)<input value={form.clientShort} onChange={(event) => set({ clientShort: event.target.value })} /></label>
              <label>Greeting<select value={form.style} onChange={(event) => set({ style: event.target.value === "formal" ? "formal" : "informal" })}><option value="informal">Informal — Hi first name</option><option value="formal">Formal — Dear salutation last name</option></select></label>
              <label>Recipient email<input type="email" value={form.recipientEmail} onChange={(event) => set({ recipientEmail: event.target.value })} /></label>
              <label>Salutation<input value={form.recipientSalutation} placeholder="Ms / Mr / Dr" onChange={(event) => set({ recipientSalutation: event.target.value })} /></label>
              <label>Recipient first name<input value={form.recipientFirstName} onChange={(event) => set({ recipientFirstName: event.target.value })} /></label>
              <label>Recipient last name<input value={form.recipientLastName} onChange={(event) => set({ recipientLastName: event.target.value })} /></label>
              <label>Sender name<input value={form.senderName} onChange={(event) => set({ senderName: event.target.value })} /></label>
              <label>Sender role<input value={form.senderRole} onChange={(event) => set({ senderRole: event.target.value })} /></label>
              <label>Sender email<input type="email" value={form.senderEmail} onChange={(event) => set({ senderEmail: event.target.value })} /></label>
            </div>
            {staticsError ? <p className={styles.error} role="alert">{staticsError}</p> : null}
            <div className={styles.actions}>
              <button type="button" className="btn btn-primary" onClick={() => void saveStatics()}>{busy === "statics" ? "Saving..." : "Save recipient and sender"}</button>
              {statics ? <button type="button" className="btn btn-secondary" onClick={() => { setForm(staticsFormFrom(statics, { companyName, contactPerson })); setEditStatics(false); setStaticsError(null); }}>Cancel</button> : null}
            </div>
          </fieldset> : null}

          {draft && copy && text ? <>
            <div className={styles.emailLengths} role="tablist" aria-label="Draft length">
              {EMAIL_LENGTHS.map((name) => <button key={name} type="button" role="tab" aria-selected={name === length} data-testid={`email-length-${name}`}
                className={name === length ? styles.emailLengthActive : undefined} onClick={() => setLength(name)}>
                <strong>{EMAIL_LENGTH_LABEL[name]}</strong>
                <small>{contentWordCount(copy[name].body)} / {draft.wordLimits[name]} words{dirty.includes(name) ? " · unsaved" : draft.lengths[name].edited ? " · edited" : ""}</small>
              </button>)}
            </div>
            <label className={styles.emailSubject} htmlFor="follow-up-subject"><span>Subject</span>
              <input id="follow-up-subject" value={text.subject} maxLength={200} disabled={working}
                onChange={(event) => setCopy({ ...copy, [length]: { ...text, subject: event.target.value } })} /></label>
            <label htmlFor="follow-up-body" className={styles.emailBody}><span className="sr-only">Message</span>
              <textarea id="follow-up-body" value={text.body} disabled={working}
                onChange={(event) => setCopy({ ...copy, [length]: { ...text, body: event.target.value } })} /></label>
            <div className={styles.emailFooter}>
              <small className={overLimit ? styles.packageError : undefined} data-testid="email-words">{words} of {limit} content words{overLimit ? " — shorten this draft or use a longer one" : ""}</small>
              <span className={state === "unsaved" ? styles.emailUnsaved : styles.emailSaved} data-testid="email-save-state" role="status">
                {state === "unsaved" ? `Unsaved changes in ${dirty.map((name) => EMAIL_LENGTH_LABEL[name]).join(", ")}` : state === "confirmed" ? "✓ Confirmed · not sent" : `✓ ${EMAIL_STATE_LABEL.saved}`}
              </span>
            </div>
            <div className={styles.actions}>
              <button className={dirty.length ? "btn btn-primary" : "btn btn-secondary"} data-testid="email-save" disabled={working || !dirty.length || overLimit || conflict} onClick={() => void save()}>{busy === "saving" ? "Saving..." : "Save changes"}</button>
              {dirty.length ? <button className="btn btn-secondary" disabled={working} onClick={() => setCopy(workingCopy(draft))}>Discard unsaved changes</button> : null}
              <button className="btn btn-secondary" data-testid="email-regenerate" disabled={working || !eligible} onClick={requestGenerate}>{busy === "generating" ? "Generating..." : "Regenerate from the finalized package"}</button>
            </div>
            <small>Draft revision {draft.revision} · saved {dateTime(draft.updatedAt)}{draft.status === "confirmed" ? ` · confirmed ${dateTime(draft.confirmedAt)}` : ""}</small>
            {writerLabel(draft.writer) ? <small data-testid="email-writer" data-writer={draft.writer?.mode}>{writerLabel(draft.writer)}{draft.lengths[length].edited ? " This length has since been edited by you." : ""}</small> : null}
          </> : <div className={styles.emailEmpty}>
            <p>{busy === "loading" ? "Loading the saved email draft..." : eligible ? "No follow-up email has been prepared yet. It is written from the finalized presentation package: the confirmed meeting findings and your recipient and sender details." : "The follow-up email is written from the finalized presentation package."}</p>
            {eligible ? <div className={styles.actions}><button className="btn btn-primary" data-testid="email-generate" disabled={!live || !loaded || working || editStatics} onClick={() => void generate(false)}>{busy === "generating" ? "Generating..." : "Prepare email draft"}</button>
              {editStatics ? <small>Save the recipient and sender first.</small> : null}</div>
              : <p><Link href={`${root}/${master ? "post-meeting-presentation" : "review"}`}>{master ? "Review and finalize Master Presentation V2" : "Review and finalize the current documents"}</Link> before preparing the email.</p>}
            {!loaded && busy !== "loading" && live ? <button className="btn btn-secondary" onClick={() => setReload((value) => value + 1)}>Retry loading</button> : null}
          </div>}
        </article>

        {draft && ADVANCED_REVIEW ? <FollowUpAdvancedReview draft={draft} length={length} exportable={exportable} working={working} confirming={busy === "confirming"} dirty={dirty}
          checks={checks} setChecks={setChecks} acknowledged={acknowledged} setAcknowledged={setAcknowledged} openFlags={openFlags} needsReview={needsReview}
          selectedFiles={selectedFiles} blockedFile={blockedFile} canConfirm={canConfirm} onConfirm={() => void confirm()} /> : null}
      </div>

      <aside className={`${styles.card} ${styles.packageSidebar}`} aria-label="Follow-up package">
        <header>
          <h3 className={styles.packageLabel}>Updated presentation</h3>
          {!master && !draft?.source ? <StandalonePresentationCard opportunityId={opportunityId} token={live ? accessToken : null} reviewHref={`${root}/post-meeting-presentation`} /> : <div className={styles.packageDocument} data-testid="email-presentation">
            <strong>{draft?.source ? (draft.attachments.find((item) => item.artifact === "master_presentation_v2")?.fileName.replace(/\.(pptx|pdf)$/, "") ?? "Master Presentation V2") : master ? "Master Presentation V2" : "Updated presentation"}</strong>
            <small>{draft?.source ? `Master Presentation V2 · version ${draft.source.versionNumber} · reviewed and finalized` : eligible ? "Finalized · prepare the email to pin this version" : "Not finalized yet"}</small>
            <div className={styles.packageDocumentActions}>
              <Link className="btn btn-secondary" href={`${root}/${master ? "post-meeting-presentation" : "review"}`}>Open in Deck Center</Link>
              {draft?.attachments.filter((item) => item.artifact === "master_presentation_v2").map((item) => <button key={item.key} className="btn btn-secondary" type="button" disabled={!item.available || working} onClick={() => void download(item)}>{item.format.toUpperCase()}</button>)}
            </div>
            {draft?.source ? <small>Downloads are the reviewed version {draft.source.versionNumber}, never a newer one.</small> : null}
          </div>}
        </header>
        <section className={styles.packageChanges}>
          <h3 className={styles.packageLabel}>What the email is based on</h3>
          <p>{draft?.source
            ? `${draft.source.confirmedFindings ?? 0} findings you confirmed for this presentation version${draft.source.excludedFindings ? `; ${draft.source.excludedFindings} excluded ${draft.source.excludedFindings === 1 ? "finding is" : "findings are"} not used` : ""}. ${draft.source.extractionMode === "live" ? "Findings were extracted by the live AI analysis." : "Findings were extracted by the built-in demo extraction, not by a live AI analysis."}`
            : master ? "The findings you confirmed for the finalized Master Presentation V2." : "The meeting transcript of this opportunity."}</p>
        </section>
        <section className={styles.packageAttachments}>
          <h3 className={styles.packageLabel}>Email attachments</h3>
          {draft?.attachments.length ? <ul aria-label="Approved email attachments" data-testid="email-attachments">
            {draft.attachments.map((item) => <li key={item.key} className={styles.emailAttachment} data-artifact={item.artifact}>
              <label className={styles.check}><input type="checkbox" checked={item.selected} disabled={working || conflict} data-testid={`email-attach-${item.key}`} onChange={() => void toggleAttachment(item)} />
                <span>{item.fileName}<small>{ATTACHMENT_ARTIFACT_LABEL[item.artifact]} · {item.available ? `${item.format.toUpperCase()} · ${formatFileSize(item.sizeBytes)} · approved version ${item.versionNumber ?? ""}` : "Not available — this file cannot be attached"}</small></span></label>
              {item.available ? <button type="button" className={styles.emailLink} disabled={working} onClick={() => void download(item)}>Download</button> : null}
            </li>)}
          </ul> : <p>{draft ? "This draft has no approved presentation version to attach." : "Available after the email draft is prepared."}</p>}
          {draft?.attachments.length ? <small>Selected files are packed into the exported email file. A file that is not selected is not attached.</small> : null}
          {mismatch.length ? <p className={styles.error} role="status" data-testid="email-attachment-mismatch">No file is selected, but your edited {mismatch.map((name) => EMAIL_LENGTH_LABEL[name].toLowerCase()).join(", ")} {mismatch.length === 1 ? "text still says" : "texts still say"} that something is attached. Select a file or change that sentence: it cannot be approved like this.</p> : null}
        </section>
        <footer className={styles.packageExport} data-testid="email-next-action">
          <span className={styles.packageLabel}>{exportable ? "Email ready" : ADVANCED_REVIEW ? "Email export" : "Next step"}</span>
          <h2 data-testid="email-export-title">{exportable ? "Ready to export" : ADVANCED_REVIEW ? "Not ready to export" : !draft ? "Prepare the email" : dirty.length ? "Save your changes" : "Review & confirm"}</h2>
          <p>{exportable
            ? `The confirmed ${draft?.selectedLength ? EMAIL_LENGTH_LABEL[draft.selectedLength].toLowerCase() : ""} draft${selectedFiles.length ? ` with ${selectedFiles.length} attached file${selectedFiles.length > 1 ? "s" : ""}` : " without attachments"}, as an unsent email file for your mail program.`
            : ADVANCED_REVIEW ? (draft ? "Save, review and confirm the draft. Only a confirmed draft can be exported." : "Prepare, review and confirm the email first.")
            : !draft ? "Prepare the email draft first. You review and confirm it here before it can be exported."
            : dirty.length ? "The review is of the saved text. Save or discard your changes, then review."
            : overLimit ? "This draft is over its word limit. Shorten it or use a longer draft."
            : `Read the ${EMAIL_LENGTH_LABEL[length].toLowerCase()} email once more with its recipient, attachments and the points to check, then approve it. Only a confirmed email can be exported.`}</p>
          {exportable || ADVANCED_REVIEW ? <>
            <button className="btn btn-primary" data-testid="email-export" disabled={!exportable || working} onClick={() => void exportEmail()}>{busy === "exporting" ? "Preparing..." : "Download email file (.eml)"}</button>
            <button className="btn btn-secondary" data-testid="email-copy" disabled={!exportable || working} onClick={() => void copyText()}>Copy subject and message</button>
            {exportable && !ADVANCED_REVIEW ? <button type="button" className={styles.emailLink} data-testid="email-review-record" disabled={working} onClick={() => { setReviewError(null); setReview("record"); }}>View what you confirmed</button> : null}
          </> : <button className="btn btn-primary" data-testid="email-review-open" disabled={!canReview} onClick={() => { setReviewError(null); setReview("approve"); }}>Review &amp; confirm</button>}
          <small id="email-export-note">Nothing is sent from this application. You send the email yourself after your own check.</small>
        </footer>
      </aside>
    </div>
    {draft && review && !ADVANCED_REVIEW ? <FollowUpReviewDialog draft={draft} length={reviewLength} companyName={companyName} busy={busy === "confirming"}
      readOnly={review === "record"} error={reviewError} onApprove={(acknowledgedFlags, checksConfirmed) => void approve(acknowledgedFlags, checksConfirmed)} onClose={() => setReview(null)} /> : null}
  </section>;
}
