"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";

import { useAuth } from "@/components/AuthProvider";
import { usePreviewJourney } from "@/components/PreviewJourneyProvider";
import {
  DOCUMENT_LANGUAGES,
  deckLanguageSummary,
  documentLanguageErrorMessage,
  documentLanguageLabel,
  languageChangeMessage,
  languageChangeMode,
  normalizeDocumentLanguage,
  saveDocumentLanguage,
  type DeckLanguageSource,
  type DocumentLanguage,
} from "@/lib/documentLanguage";

/**
 * BAP-12: the document language of one opportunity - read from, and saved to, the server for a
 * live client. It is separate from the interface language (LanguageProvider). Changing it never
 * generates, approves or replaces a document by itself.
 */
export function useDocumentLanguage(opportunityId: string) {
  const { accessToken, previewMode } = useAuth();
  const { getOpportunity, setDocumentLanguage } = usePreviewJourney();
  const live = Boolean(accessToken) && !previewMode;
  const record = getOpportunity(opportunityId);
  const language = normalizeDocumentLanguage(record?.document_language);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Answers for an earlier client (or an earlier choice) never land on the current one.
  const requestRef = useRef(0);
  useEffect(() => {
    // Another client (or another signed-in session): drop the messages and any answer still on its way.
    requestRef.current += 1;
    setSaving(false); setError(null); setNotice(null);
  }, [opportunityId, live]);

  async function change(next: DocumentLanguage) {
    if (next === language || saving) return;
    const request = ++requestRef.current;
    setError(null);
    setNotice(null);
    const mode = languageChangeMode(live && Boolean(accessToken), opportunityId);
    if (mode !== "server" || !accessToken) {
      // Preview: local only. Pending: the server record is created with this language at the first generation.
      setDocumentLanguage(opportunityId, next);
      setNotice(languageChangeMessage(mode, next));
      return;
    }
    setSaving(true);
    try {
      const stored = await saveDocumentLanguage(accessToken, opportunityId, next);
      if (request !== requestRef.current) return;
      setDocumentLanguage(opportunityId, stored);
      setNotice(languageChangeMessage("server", stored));
    } catch (cause) {
      if (request !== requestRef.current) return;
      setError(documentLanguageErrorMessage(cause) ?? "The document language could not be saved. Check the connection and try again; it was not changed.");
    } finally {
      if (request === requestRef.current) setSaving(false);
    }
  }

  return { language, live, saving, error, notice, change, known: Boolean(record) };
}

interface DocumentLanguageFieldProps {
  value: DocumentLanguage;
  onChange: (language: DocumentLanguage) => void;
  disabled?: boolean;
  /** Why the choice cannot be changed right now, shown under the options. */
  disabledReason?: string | null;
  description?: string;
  compact?: boolean;
  busy?: boolean;
}

/** The two-option radio group. Arrow keys move between the options (native radio behaviour). */
export function DocumentLanguageField({ value, onChange, disabled = false, disabledReason, description, compact = false, busy = false }: DocumentLanguageFieldProps) {
  const id = useId();
  const descriptionId = `${id}-description`;
  return (
    <fieldset className={`document-language-field${compact ? " is-compact" : ""}`} aria-describedby={description || disabledReason ? descriptionId : undefined} aria-busy={busy || undefined} disabled={disabled}>
      <legend>Document language</legend>
      <div className="document-language-options">
        {DOCUMENT_LANGUAGES.map((option) => (
          <label key={option.code} className={`document-language-option${value === option.code ? " is-selected" : ""}`}>
            <input
              type="radio"
              name={`${id}-document-language`}
              value={option.code}
              checked={value === option.code}
              onChange={() => onChange(option.code)}
              aria-labelledby={`${id}-${option.code}-label`}
            />
            <span id={`${id}-${option.code}-label`} lang={option.code}>{option.label}</span>
            {option.code === "en" ? <small>Default</small> : null}
          </label>
        ))}
      </div>
      {description || disabledReason ? (
        <p id={descriptionId} className="document-language-help">{disabledReason ?? description}</p>
      ) : null}
    </fieldset>
  );
}

interface DocumentLanguageSelectorProps {
  opportunityId: string;
  /** e.g. while a generation runs - the language must not change under it. */
  locked?: boolean;
  lockedReason?: string | null;
  description?: string;
  compact?: boolean;
}

/** The selector bound to one opportunity: shows, saves and reports the document language. */
export function DocumentLanguageSelector({ opportunityId, locked = false, lockedReason, description, compact }: DocumentLanguageSelectorProps) {
  const state = useDocumentLanguage(opportunityId);
  return (
    <div className="document-language-selector" data-testid="document-language-selector">
      <DocumentLanguageField
        value={state.language}
        onChange={(next) => void state.change(next)}
        disabled={locked || state.saving}
        disabledReason={locked ? lockedReason ?? null : null}
        description={description ?? (state.live
          ? "Client documents are generated in this language. The app's own language is set separately."
          : "Preview mode: the choice is shown here only and is not saved to the server.")}
        compact={compact}
        busy={state.saving}
      />
      {state.saving ? <p className="document-language-status" role="status">Saving the document language...</p> : null}
      {state.error ? <p className="client-information-error" role="alert">{state.error}</p> : null}
      {state.notice ? <p className="document-language-status" role="status" aria-live="polite">{state.notice}</p> : null}
    </div>
  );
}

/**
 * PR #14 review: the document language as shown in the page header of every workflow page, with a
 * "Change" button that opens the selector in place. Changing it only saves the setting; every
 * generate action keeps its own language gate, so no approved document is touched or bypassed.
 */
export function DocumentLanguageControl({ opportunityId, note }: { opportunityId: string; note?: string }) {
  const state = useDocumentLanguage(opportunityId);
  const [open, setOpen] = useState(false);
  const panelId = useId();
  return (
    <div className="document-language-control" data-testid="document-language-control">
      <div className="document-language-control-row">
        <DocumentLanguageBadge language={state.language} prefix="Document language" />
        <button type="button" className="btn btn-secondary document-language-change" aria-expanded={open} aria-controls={panelId}
          onClick={() => setOpen((value) => !value)}>{open ? "Done" : "Change"}</button>
      </div>
      {note && !open ? <small className="document-language-help">{note}</small> : null}
      <div id={panelId} hidden={!open} className="document-language-control-panel">
        {open ? <DocumentLanguageSelector
          opportunityId={opportunityId}
          compact
          description={state.live
            ? "Saved for this client. Only the next generation uses it: approved documents keep their language, and anything built from a document in another language waits for a new approved version."
            : undefined}
        /> : null}
      </div>
    </div>
  );
}

/** A small, read-only label: the language a document was generated in. */
export function DocumentLanguageBadge({ language, prefix = "Language" }: { language: unknown; prefix?: string }) {
  const code = normalizeDocumentLanguage(language);
  return (
    <span className="document-language-badge" data-language={code}>
      {prefix}: <span lang={code}>{documentLanguageLabel(code)}</span>
    </span>
  );
}

/** The two languages of a Master Presentation and a concise, non-blocking explanation. */
export function DeckLanguageNotice({ source }: { source: DeckLanguageSource | null | undefined }) {
  const summary = deckLanguageSummary(source);
  if (!summary) return null;
  return (
    <div className="document-language-deck" data-testid="deck-language">
      <span className="document-language-badge" data-language={summary.content}>{summary.label}</span>
      {summary.notice ? <p className="document-language-help">{summary.notice}</p> : null}
    </div>
  );
}

/** A non-blocking review warning (other-language statements, or a document in another language). */
export function DocumentLanguageWarning({ children }: { children: ReactNode }) {
  return <p className="document-language-warning" role="note">{children}</p>;
}
