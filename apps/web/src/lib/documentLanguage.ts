import { ApiRequestError, apiFetch, resolveBackendOpportunityId } from "./api";
import { BACKEND_UUID } from "./backendOpportunityMap";

/**
 * BAP-12: the language the client documents of one opportunity are generated in.
 *
 * This is NOT the interface language (LanguageProvider, kept per browser). It belongs to the
 * opportunity, is stored by the API (opportunity `language`, BAP-13) and is the same for everyone
 * who opens the client. Discovery 1 is generated in it; every later artifact inherits the language
 * of the approved document it is built from:
 *
 *   Master Presentation V1  <- approved Discovery 1
 *   Discovery 2             <- approved Discovery 1
 *   Master Presentation V2  <- approved Discovery 2
 *   follow-up email         <- the finalized V2 package
 */
export type DocumentLanguage = "en" | "de";

export const DEFAULT_DOCUMENT_LANGUAGE: DocumentLanguage = "en";

export const DOCUMENT_LANGUAGES: readonly { code: DocumentLanguage; label: string; english: string }[] = [
  { code: "en", label: "English", english: "English" },
  { code: "de", label: "Deutsch", english: "German" },
];

export function isDocumentLanguage(value: unknown): value is DocumentLanguage {
  return value === "en" || value === "de";
}

/** Any stored or reported value as a supported language; unknown or missing values are English. */
export function normalizeDocumentLanguage(value: unknown): DocumentLanguage {
  if (typeof value !== "string") return DEFAULT_DOCUMENT_LANGUAGE;
  const code = value.trim().toLowerCase();
  return code === "de" || code.startsWith("de-") || code === "german" || code === "deutsch" ? "de" : DEFAULT_DOCUMENT_LANGUAGE;
}

/** The name of a language as the selector shows it (each language in its own name). */
export function documentLanguageLabel(language: unknown): string {
  return normalizeDocumentLanguage(language) === "de" ? "Deutsch" : "English";
}

// ------------------------------------------------------------------------------------- saving

/** Whether the opportunity already exists on the server (otherwise the choice travels with its creation). */
export function hasBackendOpportunity(opportunityId: string): boolean {
  return BACKEND_UUID.test(resolveBackendOpportunityId(opportunityId));
}

/**
 * Where a changed choice goes. "preview": shown here only, never sent (fixture / preview mode).
 * "pending": a live client not yet on the server - sent when its record is created at the first
 * Discovery generation. "server": saved to the opportunity now.
 */
export function languageChangeMode(live: boolean, opportunityId: string): "preview" | "pending" | "server" {
  if (!live) return "preview";
  return hasBackendOpportunity(opportunityId) ? "server" : "pending";
}

/** What the selector says after a change, by where the choice went. Never claims a preview change was saved. */
export function languageChangeMessage(mode: "preview" | "pending" | "server", language: DocumentLanguage): string {
  if (mode === "preview") return `Preview only: ${documentLanguageLabel(language)} is shown here and was not saved to the server.`;
  if (mode === "pending") return `${documentLanguageLabel(language)} will be saved with this client when its first Discovery is generated.`;
  return `Document language saved: ${documentLanguageLabel(language)}. Nothing was generated or changed.`;
}

/**
 * Saves the document language of a server opportunity. It changes only the setting: nothing is
 * generated, approved or replaced. Returns the language the server stored.
 */
export async function saveDocumentLanguage(token: string, opportunityId: string, language: DocumentLanguage): Promise<DocumentLanguage> {
  if (!isDocumentLanguage(language)) throw new ApiRequestError("Choose English or Deutsch.", 422, "LANGUAGE_UNSUPPORTED");
  const backendId = resolveBackendOpportunityId(opportunityId);
  if (!BACKEND_UUID.test(backendId)) {
    throw new ApiRequestError("This client is not on the server yet. The language is sent when the first Discovery is generated.", 404, "OPPORTUNITY_NOT_SYNCED");
  }
  const row = await apiFetch<{ id?: string; language?: unknown }>(`/opportunities/${backendId}`, token, {
    method: "PATCH",
    body: JSON.stringify({ language }),
  });
  if (row?.id !== undefined && row.id !== backendId) {
    throw new ApiRequestError("The server answered for another opportunity. Reload the client.", 409, "OPPORTUNITY_IDENTITY_MISMATCH");
  }
  return normalizeDocumentLanguage(row?.language ?? language);
}

/** The JSON body of a generate call that names its language explicitly. */
export function languageBody(language: DocumentLanguage | null | undefined, extra: Record<string, unknown> = {}): string | undefined {
  const body = isDocumentLanguage(language) ? { ...extra, language } : extra;
  return Object.keys(body).length ? JSON.stringify(body) : undefined;
}

// ------------------------------------------------------------------------------------- errors

/** An actionable message for a refused language request, or null when the error is not about it. */
export function documentLanguageErrorMessage(error: unknown): string | null {
  if (!(error instanceof ApiRequestError)) return null;
  if (error.code === "LANGUAGE_MISMATCH") {
    return `${error.message} Nothing was generated or changed. Either switch the document language back, or generate and approve a new Discovery in the language you want first.`;
  }
  if (error.code === "LANGUAGE_UNSUPPORTED") {
    return `${error.message} Nothing was generated. Use English for this document, or generate it from a finalized Master Presentation package.`;
  }
  if (error.status === 401) return "Your session expired. Sign in again; the document language was not changed.";
  if (error.status === 403) return "This client is not available to your account, so its document language cannot be changed.";
  if (error.status === 404) return error.code === "OPPORTUNITY_NOT_SYNCED" ? error.message : "This client was not found on the server. Reload the client list and open it again.";
  if (error.status === 422) return "The server did not accept this language. Choose English or Deutsch.";
  return null;
}

// ------------------------------------------------------------------------------- inheritance

/** The latest approved Discovery 1 and the language it was written in. */
export interface ApprovedDiscoveryLanguage {
  versionId: string;
  versionNumber: number;
  language: DocumentLanguage;
}

/**
 * Reads the latest approved Discovery 1 with its language. Versions from before BAP-12 carry no
 * language in the list; then the approved paper itself is read.
 */
export async function loadApprovedDiscovery1Language(token: string, opportunityId: string, signal?: AbortSignal): Promise<ApprovedDiscoveryLanguage | null> {
  const backendId = resolveBackendOpportunityId(opportunityId);
  if (!BACKEND_UUID.test(backendId)) return null;
  const envelope = await apiFetch<{ versions?: unknown[] }>(`/opportunities/${backendId}/discovery-paper/versions`, token, { signal, cache: "no-store" });
  const approved = (Array.isArray(envelope?.versions) ? envelope.versions : [])
    .filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === "object" && (row as Record<string, unknown>).status === "approved")
    .sort((a, b) => Number(b.version_number) - Number(a.version_number))[0];
  if (!approved) return null;
  let language: unknown = approved.language;
  if (!isDocumentLanguage(language)) {
    const paper = await apiFetch<{ paper_json?: { language?: unknown } }>(`/opportunities/${backendId}/discovery-paper/approved`, token, { signal, cache: "no-store" });
    language = paper?.paper_json?.language;
  }
  return { versionId: String(approved.id), versionNumber: Number(approved.version_number), language: normalizeDocumentLanguage(language) };
}

/** Whether a downstream artifact may be generated now, and why not. */
export interface LanguageGate {
  blocked: boolean;
  /** The language the artifact will be written in (its approved source's). */
  language: DocumentLanguage;
  message: string | null;
}

/**
 * A downstream artifact is written in the language of its approved source. When the opportunity's
 * document language was changed after that source was approved, the artifact is NOT generated
 * against the old source: a new source version in the new language must be generated and approved
 * first. The approved source stays as it is.
 */
export function downstreamLanguageGate(
  selected: DocumentLanguage,
  source: { language: DocumentLanguage; versionNumber?: number | null } | null,
  names: { artifact: string; source: string },
): LanguageGate {
  if (!source) return { blocked: false, language: selected, message: null };
  if (source.language === selected) return { blocked: false, language: source.language, message: null };
  const version = source.versionNumber ? ` (version ${source.versionNumber})` : "";
  return {
    blocked: true,
    language: source.language,
    message: `The document language is ${documentLanguageLabel(selected)}, but the approved ${names.source}${version} is in ${documentLanguageLabel(source.language)}. `
      + `${names.artifact} is always written in the language of its approved ${names.source}, so it is not generated now. `
      + `Generate a new ${names.source} in ${documentLanguageLabel(selected)} and approve it first, or switch the document language back to ${documentLanguageLabel(source.language)}. `
      + `The approved ${documentLanguageLabel(source.language)} version stays unchanged in the history.`,
  };
}

/** What changing the language means right now - shown next to the selector, never acted on by itself. */
export function languageChangeNotice(
  selected: DocumentLanguage,
  current: { language: DocumentLanguage; status: "draft" | "approved"; versionNumber?: number | null } | null,
): string | null {
  if (!current || current.language === selected) return null;
  const version = current.versionNumber ? ` version ${current.versionNumber}` : "";
  const state = current.status === "approved" ? "approved" : "current draft";
  return `The ${state} Discovery${version} is in ${documentLanguageLabel(current.language)}. A new Discovery version in ${documentLanguageLabel(selected)} is needed: `
    + `choose "Regenerate", then review and approve it before the presentation and post-meeting documents are generated. `
    + `Earlier versions stay in the history unchanged.`;
}

// -------------------------------------------------------------------------- canonical deck

/** What a Master Presentation version reports about its languages (BAP-13 manifest). */
export interface DeckLanguageSource {
  language?: unknown;
  master_language?: unknown;
  other_language_findings?: unknown;
}

export interface DeckLanguageSummary {
  content: DocumentLanguage;
  master: DocumentLanguage;
  mixed: boolean;
  otherLanguageFindings: number;
  /** Short label for a badge, e.g. "Appendix: Deutsch · Master slides: English". */
  label: string;
  /** Concise, non-blocking explanation when the deck is in two languages. */
  notice: string | null;
}

/**
 * The canonical Borek master slides keep their own language; only the client-specific appendix is
 * written in the document language. A deck is therefore never described as fully German.
 */
export function deckLanguageSummary(source: DeckLanguageSource | null | undefined): DeckLanguageSummary | null {
  if (!source) return null;
  const content = normalizeDocumentLanguage(source.language);
  const master = normalizeDocumentLanguage(source.master_language);
  const findings = typeof source.other_language_findings === "number" && source.other_language_findings > 0 ? Math.floor(source.other_language_findings) : 0;
  const mixed = content !== master;
  return {
    content,
    master,
    mixed,
    otherLanguageFindings: findings,
    label: mixed ? `Appendix: ${documentLanguageLabel(content)} · Master slides: ${documentLanguageLabel(master)}` : `Language: ${documentLanguageLabel(content)}`,
    notice: mixed
      ? `The client-specific appendix is in ${documentLanguageLabel(content)}. The canonical Borek master slides stay in ${documentLanguageLabel(master)}; a ${documentLanguageLabel(content)} master deck is not available, and nothing is translated automatically.`
      : null,
  };
}

/** Review text for confirmed statements that read as another language than their document. */
export function otherLanguageReviewText(count: number, documentLanguage: DocumentLanguage, artifact: string): string | null {
  if (!count) return null;
  const one = count === 1;
  return `${one ? "1 statement reads" : `${count} statements read`} as another language than this ${artifact} (${documentLanguageLabel(documentLanguage)}). `
    + `${one ? "It is kept in the words it was" : "They are kept in the words they were"} confirmed in, not translated. `
    + `Review ${one ? "it and reword it" : "them and reword them"} if needed; acknowledging this does not translate ${one ? "it" : "them"}.`;
}
