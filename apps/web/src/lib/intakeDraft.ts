import {
  normalizeClientInformation,
  normalizeClientInformationExtras,
  validateClientInformation,
  validateClientInformationExtras,
  type ClientInformationExtras,
} from "./clientInformation";
import type { ClientInformationViewModel } from "./discoveryFirst";
import { normalizeDocumentLanguage, type DocumentLanguage } from "./documentLanguage";

const LEGACY_KEY = "borek-premeeting-create-draft-v1";

export interface IntakeDraft {
  step: 1 | 2 | 3;
  values: ClientInformationViewModel;
  extras: ClientInformationExtras;
  /** BAP-12: the document language chosen while creating the client (English when missing). */
  document_language?: DocumentLanguage;
}

export function intakeDraftStorageKey(ownerId: string): string {
  return `${LEGACY_KEY}:${ownerId}`;
}

export function intakeStorage(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function clearIntakeDraft(ownerId: string | null, storage = intakeStorage()): boolean {
  let cleared = Boolean(storage);
  for (const key of [LEGACY_KEY, ...(ownerId ? [intakeDraftStorageKey(ownerId)] : [])]) {
    try {
      storage?.removeItem(key);
    } catch {
      cleared = false;
    }
  }
  return cleared;
}

export function parseIntakeDraft(value: unknown): IntakeDraft | null {
  if (!value || typeof value !== "object") return null;
  const draft = value as IntakeDraft;
  if (![1, 2, 3].includes(draft.step) || !draft.values || !draft.extras) return null;
  const fields = ["company_name", "contact_person", "website_url", "meeting_purpose", "additional_information"] as const;
  const extras = ["company_logo_name", "business_industry", "contact_phone", "contact_position", "pitch_notes", "additional_opportunity_information"] as const;
  if (fields.some((key) => typeof draft.values[key] !== "string") ||
      extras.some((key) => typeof draft.extras[key] !== "string") ||
      !Array.isArray(draft.extras.pitch_file_names) ||
      draft.extras.pitch_file_names.some((name) => typeof name !== "string")) return null;
  const values = normalizeClientInformation(draft.values);
  const normalizedExtras = normalizeClientInformationExtras(draft.extras);
  if (draft.step > 1) {
    const errors = validateClientInformation(values);
    if (draft.step === 2) delete errors.meeting_purpose;
    if (Object.keys(errors).length || Object.keys(validateClientInformationExtras(normalizedExtras)).length) return null;
  }
  return {
    step: draft.step, values, extras: normalizedExtras,
    ...(draft.document_language === undefined ? {} : { document_language: normalizeDocumentLanguage(draft.document_language) }),
  };
}

export function restoreIntakeDraft(ownerId: string | null, storage = intakeStorage()): IntakeDraft | null {
  // An unscoped draft has no trustworthy owner and must never be migrated.
  clearIntakeDraft(null, storage);
  if (!ownerId) return null;
  try {
    const draft = parseIntakeDraft(JSON.parse(storage?.getItem(intakeDraftStorageKey(ownerId)) ?? "null"));
    if (!draft) clearIntakeDraft(ownerId, storage);
    return draft;
  } catch {
    clearIntakeDraft(ownerId, storage);
    return null;
  }
}

export function persistIntakeDraft(ownerId: string | null, draft: IntakeDraft, storage = intakeStorage()): boolean {
  const validated = parseIntakeDraft(draft);
  if (!ownerId || !storage || !validated) return false;
  try {
    storage.setItem(intakeDraftStorageKey(ownerId), JSON.stringify(validated));
    return true;
  } catch {
    return false;
  }
}
