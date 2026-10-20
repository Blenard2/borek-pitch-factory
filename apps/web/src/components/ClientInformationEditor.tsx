"use client";

import React, { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { WorkflowActionBar } from "@/components/WorkflowActionBar";
import { useAuth } from "@/components/AuthProvider";
import { useLanguage } from "@/components/LanguageProvider";
import { usePreviewJourney } from "@/components/PreviewJourneyProvider";
import { ApiRequestError, saveAdditionalInformation } from "@/lib/api";
import {
  ClientInformationAdapterError,
  clientInformationErrorMessage,
  createFixtureClientInformationAdapter,
  EMPTY_CLIENT_INFORMATION_EXTRAS,
  normalizeClientInformation,
  normalizeClientInformationExtras,
  restoreClientInformationDraft,
  validateClientInformation,
  validateClientInformationExtras,
  type ClientInformationExtraErrors,
  type ClientInformationExtras,
  type ClientInformationField,
  type ClientInformationFieldErrors,
  type ClientInformationRecord,
} from "@/lib/clientInformation";
import { clearIntakeDraft, persistIntakeDraft, restoreIntakeDraft } from "@/lib/intakeDraft";

interface ClientInformationEditorProps {
  initialRecord: ClientInformationRecord;
}

const FIELDS: Array<{
  key: ClientInformationField;
  type?: "url";
  multiline?: boolean;
}> = [
  { key: "company_name" },
  { key: "contact_person" },
  { key: "website_url", type: "url" },
  { key: "meeting_purpose", multiline: true },
  { key: "additional_information", multiline: true },
];

// BAP-10: the one client field a live session saves - context for the next Discovery generation,
// e.g. after a failed website analysis.
const LIVE_EDITABLE_FIELD: ClientInformationField = "additional_information";

/** What went wrong when a live client field could not be saved. */
function liveSaveErrorMessage(error: ApiRequestError): string {
  if (error.status === 401) return "Your session has ended. Sign in again and save once more; your text is still here.";
  if (error.status === 403 || error.status === 404) return "You do not have permission to update this client information.";
  if (error.status === 422 || error.status === 400) return "Additional information could not be saved: it is too long (at most 20,000 characters) or contains unsupported characters.";
  return "Client information could not be saved. Your text is still here; try again.";
}

const INDUSTRIES = [
  "Automotive and mobility",
  "Consumer and retail",
  "Financial services",
  "Healthcare",
  "Industrial and manufacturing",
  "Professional services",
  "Technology and software",
  "Other",
] as const;

export function ClientInformationEditor({ initialRecord }: ClientInformationEditorProps) {
  const router = useRouter();
  const { copy } = useLanguage();
  const { ownerId, accessToken, loading: authLoading } = useAuth();
  const { createOpportunity, getOpportunity, updateClient, hydrated } = usePreviewJourney();
  const adapterRef = useRef(createFixtureClientInformationAdapter(initialRecord));
  const errorSummaryRef = useRef<HTMLDivElement>(null);
  const [record, setRecord] = useState(initialRecord);
  const [draft, setDraft] = useState(initialRecord.values);
  const [editing, setEditing] = useState(initialRecord.opportunity_id === "new");
  const [busy, setBusy] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<ClientInformationFieldErrors>({});
  const [extras, setExtras] = useState<ClientInformationExtras>(EMPTY_CLIENT_INFORMATION_EXTRAS);
  const [savedExtras, setSavedExtras] = useState<ClientInformationExtras>(EMPTY_CLIENT_INFORMATION_EXTRAS);
  const [draftOwner, setDraftOwner] = useState<string | null>(null);
  const previousOwner = useRef<string | null>(null);
  const currentOwner = useRef(ownerId);
  currentOwner.current = ownerId;
  const [extraErrors, setExtraErrors] = useState<ClientInformationExtraErrors>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [createStep, setCreateStep] = useState<1 | 2 | 3>(1);
  const createMode = initialRecord.opportunity_id === "new";
  const knownOpportunity = getOpportunity(initialRecord.opportunity_id);
  const currentRecord = initialRecord.source === "live" && knownOpportunity?.client.source !== "live"
    ? initialRecord : knownOpportunity?.client ?? initialRecord;
  const liveRecord = currentRecord.source === "live";
  const canEdit = createMode || Boolean(knownOpportunity);
  const labels: Record<ClientInformationField, string> = {
    company_name: copy.clientForm.company,
    contact_person: copy.clientForm.contact,
    website_url: copy.clientForm.website,
    meeting_purpose: copy.clientForm.purpose,
    additional_information: copy.clientForm.additional,
  };

  useEffect(() => {
    if (authLoading) return;
    if (previousOwner.current !== ownerId) {
      if (previousOwner.current) clearIntakeDraft(previousOwner.current);
      setError(null);
      setNotice(null);
    }
    previousOwner.current = ownerId;
  }, [authLoading, ownerId]);

  useEffect(() => {
    if (createMode || authLoading || !hydrated) return;
    const current = currentRecord;
    const restored = restoreClientInformationDraft(current, knownOpportunity?.client_extras);
    adapterRef.current = createFixtureClientInformationAdapter(current);
    setRecord(current);
    setDraft(restored.values);
    setExtras(restored.extras);
    setSavedExtras(restored.extras);
    setEditing(false);
    setFieldErrors({});
    setExtraErrors({});
    setError(null);
    setDraftOwner(ownerId);
  }, [createMode, authLoading, hydrated, ownerId, currentRecord, knownOpportunity?.client_extras]);

  useEffect(() => {
    if (!createMode || authLoading) return;
    const stored = restoreIntakeDraft(ownerId);
    setDraft(stored?.values ?? initialRecord.values);
    setExtras(stored?.extras ?? EMPTY_CLIENT_INFORMATION_EXTRAS);
    setCreateStep(stored?.step ?? 1);
    setFieldErrors({});
    setExtraErrors({});
    setError(null);
    setNotice(stored ? "Your saved pre-meeting draft was restored." : null);
    setDraftOwner(ownerId);
  }, [createMode, authLoading, ownerId, initialRecord.values]);

  function persistCreateDraft(step: 2 | 3) {
    const persisted = persistIntakeDraft(ownerId, {
      step,
      values: normalizeClientInformation(draft),
      extras: normalizeClientInformationExtras(extras),
    });
    if (!persisted) {
      setNotice(null);
      setError("Browser storage is unavailable. Your edits are still here; retry saving before continuing.");
      requestAnimationFrame(() => errorSummaryRef.current?.focus());
    }
    return persisted;
  }

  function updateExtra<Key extends keyof ClientInformationExtras>(key: Key, value: ClientInformationExtras[Key]) {
    setExtras((current) => ({ ...current, [key]: value }));
    setExtraErrors((current) => ({ ...current, [key]: undefined }));
  }

  function selectLogo(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!["image/png", "image/jpeg", "image/svg+xml"].includes(file.type) || file.size > 5 * 1024 * 1024) {
      setExtraErrors((current) => ({ ...current, company_logo_name: "Use a PNG, JPG, or SVG file up to 5 MB." }));
      event.target.value = "";
      return;
    }
    updateExtra("company_logo_name", file.name);
  }

  function selectPitchFiles(event: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    const allowed = new Set([
      "application/pdf",
      "application/msword",
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]);
    if (files.some((file) => !allowed.has(file.type) || file.size > 10 * 1024 * 1024)) {
      setExtraErrors((current) => ({ ...current, pitch_file_names: "Use PDF, DOC, or DOCX files up to 10 MB each." }));
      event.target.value = "";
      return;
    }
    updateExtra("pitch_file_names", files.map((file) => file.name));
  }

  function updateField(key: ClientInformationField, value: string) {
    setDraft((current) => ({ ...current, [key]: value }));
    setFieldErrors((current) => ({ ...current, [key]: undefined }));
  }

  async function reload() {
    if (!canEdit || !ownerId || draftOwner !== ownerId) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const loaded = liveRecord ? currentRecord : await adapterRef.current.load(record.opportunity_id);
      if (currentOwner.current !== ownerId) return;
      setRecord(loaded);
      setDraft(loaded.values);
      const restored = restoreClientInformationDraft(loaded, getOpportunity(record.opportunity_id)?.client_extras);
      setExtras(restored.extras);
      setSavedExtras(restored.extras);
      setFieldErrors({});
      setExtraErrors({});
      setEditing(false);
      setNotice(liveRecord ? "Local extras reloaded. Live fields show the canonical record supplied by the provider." : "Client information and extras reloaded from this local preview.");
    } catch (loadError) {
      setError(clientInformationErrorMessage(loadError));
    } finally {
      setBusy(false);
    }
  }

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (authLoading || !ownerId || draftOwner !== ownerId || !canEdit || busy) return;
    if (createMode && createStep === 1) {
      const stepErrors = validateClientInformation(draft);
      const stepExtraErrors = validateClientInformationExtras(extras);
      if (Object.keys(stepErrors).length > 0 || Object.keys(stepExtraErrors).length > 0) {
        setFieldErrors(stepErrors);
        setExtraErrors(stepExtraErrors);
        setError("Check the highlighted fields and try again.");
        requestAnimationFrame(() => errorSummaryRef.current?.focus());
        return;
      }
      setFieldErrors({});
      setExtraErrors({});
      setError(null);
      if (!persistCreateDraft(2)) return;
      setCreateStep(2);
      setNotice("Client Information saved in this local preview.");
      return;
    }
    const localErrors = liveRecord ? {} : validateClientInformation(draft);
    const localExtraErrors = validateClientInformationExtras(extras);
    if (Object.keys(localErrors).length > 0 || Object.keys(localExtraErrors).length > 0) {
      setFieldErrors(localErrors);
      setExtraErrors(localExtraErrors);
      setError("Check the highlighted fields and try again.");
      requestAnimationFrame(() => errorSummaryRef.current?.focus());
      return;
    }
    if (createMode && createStep === 2) {
      setFieldErrors({});
      setError(null);
      if (!persistCreateDraft(3)) return;
      setCreateStep(3);
      setNotice("Pitch Information saved in this local preview.");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (createMode) {
        const created = createOpportunity(
          normalizeClientInformation(draft),
          normalizeClientInformationExtras(extras),
        );
        clearIntakeDraft(ownerId);
        router.push(`/opportunities/${encodeURIComponent(created.opportunity_id)}/discovery`);
        return;
      }
      // Live: only "Additional information" is saved (BAP-10), on its own, so the other client
      // fields keep the values the API holds; nothing else in the form is sent.
      const liveChanged = liveRecord && draft.additional_information.trim() !== currentRecord.values.additional_information.trim();
      if (liveChanged && !accessToken) throw new ApiRequestError("Sign in again to save client information.", 401);
      const saved = liveRecord
        ? liveChanged
          ? {
              ...currentRecord,
              values: {
                ...currentRecord.values,
                additional_information: await saveAdditionalInformation(accessToken!, record.opportunity_id, draft.additional_information),
              },
            }
          : currentRecord
        : await adapterRef.current.save({
            opportunity_id: record.opportunity_id,
            expected_revision: record.revision,
            values: normalizeClientInformation(draft),
          });
      if (currentOwner.current !== ownerId) return;
      setRecord(saved);
      setDraft(saved.values);
      const normalizedExtras = normalizeClientInformationExtras(extras);
      updateClient(saved, normalizedExtras);
      setExtras(normalizedExtras);
      setSavedExtras(normalizedExtras);
      setFieldErrors({});
      setExtraErrors({});
      setEditing(false);
      setNotice(liveRecord
        ? liveChanged
          ? "Additional information saved for this client. The next Discovery generation uses it; the other client fields were not changed."
          : "Nothing to save: in a live session only Additional information can be changed here."
        : "Client information and extras saved in this local preview. Files are names only, not uploaded content.");
    } catch (saveError) {
      if (saveError instanceof ClientInformationAdapterError) {
        setFieldErrors(saveError.fieldErrors);
      }
      setError(saveError instanceof ApiRequestError ? liveSaveErrorMessage(saveError) : clientInformationErrorMessage(saveError));
      requestAnimationFrame(() => errorSummaryRef.current?.focus());
    } finally {
      setBusy(false);
    }
  }

  function cancel() {
    if (record.opportunity_id === "new") {
      clearIntakeDraft(ownerId);
      router.push("/clients");
      return;
    }
    const restored = restoreClientInformationDraft(record, savedExtras);
    setDraft(restored.values);
    setExtras(restored.extras);
    setFieldErrors({});
    setExtraErrors({});
    setError(null);
    setNotice(null);
    setEditing(false);
  }

  function renderField(key: ClientInformationField, locked = false, labelOverride?: string) {
    const field = FIELDS.find((candidate) => candidate.key === key)!;
    const label = labelOverride ?? labels[key];
    const errorId = `${key}-error`;
    const controlProps = {
      id: key,
      name: key,
      value: draft[key],
      // Live: only Additional information is saved here; the other fields stay read-only.
      disabled: busy || locked || (liveRecord && key !== LIVE_EDITABLE_FIELD),
      "aria-invalid": Boolean(fieldErrors[key]),
      "aria-describedby": fieldErrors[key] ? errorId : undefined,
      onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
        updateField(key, event.target.value),
    };
    return (
      <div key={key} className={field.multiline ? "is-wide" : undefined}>
        <label htmlFor={key}>{label}</label>
        {field.multiline ? (
          <textarea {...controlProps} rows={key === "additional_information" ? 5 : 3} />
        ) : (
          <input {...controlProps} type={field.type ?? "text"} />
        )}
        {fieldErrors[key] ? <span id={errorId} className="client-field-error">{fieldErrors[key]}</span> : null}
      </div>
    );
  }

  function renderExtra(key: keyof ClientInformationExtras, locked = false) {
    const labels: Record<keyof ClientInformationExtras, string> = {
      company_logo_name: copy.clientForm.logo,
      business_industry: copy.clientForm.industry,
      contact_phone: copy.clientForm.phone,
      contact_position: copy.clientForm.position,
      pitch_notes: copy.clientForm.notes,
      pitch_file_names: copy.clientForm.pitchFiles,
      additional_opportunity_information: copy.clientForm.additionalOpportunity,
    };
    // Everything is optional; the tag stays on the fields the design already labels that way.
    const optionalTag = !["business_industry", "contact_phone", "contact_position"].includes(key);
    const props = {
      id: key,
      disabled: busy || locked || liveRecord,
      "aria-invalid": Boolean(extraErrors[key]),
      "aria-describedby": extraErrors[key] ? `${key}-error` : undefined,
    };
    return (
      <div key={key} className={key === "additional_opportunity_information" ? "is-wide" : undefined}>
        <label htmlFor={key}>{labels[key]}{optionalTag ? <> <small>{copy.clientForm.optional}</small></> : null}</label>
        {key === "company_logo_name" || key === "pitch_file_names" ? <>
          <label className={`${key === "company_logo_name" ? "client-logo-upload" : "client-pitch-upload"}${locked ? " is-disabled" : ""}`} htmlFor={key}>
            <span aria-hidden="true">+</span>
            <strong>{key === "company_logo_name" ? extras.company_logo_name || copy.clientForm.uploadLogo : copy.clientForm.addPitchFiles}</strong>
            <small>{key === "company_logo_name" ? "PNG, JPG or SVG · max 5 MB" : extras.pitch_file_names.join(", ") || "PDF, DOC or DOCX files"}</small>
          </label>
          {key === "company_logo_name" ?
            <input {...props} className="sr-only" type="file" accept=".png,.jpg,.jpeg,.svg" onChange={selectLogo} /> :
            <input {...props} className="sr-only" type="file" accept=".pdf,.doc,.docx" multiple onChange={selectPitchFiles} />}
          <small className="client-input-help">Local preview stores file names only, not uploaded content.</small>
          {(key === "company_logo_name" ? Boolean(extras.company_logo_name) : extras.pitch_file_names.length > 0) ?
            <button type="button" className="btn btn-secondary" disabled={busy || locked} onClick={() => key === "company_logo_name" ? updateExtra(key, "") : updateExtra(key, [])}>Clear selection</button> : null}
        </> : key === "business_industry" ? (
          <select {...props} value={extras[key]} onChange={(event) => updateExtra(key, event.target.value)}>
            <option value="">{copy.clientForm.selectIndustry}</option>
            {extras[key] && !INDUSTRIES.some((industry) => industry === extras[key]) ? <option>{extras[key]}</option> : null}
            {INDUSTRIES.map((industry) => <option key={industry}>{industry}</option>)}
          </select>
        ) : key === "pitch_notes" || key === "additional_opportunity_information" ? (
          <textarea {...props} rows={4} value={extras[key]} placeholder={key === "pitch_notes" ? copy.clientForm.notesPlaceholder : undefined} onChange={(event) => updateExtra(key, event.target.value)} />
        ) : (
          <input {...props} type={key === "contact_phone" ? "tel" : "text"} value={extras[key]} placeholder={key === "contact_phone" ? "+49 30 1234 5678" : copy.clientForm.positionPlaceholder} onChange={(event) => updateExtra(key, event.target.value)} />
        )}
        {key === "contact_phone" ? <small className="client-input-help">{copy.clientForm.countryCode}</small> : null}
        {extraErrors[key] ? <span id={`${key}-error`} className="client-field-error">{extraErrors[key]}</span> : null}
      </div>
    );
  }

  if (authLoading || !hydrated || draftOwner !== ownerId) return <p role="status">Loading client information...</p>;
  if (!ownerId) return <p role="status">Sign in to view client information.</p>;

  if (createMode) {
    const stepClass = (step: 1 | 2 | 3) =>
      createStep === step ? "is-current" : createStep > step ? "is-complete" : undefined;
    return (
      <section className="premeeting-create-flow" aria-labelledby="premeeting-create-title">
        <header className="premeeting-intro">
          <p className="premeeting-breadcrumb">{copy.clientForm.breadcrumb}</p>
          <h1 id="premeeting-create-title">{copy.clientForm.createTitle}</h1>
          <p>{copy.clientForm.createLead}</p>
        </header>
        <ol className="premeeting-create-steps" aria-label={copy.clientForm.preMeetingTitle}>
          <li className={stepClass(1)}><span>1</span><strong>{copy.clientForm.stepClient}</strong></li>
          <li className={stepClass(2)}><span>2</span><strong>{copy.clientForm.stepPitch}</strong></li>
          <li className={stepClass(3)}><span>3</span><strong>{copy.clientForm.stepGenerate}</strong></li>
        </ol>
        {error ? (
          <div className="client-information-error" role="alert" tabIndex={-1} ref={errorSummaryRef}>
            <strong>Client information was not saved.</strong><span>{error}</span>
          </div>
        ) : null}
        {notice ? <p className="client-information-notice" role="status" aria-live="polite">{notice}</p> : null}
        <div className="premeeting-create-grid">
          <form id="client-information-form" className="discovery-client-information is-create client-information-form" onSubmit={(event) => void save(event)} noValidate>
            <header>
              <p>{String(createStep).padStart(2, "0")}</p>
              <h2>{createStep === 1 ? copy.clientForm.informationSection : createStep === 2 ? copy.clientForm.stepPitch : copy.clientForm.reviewTitle}</h2>
            </header>
            {createStep === 1 ? <>
              <fieldset className="client-form-section">
              <legend>{copy.clientForm.companySection}</legend>
              <div className="client-form-section-grid">
                {renderField("company_name", createStep > 1)}
                {renderField("website_url", createStep > 1)}
                {renderExtra("company_logo_name", createStep > 1)}
                {renderExtra("business_industry", createStep > 1)}
              </div>
              </fieldset>
              <fieldset className="client-form-section is-muted">
              <legend>{copy.clientForm.contactSection}</legend>
              <p className="client-section-help">{copy.clientForm.contactHelp}</p>
              <div className="client-form-section-grid">
                {renderField("contact_person", createStep > 1)}
                {renderExtra("contact_position", createStep > 1)}
                {renderExtra("contact_phone", createStep > 1)}
                {renderField("additional_information", createStep > 1, copy.clientForm.relevantInformation)}
              </div>
              </fieldset>
            </> : null}
            {createStep === 2 ? (
              <fieldset className="client-form-section premeeting-pitch-section">
                <legend>{copy.clientForm.stepPitch}</legend>
                <div className="client-form-section-grid premeeting-pitch-fields">
                  {renderField("meeting_purpose", createStep > 2, copy.clientForm.salesOpportunity)}
                  {renderExtra("pitch_notes", createStep > 2)}
                  {renderExtra("pitch_file_names", createStep > 2)}
                  {renderExtra("additional_opportunity_information", createStep > 2)}
                </div>
              </fieldset>
            ) : null}
            {createStep === 3 ? (
              <section className="premeeting-generate-review">
                <h3>{copy.clientForm.reviewTitle}</h3><p>{copy.clientForm.reviewLead}</p>
              </section>
            ) : null}
            <div className="client-information-form-actions">
              <span className="client-required-note">{copy.clientForm.allOptional}</span>
              <button className="btn btn-secondary" type="button" onClick={() => createStep === 1 ? cancel() : setCreateStep((createStep - 1) as 1 | 2)}>
                {createStep === 1 ? copy.clientForm.cancel : copy.clientForm.backStep}
              </button>
              <button className="btn btn-primary" type="submit" disabled={busy}>
                {busy ? copy.clientForm.saving : createStep === 1 ? copy.clientForm.save : createStep === 2 ? copy.clientForm.savePitch : copy.clientForm.generatePitch}
              </button>
            </div>
          </form>
          <aside className="premeeting-create-summary">
            <p>{copy.clientForm.oneFlow}</p><h2>{copy.clientForm.createSummary}</h2>
            <ol>
              <li><span>1</span><div><strong>{copy.clientForm.clientProfile}</strong><small>{createStep > 1 ? copy.clientForm.created : copy.workflow.current}</small></div></li>
              <li><span>2</span><div><strong>{copy.clientForm.firstPitch}</strong><small>{createStep > 2 ? copy.clientForm.created : createStep === 2 ? copy.workflow.current : copy.workflow.unavailable}</small></div></li>
              <li><span>3</span><div><strong>{copy.clientForm.pitchGeneration}</strong><small>{createStep === 3 ? copy.clientForm.startsAutomatically : copy.workflow.unavailable}</small></div></li>
            </ol>
            <button className="btn btn-primary" type="submit" form="client-information-form" disabled={createStep !== 3 || busy}>{copy.clientForm.generatePitch}</button>
            <small>{copy.clientForm.generationTime}</small>
          </aside>
        </div>
      </section>
    );
  }

  return (
    <section className={`discovery-client-information${createMode ? " is-create" : ""}`} aria-labelledby="client-information-title">
      <header>
        <p>{createMode ? "01" : copy.clientForm.editKicker}</p>
        <h1 id="client-information-title">{createMode ? copy.clientForm.informationSection : copy.clientForm.editTitle}</h1>
        {!createMode ? <span>{copy.clientForm.editLead}</span> : null}
      </header>

      {error ? (
        <div className="client-information-error" role="alert" tabIndex={-1} ref={errorSummaryRef}>
          <strong>Client information was not saved.</strong>
          <span>{error}</span>
        </div>
      ) : null}
      {notice ? <p className="client-information-notice" role="status">{notice}</p> : null}
      {liveRecord || !canEdit ? <p role="status">{liveRecord
        ? "In a live session only Additional information can be changed here; it is saved for this client and used by the next Discovery generation. The other client fields are read-only: the backend update contract cannot safely round-trip all five fields with revision checks. Extras are local preview metadata, not live-saved data."
        : "This opportunity is not available in your local preview. Editing is disabled."}</p> : null}

      {editing && canEdit ? (
        <form id="client-information-form" className="client-information-form" onSubmit={(event) => void save(event)} noValidate>
          <fieldset className="client-form-section">
            <legend>{copy.clientForm.companySection}</legend>
            <div className="client-form-section-grid">{renderField("company_name")}{renderField("website_url")}{renderExtra("company_logo_name")}{renderExtra("business_industry")}</div>
          </fieldset>
          <fieldset className="client-form-section is-muted">
            <legend>{copy.clientForm.contactSection}</legend>
            <div className="client-form-section-grid">{renderField("contact_person")}{renderExtra("contact_position")}{renderExtra("contact_phone")}</div>
          </fieldset>
          <fieldset className="client-form-section">
            <legend>{copy.clientForm.meetingSection}</legend>
            <div className="client-form-section-grid">{renderField("meeting_purpose")}{renderField("additional_information")}</div>
          </fieldset>
          <fieldset className="client-form-section premeeting-pitch-section">
            <legend>{copy.clientForm.stepPitch}</legend>
            <div className="client-form-section-grid">{renderExtra("pitch_notes")}{renderExtra("pitch_file_names")}{renderExtra("additional_opportunity_information")}</div>
          </fieldset>
          <div className="client-information-form-actions">
            {createMode ? <span className="client-required-note">{copy.clientForm.allOptional}</span> : null}
            <button className="btn btn-secondary" type="button" disabled={busy} onClick={cancel}>{copy.clientForm.cancel}</button>
            {!createMode ? <button className="btn btn-primary" type="submit" disabled={busy}>{busy ? copy.clientForm.saving : copy.clientForm.save}</button> : null}
          </div>
        </form>
      ) : (
        <dl>
          {FIELDS.map(({ key }) => (
            <div key={key}>
              <dt>{labels[key]}</dt>
              <dd>{record.values[key] || "Not provided"}</dd>
            </div>
          ))}
          {([
            [copy.clientForm.logo, savedExtras.company_logo_name],
            [copy.clientForm.industry, savedExtras.business_industry],
            [copy.clientForm.position, savedExtras.contact_position],
            [copy.clientForm.phone, savedExtras.contact_phone],
            [copy.clientForm.notes, savedExtras.pitch_notes],
            [copy.clientForm.pitchFiles, savedExtras.pitch_file_names.join(", ")],
            [copy.clientForm.additionalOpportunity, savedExtras.additional_opportunity_information],
          ]).map(([label, value]) => <div key={label}><dt>{label} (local preview)</dt><dd>{value || "Not provided"}</dd></div>)}
        </dl>
      )}

      {!editing ? (
        <WorkflowActionBar
          backHref="/clients"
          backLabel={copy.clientForm.back}
          contextLabel={copy.clientForm.revision}
          context={`Revision ${record.revision}`}
        >
          <button className="btn btn-secondary" type="button" disabled={busy || !canEdit} onClick={() => void reload()}>{copy.clientForm.reload}</button>
          <button className="btn btn-secondary" type="button" disabled={busy || !canEdit} onClick={() => setEditing(true)}>{copy.clientForm.edit}</button>
          <button className="btn btn-primary" type="button" onClick={() => router.push(`/opportunities/${encodeURIComponent(record.opportunity_id)}/discovery`)}>{copy.clientForm.continue}</button>
        </WorkflowActionBar>
      ) : null}
    </section>
  );
}
