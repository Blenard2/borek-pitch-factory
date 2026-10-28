"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/components/AuthProvider";
import { getAuthOwnerId } from "@/lib/authSession";
import { PostMeetingPhaseNav, usePostMeeting } from "@/components/PostMeetingShell";
import { generateMeetingExtraction, listAvailableUseCases, saveSelectedUseCases, uploadTranscript, type AvailableUseCase } from "@/lib/api";
import { approvedDiscovery2Language, loadPostMeetingDiscovery, needsDiscovery2 } from "@/lib/postMeetingDiscovery";
import { DocumentLanguageWarning, useDocumentLanguage } from "@/components/DocumentLanguageSelector";
import { documentLanguageErrorMessage, documentLanguageLabel, failedLookup, sourceLanguageGate, type SourceLanguageLookup } from "@/lib/documentLanguage";
import { isMasterJourney } from "@/lib/discoveryFirst";
import { awaitRunningMasterV2, generateAndAwaitMasterV2, loadMasterV2Status, masterV2Error, masterV2StageText, type MasterV2Status } from "@/lib/masterPresentationV2";
import { generateAndAwaitPostMeetingPresentation } from "@/lib/ppt2Generation";
import {
  blockerText, confirmMeetingReview, excludedFromConfirmation, EXTRACTION_FIELDS, FINDING_SOURCE_LABEL, findingsState, isStaleReviewError,
  loadPostMeetingReview, MeetingNotesConflict, mergedSpeakerText, postMeetingError, prepareMeetingEvidence, readTranscriptChoice,
  restoreTranscriptSelection, reviewHeadline, savePersonalNotesChecked, saveTranscriptChoice, staleReasonText, validateTranscript, workflowCompleted,
  type ExtractionCategory, type PersonalNotes, type PostMeetingReview,
} from "@/lib/postMeeting";
import styles from "./post-meeting.module.css";

type Excluded = Partial<Record<ExtractionCategory, string[]>>;

const when = (value: string | null) => value ? new Date(value).toLocaleString() : "";
const sameExcluded = (a: Excluded, b: Excluded) => EXTRACTION_FIELDS.every(([key]) =>
  [...(a[key] ?? [])].sort().join("\n") === [...(b[key] ?? [])].sort().join("\n"));

export function MeetingEvidencePanel({ opportunityId }: { opportunityId: string }) {
  const { accessToken, previewMode } = useAuth();
  const { workflow, refreshWorkflow, companyName } = usePostMeeting();
  const router = useRouter();
  const live = Boolean(accessToken) && !previewMode;
  const [review, setReview] = useState<PostMeetingReview | null>(null);
  const [notes, setNotes] = useState("");
  const [transcriptId, setTranscriptId] = useState("");
  const [excluded, setExcluded] = useState<Excluded>({});
  const [previewFile, setPreviewFile] = useState<File | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [conflictingNotes, setConflictingNotes] = useState<PersonalNotes | null>(null);
  const [available, setAvailable] = useState<AvailableUseCase[] | null>(null);
  const [useCaseDraft, setUseCaseDraft] = useState<string[] | null>(null);
  const [v2, setV2] = useState<MasterV2Status | null>(null);
  const [v2Stage, setV2Stage] = useState<string | null>(null);
  const [v2Error, setV2Error] = useState<string | null>(null);
  // BAP-12: V2 is written in the language of the approved Discovery 2.
  const selectedLanguage = useDocumentLanguage(opportunityId).language;
  // PR #14 review: fail-closed - V2 waits until the approved Discovery 2's language is verified.
  const [discovery2Language, setDiscovery2Language] = useState<SourceLanguageLookup>({ status: "none" });
  const [languageCheck, setLanguageCheck] = useState(0);
  // Which meeting input is in front. Both stay mounted, so typed notes survive switching.
  const [inputTab, setInputTab] = useState<"transcript" | "notes">("transcript");
  // The readiness checklist is always shown on desktop. At 960px and below it is collapsed behind
  // a toggle, so the meeting inputs are close to the top of the page.
  const [detailsOpen, setDetailsOpen] = useState(false);
  const loaded = useRef(false);
  const operation = useRef<AbortController | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  useEffect(() => () => operation.current?.abort(), []);

  /** Shows what the server holds. Typed notes and the transcript choice survive a refresh. */
  const apply = useCallback((value: PostMeetingReview, initial = false) => {
    setReview(value);
    // Earlier exclusions stay unticked when a confirmation goes out of date; they count once confirmed again.
    setExcluded(excludedFromConfirmation(value));
    // The owner's choice wins; after a refresh it is read back, and a newer upload is never
    // replaced by the older transcript the findings were made from.
    setTranscriptId((current) => {
      if (!initial && value.transcripts.some((item) => item.id === current)) return current;
      return restoreTranscriptSelection(value.transcripts, readTranscriptChoice(getAuthOwnerId(), opportunityId));
    });
    if (initial) setNotes(value.personal_notes.text ?? "");
  }, [opportunityId]);

  /** A transcript the owner picked or uploaded becomes the selection, also after a refresh. */
  const selectTranscript = useCallback((id: string) => {
    setTranscriptId(id);
    saveTranscriptChoice(getAuthOwnerId(), opportunityId, id);
  }, [opportunityId]);

  useEffect(() => {
    if (!live || !accessToken || loaded.current) return;
    const controller = new AbortController();
    setError(null);
    void loadPostMeetingReview(accessToken, opportunityId, controller.signal).then((value) => {
      if (controller.signal.aborted) return;
      loaded.current = true;
      apply(value, true);
    }).catch((cause) => { if (!controller.signal.aborted) setError(postMeetingError(cause)); });
    return () => controller.abort();
  }, [accessToken, apply, live, opportunityId, reload]);

  const baseline: PersonalNotes = { text: review?.personal_notes.text ?? null, updated_at: review?.personal_notes.updated_at ?? null };
  const notesDirty = Boolean(review) && notes.trim() !== (baseline.text ?? "");
  const finalized = Boolean(review?.finalized ?? workflow?.finalization);
  // A generation that is running on the server locks the inputs, whichever page or tab started it.
  const v2Running = v2?.state === "generating";
  const editingDisabled = Boolean(busy) || v2Running || finalized || (!previewMode && (!live || !review));
  const transcript = review?.transcripts.find((item) => item.id === transcriptId);
  const state = review ? findingsState(review, transcriptId, notesDirty) : "no-transcript";
  const storedExcluded = review && review.confirmation.status === "current" ? excludedFromConfirmation(review) : {};
  const selectionChanged = review?.confirmation.status === "current" && !sameExcluded(excluded, storedExcluded);
  const confirmedCurrent = review?.confirmation.status === "current" && !selectionChanged && state === "current";
  const root = `/opportunities/${encodeURIComponent(opportunityId)}`;
  // A pitch whose pre-meeting deck is not a Master Presentation still uses the earlier PPT #2 flow.
  const legacyJourney = Boolean(review) && review!.master_presentation.status !== "ready" && !isMasterJourney(workflow?.documents);

  // What exists of Master Presentation V2 is read from the server after every change of the review.
  const reviewKey = review ? `${review.review_fingerprint}:${review.confirmation.status}:${review.confirmation.confirmed_at}` : "";
  useEffect(() => {
    if (!live || !accessToken || !reviewKey) return;
    const controller = new AbortController();
    void loadMasterV2Status(accessToken, opportunityId, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setV2(value); })
      .catch((cause) => { if (!controller.signal.aborted) setV2Error(masterV2Error(cause)); });
    return () => controller.abort();
  }, [accessToken, live, opportunityId, reviewKey]);

  // Typed notes exist only in this tab until they are saved.
  useEffect(() => {
    if (!notesDirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [notesDirty]);

  async function run(label: string, action: (token: string, signal: AbortSignal) => Promise<void>) {
    if (!accessToken || !live || operation.current) return;
    const controller = new AbortController();
    operation.current = controller; setBusy(label); setError(null); setSaved(null);
    try { await action(accessToken, controller.signal); }
    catch (cause) {
      if (!controller.signal.aborted) {
        if (cause instanceof MeetingNotesConflict) setConflictingNotes(cause.savedNotes);
        setError(postMeetingError(cause));
      }
    } finally {
      if (operation.current === controller) operation.current = null;
      if (!controller.signal.aborted) { setBusy(""); refreshWorkflow(); }
    }
  }

  const refresh = async (token: string, signal: AbortSignal) => {
    const value = await loadPostMeetingReview(token, opportunityId, signal);
    if (!signal.aborted) apply(value);
    return value;
  };

  async function onTranscript(file: File) {
    if (editingDisabled) return;
    const invalid = validateTranscript(file);
    if (invalid) { setError(invalid); return; }
    if (previewMode) { setPreviewFile(file); setError(null); return; }
    await run("Uploading transcript", async (token, signal) => {
      const response = await uploadTranscript(token, opportunityId, file);
      signal.throwIfAborted();
      await refresh(token, signal);
      selectTranscript(response.transcript.id);
      setSaved(`${file.name} uploaded and selected.`);
    });
  }

  async function saveNotes() {
    if (!review || !notesDirty) return;
    await run("Saving notes", async (token, signal) => {
      const stored = await savePersonalNotesChecked(token, opportunityId, notes, baseline, signal);
      setNotes(stored.text ?? "");
      await refresh(token, signal);
      setSaved("Notes saved.");
    });
  }

  async function analyse() {
    if (!review || !transcriptId || notesDirty) return;
    // Exactly the transcript selected on screen is analysed, never another one.
    const selected = transcriptId;
    await run("Analysing meeting", async (token, signal) => {
      await generateMeetingExtraction(token, opportunityId, selected);
      signal.throwIfAborted();
      const analysedReview = await refresh(token, signal);
      setSaved(analysedReview.extraction.item_count === 0 ? "Meeting analysed. No findings were found." : "Meeting analysed. Review the findings below.");
    });
  }

  async function confirm() {
    if (!review || state !== "current") return;
    await run("Confirming findings", async (token, signal) => {
      try {
        apply(await confirmMeetingReview(token, opportunityId, review, excluded, signal));
      } catch (cause) {
        if (!isStaleReviewError(cause)) throw cause;
        // Nothing was confirmed. Show the sources as they are now, so the owner reviews them first.
        await refresh(token, signal);
        throw new Error("A source changed while you were reviewing, so nothing was confirmed. The review below is now up to date: check it and confirm again.");
      }
      setSaved("Meeting information confirmed.");
    });
  }

  async function openUseCases() {
    if (available || !accessToken || !live) return;
    try { setAvailable((await listAvailableUseCases(accessToken, opportunityId)).use_cases); }
    catch (cause) { setError(postMeetingError(cause)); }
  }

  async function saveUseCases() {
    if (!useCaseDraft) return;
    await run("Saving use cases", async (token, signal) => {
      await saveSelectedUseCases(token, opportunityId, useCaseDraft);
      signal.throwIfAborted();
      await refresh(token, signal);
      setUseCaseDraft(null);
      setSaved("Use cases saved.");
    });
  }

  // A V2 generation that is already running when this page opens - after a reload, or after the
  // user left and came back - is followed until the server reports ready or failed. This only
  // reads the job and the status; the generation started here is followed by generateV2 itself.
  const v2JobId = v2?.job?.job_id ?? null;
  useEffect(() => {
    if (!live || !accessToken || !v2 || v2.state !== "generating" || operation.current) return;
    const controller = new AbortController();
    setV2Stage((stage) => stage ?? "QUEUED");
    void awaitRunningMasterV2(accessToken, opportunityId, v2, (job) => setV2Stage(job.current_stage), controller.signal).then((final) => {
      if (controller.signal.aborted) return;
      setV2(final); setV2Stage(null);
      if (final.state === "ready") setSaved("Master Presentation V2 is ready.");
      else if (final.state === "failed") setV2Error(final.job?.error_message || "Master Presentation V2 was not generated. Retry the generation.");
      refreshWorkflow();
    }).catch((cause) => {
      if (controller.signal.aborted) return;
      setV2Stage(null); setV2Error(masterV2Error(cause));
    });
    return () => controller.abort();
    // Keyed on the running job, not on the status object: every poll result would restart it.
  }, [accessToken, live, opportunityId, v2?.state, v2JobId]);

  async function generateV2() {
    if (v2LanguageGate.blocked) return;
    if (!review || !accessToken || !live || operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy("Generating Master Presentation V2"); setError(null); setSaved(null); setV2Error(null); setV2Stage("QUEUED");
    setV2((value) => value ? { ...value, state: "generating", can_generate: false } : value);
    try {
      setV2(await generateAndAwaitMasterV2(accessToken, opportunityId, (job) => setV2Stage(job.current_stage), controller.signal, discovery2Language.status === "verified" ? discovery2Language.language : selectedLanguage));
      setSaved("Master Presentation V2 is ready.");
    } catch (cause) {
      if (controller.signal.aborted) return;
      setV2Error(documentLanguageErrorMessage(cause) ?? masterV2Error(cause));
      // Show what the server holds now: a failed job, a still-running one, or changed sources.
      try { setV2(await loadMasterV2Status(accessToken, opportunityId)); } catch { /* the error above is shown */ }
      try { await refresh(accessToken, controller.signal); } catch { /* the review on screen stays */ }
    } finally {
      if (operation.current === controller) operation.current = null;
      if (!controller.signal.aborted) { setBusy(""); setV2Stage(null); refreshWorkflow(); }
    }
  }

  // Previous document flow (standalone PPT #2). Not part of the Master Presentation V2 path.
  // BAP-12: that flow writes English only. A German client is not given English documents silently.
  const legacyLanguageBlocked = selectedLanguage !== "en";
  async function generateDocuments() {
    if (!review || !transcript || notesDirty || legacyLanguageBlocked) return;
    await run("Preparing documents", async (token, signal) => {
      const prepared = await prepareMeetingEvidence(token, opportunityId, transcriptId, notes, baseline, { signal, onProgress: setBusy });
      signal.throwIfAborted();
      setBusy("Generating documents");
      await generateAndAwaitPostMeetingPresentation(token, opportunityId, () => signal.throwIfAborted(), {
        regeneratePresentationId: prepared.workflow.documents.ppt2?.presentation_id,
        language: "en",
      });
      if (!signal.aborted) router.push(`${root}/follow-up`);
    });
  }

  function toggle(category: ExtractionCategory, text: string) {
    setExcluded((current) => {
      const items = current[category] ?? [];
      return { ...current, [category]: items.includes(text) ? items.filter((item) => item !== text) : [...items, text] };
    });
  }

  const selectedIds = useCaseDraft ?? review?.selected_use_cases.use_case_ids ?? [];
  const findingsMessage = {
    "no-transcript": "Upload a transcript to analyse the meeting.",
    "not-analysed": "The meeting has not been analysed yet.",
    "other-transcript": `These findings come from ${review?.extraction.transcript_file_name ?? "a transcript that was removed"}, not from the selected transcript${transcript ? ` ${transcript.file_name}` : ""}, so they may be out of date. Analyse the selected transcript to replace them.`,
    "notes-unsaved": "You have unsaved notes. Save them, then analyse again so the findings include them.",
    stale: "These findings are out of date.",
    current: review && review.extraction.item_count === 0
      ? "The analysis ran on the selected transcript and the saved notes, and found no findings."
      : "Findings match the selected transcript and the saved notes.",
  }[state];
  // The analysed transcript lost speaker changes when it was read: its findings may not match it.
  const speakerWarning = state === "current" || state === "stale" ? mergedSpeakerText(transcript) : null;
  const adoptNotes = (value: PersonalNotes, keepTyped: boolean) => {
    setReview((current) => current ? { ...current, personal_notes: { status: value.text ? "available" : "missing", text: value.text, updated_at: value.updated_at } } : current);
    if (!keepTyped) setNotes(value.text ?? "");
    setConflictingNotes(null); setError(null);
  };

  // ---- what the page shows; every action below calls the same handler and obeys the same gate as before
  const v2Generating = v2?.state === "generating" || busy === "Generating Master Presentation V2";
  const found = review ? EXTRACTION_FIELDS.reduce((sum, [key]) => sum + review.extraction.categories[key].length, 0) : 0;
  const excludedNow = review ? EXTRACTION_FIELDS.reduce((sum, [key]) => sum + review.extraction.categories[key].filter((item) => (excluded[key] ?? []).includes(item.text)).length, 0) : 0;
  const analysed = Boolean(review) && review!.extraction.status !== "missing";
  const v2Label = !v2 ? "Checking..." : v2Generating ? "Generating" : v2.state === "ready" ? `Ready · revision ${v2.latest_ready?.version_number ?? ""}`.trim()
    : v2.state === "failed" ? "Failed" : v2.state === "outdated" ? "Needs a new revision" : "Not generated";
  // "optional": nothing is missing. Personal notes are never required, so without notes the row is
  // neither done nor open, and it is not counted as a step.
  type Mark = "done" | "todo" | "attention" | "optional";
  // BAP-7: Discovery 2 is generated and approved from the confirmed meeting before V2.
  const discovery2 = review?.discovery2;
  const discovery2Pending = Boolean(review && needsDiscovery2(review.readiness.blockers));
  const discovery2Label = !confirmedCurrent ? "After confirmation" : !discovery2 ? "Checking..."
    : discovery2.status === "approved" ? `Approved · version ${discovery2.latest_version?.version_number ?? ""}`.trim()
    : { not_generated: "Not generated", generating: "Generating", failed: "Generation failed", draft: "Draft · approve it", stale: "Out of date" }[discovery2.status];
  const discovery2Mark: Mark = confirmedCurrent && discovery2?.status === "approved" && !discovery2Pending ? "done"
    : discovery2?.status === "stale" || discovery2?.status === "failed" ? "attention" : "todo";
  const readiness: { key: string; label: string; value: string; mark: Mark; optional?: boolean; testid?: string }[] = review ? [
    { key: "discovery", label: "Approved Discovery", value: review.approved_discovery.status === "available" ? `Version ${review.approved_discovery.version_number}` : "Missing", mark: review.approved_discovery.status === "available" ? "done" : "attention" },
    { key: "v1", label: "Master Presentation V1", value: review.master_presentation.status === "ready" ? "Ready" : review.master_presentation.status === "legacy" ? "Earlier deck format" : "Missing", mark: review.master_presentation.status === "ready" ? "done" : "attention" },
    { key: "transcript", label: "Transcript", value: transcript ? "Selected" : "Required", mark: transcript ? "done" : "todo" },
    { key: "notes", label: "Personal notes", value: notesDirty ? "Unsaved" : baseline.text ? "Saved" : "Optional · none", mark: notesDirty ? "attention" : baseline.text ? "done" : "optional", optional: true, testid: "notes-row" },
    { key: "findings", label: "Meeting findings", value: { "no-transcript": "Not analysed", "not-analysed": "Not analysed", "other-transcript": "Other transcript", "notes-unsaved": "Out of date", stale: "Out of date", current: review.extraction.item_count === 0 ? "None found" : `${review.extraction.item_count} found` }[state], mark: state === "current" && review.extraction.item_count > 0 ? "done" : analysed ? "attention" : "todo" },
    { key: "confirmation", label: "Meeting findings confirmed", value: confirmedCurrent ? `Yes · ${review.confirmation.confirmed_count} included` : review.confirmation.status === "none" ? "Not yet" : "Out of date", mark: confirmedCurrent ? "done" : review.confirmation.status === "none" ? "todo" : "attention", testid: "findings-confirmation" },
    { key: "discovery2", label: "Discovery 2", value: discovery2Label, mark: discovery2Mark, testid: "discovery2-row" },
    { key: "v2", label: "Master Presentation V2", value: confirmedCurrent && review.readiness.ready_for_v2 ? v2Label : confirmedCurrent && discovery2Pending ? "After Discovery 2" : "After confirmation", mark: v2?.state === "ready" ? "done" : v2?.state === "failed" || v2?.state === "outdated" ? "attention" : "todo", testid: "v2-status" },
    { key: "owner", label: "Presentation owner review", value: workflowCompleted(workflow, "owner_review") ? "Completed" : v2?.state === "ready" ? "Pending · review V2" : "Pending · after V2", mark: workflowCompleted(workflow, "owner_review") ? "done" : "todo", testid: "presentation-owner-review" },
  ] : [];
  const requiredSteps = readiness.filter((row) => !row.optional);
  const canGenerateV2 = Boolean(review?.readiness.ready_for_v2 && confirmedCurrent);
  const v2LanguageGate = sourceLanguageGate(selectedLanguage, discovery2Language, { artifact: "Master Presentation V2", source: "Discovery 2" });
  useEffect(() => {
    // V2 is offered only once Discovery 2 is approved and current; before that nothing is verified.
    if (!accessToken || !live || !canGenerateV2) { setDiscovery2Language({ status: "none" }); return; }
    const controller = new AbortController();
    setDiscovery2Language({ status: "loading" });
    void loadPostMeetingDiscovery(accessToken, opportunityId, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        const approved = approvedDiscovery2Language(value);
        // The review says V2 is ready, so a missing approved Discovery 2 is a contradiction: fail closed.
        setDiscovery2Language(approved
          ? { status: "verified", language: approved.language, versionNumber: approved.versionNumber }
          : { status: "failed", message: "the approved Discovery 2 was not found" });
      })
      .catch((cause) => { if (!controller.signal.aborted) setDiscovery2Language(failedLookup(cause)); });
    return () => controller.abort();
  }, [accessToken, live, opportunityId, canGenerateV2, languageCheck]);
  // Tabs follow the usual keyboard pattern: arrow keys, Home and End move between them, and only
  // the selected tab is in the page's tab order. Switching never touches what was typed.
  const INPUT_TABS = ["transcript", "notes"] as const;
  function onTabKey(event: React.KeyboardEvent<HTMLDivElement>) {
    const index = INPUT_TABS.indexOf(inputTab);
    const next = event.key === "ArrowRight" ? (index + 1) % INPUT_TABS.length : event.key === "ArrowLeft" ? (index + INPUT_TABS.length - 1) % INPUT_TABS.length
      : event.key === "Home" ? 0 : event.key === "End" ? INPUT_TABS.length - 1 : -1;
    if (next < 0) return;
    event.preventDefault();
    setInputTab(INPUT_TABS[next]);
    document.getElementById(`tab-${INPUT_TABS[next]}`)?.focus();
  }

  return <section aria-labelledby="meeting-title" className={styles.meetingPage}>
    <header className={styles.heading}><span className={styles.eyebrow}>Post-meeting</span><h1 id="meeting-title">Turn the meeting into the next pitch</h1><p>{companyName} · First meeting</p></header>
    <PostMeetingPhaseNav opportunityId={opportunityId} active="meeting" />
    {error ? <p className={styles.error} role="alert">{error}</p> : null}
    {busy ? <p className={styles.notice} role="status">{busy}...</p> : saved ? <p className={styles.notice} role="status">{saved}</p> : null}
    {live && !review ? <div className={styles.notice}>{error ? <button className="btn btn-secondary" onClick={() => setReload((value) => value + 1)}>Retry loading</button> : <span role="status">Loading meeting inputs...</span>}</div> : null}
    {finalized ? <p className={styles.notice}>This package is finalized. Meeting inputs are read-only.</p> : null}
    {review?.execution_mode === "fixture" ? <p className={styles.notice} data-testid="fixture-notice">Test mode: this environment analyses meetings with a rule-based extractor that only picks up lines such as “Requirement: …” or “Decision: …”. No AI model is called.</p> : null}
    <div className={`${styles.columns} ${styles.meetingColumns}`}>
      <div className={styles.stack}>
        <article className={`${styles.card} ${styles.meetingCard}`} aria-labelledby="meeting-input-title" data-testid="meeting-input">
          <span className={styles.eyebrow}>Meeting input</span><h2 id="meeting-input-title">What happened in the meeting?</h2>
          <p className={styles.meetingIntro}>Add the transcript of the first client meeting and, if you like, your own notes. The two are kept apart: notes are your view and are never treated as something said in the meeting.</p>
          <div className={styles.inputTabs} role="tablist" aria-label="Meeting input" onKeyDown={onTabKey}>
            <button type="button" role="tab" id="tab-transcript" aria-controls="panel-transcript" aria-selected={inputTab === "transcript"} tabIndex={inputTab === "transcript" ? 0 : -1} className={inputTab === "transcript" ? styles.inputTabActive : undefined} onClick={() => setInputTab("transcript")}>
              Upload transcript<small>{review?.transcripts.length ? `${review.transcripts.length} added` : previewFile ? "1 selected" : "Required"}</small></button>
            <button type="button" role="tab" id="tab-notes" aria-controls="panel-notes" aria-selected={inputTab === "notes"} tabIndex={inputTab === "notes" ? 0 : -1} className={inputTab === "notes" ? styles.inputTabActive : undefined} onClick={() => setInputTab("notes")}>
              Type notes<small data-testid="notes-tab-state" data-attention={notesDirty ? "true" : undefined}>{notesDirty ? "Unsaved" : baseline.text ? "Saved" : "Optional"}</small></button>
          </div>

          <div role="tabpanel" id="panel-transcript" aria-labelledby="tab-transcript" tabIndex={0} hidden={inputTab !== "transcript"} className={styles.inputPanel}>
            <div className={styles.actions}>
              <button className="btn btn-secondary" disabled={editingDisabled} onClick={() => fileInput.current?.click()}>{review?.transcripts.length || previewFile ? "Upload another transcript" : "Choose a transcript file"}</button>
              <small>TXT, VTT, SRT or DOCX{review && review.transcripts.length > 1 ? " · choose the one to analyse" : ""}</small>
              <input ref={fileInput} type="file" hidden accept=".txt,.vtt,.srt,.docx" aria-label="Meeting transcript" onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ""; if (file) void onTranscript(file); }} />
            </div>
            {previewMode ? <div className={styles.transcriptCard} role="status"><div><strong>{previewFile?.name ?? "No transcript added"}</strong><small>{previewFile ? "Selected locally; not uploaded" : "Layout preview only"}</small></div></div> : null}
            {live && review && !review.transcripts.length ? <div className={styles.transcriptCard} role="status"><span className={`${styles.transcriptIcon} ${styles.transcriptIconEmpty}`} aria-hidden="true">–</span><div><strong>No transcript added</strong><small>Nothing can be analysed until a transcript is uploaded.</small></div></div> : null}
            {live && review?.transcripts.length ? <ul className={styles.transcriptList} data-testid="transcript-list">{review.transcripts.map((item) => <li key={item.id} className={item.id === transcriptId ? styles.transcriptSelected : undefined}>
              <label className={styles.check}>
                {review.transcripts.length > 1
                  ? <input type="radio" name="transcript" checked={item.id === transcriptId} disabled={editingDisabled} onChange={() => selectTranscript(item.id)} />
                  : <span className={styles.transcriptIcon} aria-hidden="true">✓</span>}
                <span><strong>{item.file_name}</strong><small>Uploaded {when(item.created_at)} · {item.turn_count} speaker turns</small></span>
                <span className={styles.transcriptBadges}>
                  {item.id === transcriptId ? <em className={styles.transcriptBadge} data-testid="transcript-selected">Selected</em> : null}
                  {item.analysed ? <em className={`${styles.transcriptBadge} ${styles.transcriptBadgeMuted}`} data-testid="transcript-analysed">Current findings</em> : null}
                  {item.id !== transcriptId && !item.analysed ? <em className={`${styles.transcriptBadge} ${styles.transcriptBadgeMuted}`}>Uploaded</em> : null}
                </span>
              </label>
              {mergedSpeakerText(item) ? <p className={styles.transcriptWarning} role="note" data-testid="merged-speakers">{mergedSpeakerText(item)}</p> : null}
            </li>)}</ul> : null}
          </div>

          <div role="tabpanel" id="panel-notes" aria-labelledby="tab-notes" tabIndex={0} hidden={inputTab !== "notes"} className={styles.inputPanel}>
            <label className={styles.notesField} htmlFor="personal-notes">Personal notes <span>(optional)</span>
              <textarea id="personal-notes" value={notes} maxLength={20000} disabled={editingDisabled} placeholder="Anything you noticed that the transcript does not show?" onChange={(event) => { setNotes(event.target.value); setSaved(null); }} />
            </label>
            <div className={styles.actions}>
              <button className="btn btn-secondary" disabled={editingDisabled || !notesDirty || Boolean(conflictingNotes)} onClick={() => void saveNotes()}>Save notes</button>
              <small role="status" data-testid="notes-status">{notesDirty ? "Unsaved changes" : baseline.text ? `Saved ${when(baseline.updated_at)}` : "No notes saved"}</small>
            </div>
            {conflictingNotes ? <div className={styles.source}><strong>Notes saved in another session</strong><p>{conflictingNotes.text || "No notes"}</p><div className={styles.actions}>
              <button className="btn btn-secondary" disabled={Boolean(busy)} onClick={() => adoptNotes(conflictingNotes, false)}>Use saved notes</button>
              <button className="btn btn-secondary" disabled={Boolean(busy)} onClick={() => adoptNotes(conflictingNotes, true)}>Keep my notes</button>
            </div></div> : null}
          </div>

          {live && review ? <dl className={styles.sourceMeta} data-testid="source-metadata">
            <div><dt>Transcript</dt><dd>{transcript ? `${transcript.file_name} · ${transcript.turn_count} speaker turns` : "None selected"}</dd></div>
            <div><dt>Personal notes</dt><dd>{notesDirty ? "Unsaved changes" : baseline.text ? `Saved ${when(baseline.updated_at)}` : "None"}</dd></div>
          </dl> : null}
        </article>

        {live && review ? <article className={styles.card} aria-labelledby="findings-title" data-testid="meeting-findings">
          <div className={styles.findingsHead}>
            <div><span className={styles.eyebrow}>Analysis</span><h2 id="findings-title">Meeting findings</h2></div>
            {analysed ? <span className={`${styles.statePill} ${confirmedCurrent ? styles.statePillDone : state === "current" ? "" : styles.statePillWarn}`} data-testid="findings-state">
              {confirmedCurrent ? "Confirmed by you" : state === "current" ? "Analysis · not confirmed" : "Out of date"}</span> : null}
          </div>
          <p className={styles.meetingIntro}>The analysis sorts what it finds in the selected transcript and your saved notes into seven categories. Findings are an interpretation of the sources until you confirm them.</p>
          <p className={state === "current" && found > 0 && !speakerWarning ? styles.notice : styles.error} role="status" data-testid="findings-status">{speakerWarning
            ? `Analysed, but the findings may not match the selected transcript. ${speakerWarning}`
            : findingsMessage}{state === "stale" ? ` ${review.extraction.stale_reasons.map(staleReasonText).join(" ")}` : ""}</p>
          <div className={styles.actions}>
            <button className="btn btn-secondary" disabled={editingDisabled || !transcript || notesDirty} onClick={() => void analyse()}>{review.extraction.status === "missing" ? "Analyse meeting" : state === "other-transcript" ? "Analyse selected transcript" : "Analyse again"}</button>
            {review.extraction.generated_at ? <small>Last analysed {when(review.extraction.generated_at)}{review.extraction.execution_mode === "fixture" ? " · rule-based test extractor" : review.extraction.execution_mode === "live" ? " · AI model" : ""}</small> : null}
            {analysed ? <strong className={styles.findingTotals} data-testid="findings-totals">{found - excludedNow} included · {excludedNow} excluded</strong> : null}
          </div>
          {/* Nothing found: say why and what helps. An empty result cannot be confirmed. */}
          {state === "current" && found === 0 ? <div className={styles.source} data-testid="no-findings">
            <strong>Nothing to confirm yet</strong>
            {review.extraction.execution_mode === "fixture"
              ? <p>This test environment does not interpret conversation. Its rule-based extractor only picks up statements that are labelled with a category, for example “Requirement: …”, “Challenge: …”, “Priority: …”, “Opportunity: …”, “Discussed solution: …”, “Decision: …” or “Follow-up: …”. A time stamp and a speaker name in front of the label are fine. The selected transcript and the notes contain no such line.</p>
              : <p>The analysis found no statement it could support from the selected transcript or your notes.</p>}
            <p>Upload a transcript that contains the meeting, or add your own notes, then analyse again. Master Presentation V2 needs at least one confirmed finding.</p>
          </div> : null}
          {analysed ? <div className={styles.extraction}>{EXTRACTION_FIELDS.map(([key, label]) => {
            const items = review.extraction.categories[key];
            const out = items.filter((item) => (excluded[key] ?? []).includes(item.text)).length;
            return <details key={key} open={items.length > 0} className={styles.findingGroup} aria-label={label}>
              <summary><h3>{label}</h3><span className={styles.findingCount}>{items.length}</span>{out ? <small>{out} excluded</small> : null}</summary>
              {items.length ? <ul className={styles.findings}>{items.map((item) => {
                const excludedItem = (excluded[key] ?? []).includes(item.text);
                return <li key={item.text} className={excludedItem ? styles.findingExcluded : undefined}>
                  <label className={styles.check}>
                    <input type="checkbox" checked={!excludedItem} disabled={editingDisabled || state !== "current"} onChange={() => toggle(key, item.text)} aria-label={`Include: ${item.text}`} />
                    <span><b className={styles.findingText}>{item.text}</b><small><em className={`${styles.sourceBadge} ${item.source === "personal_notes" ? styles.sourceBadgeNotes : item.source === "both" ? styles.sourceBadgeBoth : ""}`}>{FINDING_SOURCE_LABEL[item.source]}</em>{excludedItem ? " Excluded" : ""}</small></span>
                  </label>
                </li>;
              })}</ul> : <p><small>Nothing captured. Not mentioned in the transcript or the notes.</small></p>}
            </details>;
          })}</div> : null}
        </article> : null}

        {live && review ? <article className={styles.card} aria-labelledby="use-cases-title">
          <span className={styles.eyebrow}>Borek use cases</span><h2 id="use-cases-title">Selected Borek use cases</h2>
          {review.selected_use_cases.use_cases.length ? <ul className={styles.findings}>{review.selected_use_cases.use_cases.map((item) => <li key={item.fact_id}>{item.statement ?? item.fact_id}{item.status !== "resolved" ? <small> · no longer available</small> : null}</li>)}</ul> : <p><small>No use case selected. This is optional.</small></p>}
          <details onToggle={(event) => { if ((event.target as HTMLDetailsElement).open) void openUseCases(); }}>
            <summary>Change selection</summary>
            {!available ? <p role="status"><small>Loading approved use cases...</small></p> : !available.length ? <p><small>No approved use cases are available.</small></p> : <>
              {available.map((item) => <label key={item.fact_id} className={styles.check}>
                <input type="checkbox" checked={selectedIds.includes(item.fact_id)} disabled={editingDisabled} onChange={() => setUseCaseDraft(selectedIds.includes(item.fact_id) ? selectedIds.filter((id) => id !== item.fact_id) : [...selectedIds, item.fact_id])} />
                <span>{item.statement ?? item.fact_id}</span>
              </label>)}
              <div className={styles.actions}><button className="btn btn-secondary" disabled={editingDisabled || !useCaseDraft} onClick={() => void saveUseCases()}>Save use cases</button></div>
            </>}
          </details>
        </article> : null}
      </div>

      <aside className={`${styles.summary} ${styles.meetingSummary}`} aria-labelledby="readiness-title" data-testid="meeting-readiness">
        <span className={styles.eyebrow}>{companyName}</span>
        <h2 id="readiness-title">{previewMode ? "Layout preview" : review ? selectionChanged ? "Confirm your changes" : reviewHeadline(review, v2?.state) : "Loading"}</h2><small>First meeting</small>
        {live && review ? <>
          <p className={styles.readinessSummary} data-testid="readiness-summary">
            <span data-testid="readiness-progress">{requiredSteps.filter((row) => row.mark === "done").length} of {requiredSteps.length} steps complete</span>
            <button type="button" className={styles.readinessToggle} data-testid="readiness-toggle" aria-expanded={detailsOpen} aria-controls="readiness-details readiness-notes" onClick={() => setDetailsOpen((open) => !open)}>{detailsOpen ? "Hide details" : "Show details"}</button>
          </p>
          <div id="readiness-details" className={styles.readinessDetails} data-collapsed={!detailsOpen}>
          <ul className={styles.readiness}>
            {readiness.map((row) => <li key={row.key} data-testid={row.testid} data-mark={row.mark}>
              <i className={styles.readinessMark} aria-hidden="true">{row.mark === "done" ? "✓" : row.mark === "attention" ? "!" : row.mark === "optional" ? "–" : ""}</i><span>{row.label}</span><strong>{row.value}</strong>
            </li>)}
          </ul>
          {review.confirmation.status === "stale" ? <p><small>{review.confirmation.stale_reasons.map(staleReasonText).join(" ")}</small></p> : null}
          {!canGenerateV2 ? <div data-testid="v2-blockers" className={styles.blockers}><small>Still needed before Master Presentation V2:</small><ul>{(selectionChanged ? ["MEETING_REVIEW_NOT_CONFIRMED"] : review.readiness.blockers.filter((code) => code !== "MEETING_REVIEW_NOT_CONFIRMED" || !review.readiness.blockers.includes("MEETING_FINDINGS_EMPTY"))).map((code) => <li key={code}><span>{blockerText(code)}</span></li>)}</ul></div> : null}
          </div>
          {canGenerateV2 ? <div data-testid="v2-ready">
            {v2Error ? <p className={styles.error} role="alert" data-testid="v2-error">{v2Error}</p> : null}
            {v2Generating ? <p role="status" className={styles.progressLine} data-testid="v2-progress">{masterV2StageText(v2Stage)}...</p> : null}
            {v2?.state === "ready" ? <p data-testid="v2-generated"><strong>Master Presentation V2 is ready</strong> · revision {v2.latest_ready?.version_number}.</p> : null}
            {v2?.state === "outdated" ? <p data-testid="v2-outdated"><small>A Master Presentation V2 exists (revision {v2.latest_ready?.version_number}), but it was built before the latest confirmation. <Link href={`${root}/post-meeting-presentation`}>Open it</Link> or generate a new revision.</small></p> : null}
            {!v2 && !v2Error ? <p role="status"><small>Checking Master Presentation V2...</small></p> : null}
          </div>
            : null}

          {/* BAP-12: V2 waits for a Discovery 2 in the client's document language; explained, not hidden. */}
          {canGenerateV2 && v2LanguageGate.message && <DocumentLanguageWarning>
            {v2LanguageGate.message}
            {v2LanguageGate.retry && <> <button className="btn btn-secondary" type="button" data-testid="v2-language-retry" onClick={() => setLanguageCheck((value) => value + 1)}>Check again</button></>}
          </DocumentLanguageWarning>}
          {/* One primary action: the next step the current state allows. Each one calls the same
              handler as before and is held back by the same gate. */}
          <div className={styles.primaryAction} data-testid="primary-action">
            {finalized ? <Link className="btn btn-primary" href={`${root}/follow-up`}>Open follow-up email</Link>
              : v2Generating ? <button className="btn btn-primary" data-testid="generate-v2" disabled>Generating Master Presentation V2...</button>
              : !transcript ? <button className="btn btn-primary" disabled={editingDisabled} onClick={() => { setInputTab("transcript"); fileInput.current?.click(); }}>Upload transcript</button>
              : notesDirty ? <button className="btn btn-primary" disabled={editingDisabled || Boolean(conflictingNotes)} onClick={() => void saveNotes()}>Save notes</button>
              : state !== "current" ? <button className="btn btn-primary" disabled={editingDisabled} onClick={() => void analyse()}>{review.extraction.status === "missing" ? "Analyse meeting" : state === "other-transcript" ? "Analyse selected transcript" : "Analyse again"}</button>
              : found === 0 ? <button className="btn btn-primary" data-testid="no-findings-action" disabled={editingDisabled} onClick={() => { setInputTab("transcript"); fileInput.current?.click(); }}>Upload another transcript</button>
              : !confirmedCurrent && found === excludedNow ? <button className="btn btn-primary" data-testid="confirm-blocked" disabled>Confirm meeting information</button>
              : !confirmedCurrent ? <button className="btn btn-primary" disabled={editingDisabled} onClick={() => void confirm()}>{busy === "Confirming findings" ? "Confirming..." : "Confirm meeting information"}</button>
              : discovery2Pending ? <Link className="btn btn-primary" data-testid="open-discovery2" href={`${root}/post-meeting-discovery`}>{discovery2?.status === "draft" ? "Review and approve Discovery 2" : discovery2?.status === "stale" ? "Generate Discovery 2 again" : "Continue to Discovery 2"}</Link>
              : v2?.state === "ready" ? <Link className="btn btn-primary" data-testid="open-v2" href={`${root}/post-meeting-presentation`}>Open Master Presentation V2</Link>
              : canGenerateV2 && v2 ? <button className="btn btn-primary" data-testid="generate-v2" disabled={editingDisabled || !v2.can_generate || v2LanguageGate.blocked} onClick={() => void generateV2()}>
                {v2.state === "failed" ? "Retry Master Presentation V2" : v2.state === "outdated" ? "Generate a new V2 revision" : "Generate Master Presentation V2"}</button>
              : null}
          </div>
          <div id="readiness-notes" className={styles.readinessDetails} data-collapsed={!detailsOpen}>
          {confirmedCurrent ? <p><small>Confirmed {when(review.confirmation.confirmed_at)}: {review.confirmation.confirmed_count} {review.confirmation.confirmed_count === 1 ? "finding" : "findings"} included{review.confirmation.excluded_count ? `, ${review.confirmation.excluded_count} excluded` : ""}.</small></p>
            : state === "current" && found === 0 ? null
            : state === "current" && found === excludedNow ? <p data-testid="all-excluded"><small>Every finding is excluded. Include at least one finding to confirm: Master Presentation V2 is built from confirmed findings.</small></p>
            : state === "current" ? <p><small>Untick any finding that is wrong or should not be used. Confirming records the remaining findings as checked by you.</small></p> : null}
          <p><small>Confirming the meeting findings is not the owner review of the presentation. That review is a separate, later step: it happens once Master Presentation V2 exists.</small></p>
          {canGenerateV2 && v2 && v2.state !== "ready" && !v2Generating ? <p><small>V2 is a new version of the same presentation as V1: the same 26 Borek slides with a new appendix from the confirmed findings.</small></p> : null}

          {/* Earlier pitches without a Master Presentation only. In the Master journey the next
              presentation is V2 of the same presentation; the standalone PPT #2 is not offered. */}
          {legacyJourney ? <details data-testid="previous-document-flow">
            <summary><small>Previous document flow</small></summary>
            <p><small>Generates the earlier standalone PPT #2 and follow-up documents. This pitch has no Master Presentation, so the earlier flow applies. It does not use your confirmation.</small></p>
            <button className="btn btn-secondary" disabled={editingDisabled || !transcript || notesDirty || !workflow?.documents.approved_discovery || Boolean(conflictingNotes) || legacyLanguageBlocked} onClick={() => void generateDocuments()}>Generate documents</button>
            {legacyLanguageBlocked && <DocumentLanguageWarning>The earlier document flow writes English documents only, and this client&apos;s document language is {documentLanguageLabel(selectedLanguage)}. Switch the document language to English to use it; German post-meeting documents are Master Presentation V2.</DocumentLanguageWarning>}
            {workflow?.documents.ppt2?.latest_ready_version_id ? <div className={styles.actions}><Link href={`${root}/post-meeting-presentation`}>View existing presentation</Link></div> : null}
          </details> : null}
          </div>
        </> : previewMode ? <p><small>Layout preview only. Uploading, analysing and confirming require a live session.</small></p> : null}
      </aside>
    </div>
  </section>;
}
