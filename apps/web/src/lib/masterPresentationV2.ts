import { ApiRequestError, apiFetch, resolveBackendOpportunityId, waitForJob, type JobResponse } from "./api";

/**
 * Master Presentation V2: the post-meeting version of the SAME presentation as V1.
 * Readiness is decided by the API; this module never infers it.
 */
export type MasterV2State = "none" | "generating" | "ready" | "failed" | "outdated";

export interface MasterV2Status {
  opportunity_id: string;
  product_version: "V2";
  presentation_id: string | null;
  state: MasterV2State;
  can_generate: boolean;
  ready_for_v2: boolean;
  blockers: string[];
  finalized: boolean;
  latest_ready: { presentation_version_id: string; version_number: number; generation_fingerprint: string | null; meeting_review_confirmed_at?: string | null } | null;
  job: { job_id: string; status: string; error_code: string | null; error_message?: string | null } | null;
}

export interface MasterV2Started {
  presentation_id: string;
  presentation_version_id: string | null;
  product_version: "V2";
  status: string;
  job_id: string | null;
  is_existing_job: boolean;
  is_existing_version: boolean;
}

const STATES: MasterV2State[] = ["none", "generating", "ready", "failed", "outdated"];

function path(opportunityId: string, suffix = "") {
  return `/opportunities/${encodeURIComponent(resolveBackendOpportunityId(opportunityId))}/master-presentation/v2${suffix}`;
}

export function parseMasterV2Status(value: unknown, opportunityId: string): MasterV2Status {
  const record = value as Partial<MasterV2Status> | null;
  if (!record || typeof record !== "object" || record.opportunity_id !== resolveBackendOpportunityId(opportunityId) ||
    record.product_version !== "V2" || !STATES.includes(record.state as MasterV2State) ||
    typeof record.can_generate !== "boolean" || !Array.isArray(record.blockers) ||
    (record.state === "ready" && !record.latest_ready?.presentation_version_id)) {
    throw new Error("The Master Presentation V2 status is incomplete or belongs to another opportunity. Reload to retry.");
  }
  return record as MasterV2Status;
}

export async function loadMasterV2Status(token: string, opportunityId: string, signal?: AbortSignal) {
  const value = await apiFetch<unknown>(path(opportunityId), token, { signal, cache: "no-store" });
  return parseMasterV2Status(value, opportunityId);
}

/**
 * Starts (or rejoins) the V2 generation and waits for its job. Resolves with the status the API
 * reports afterwards; the caller shows that, never an assumption about the job.
 */
export async function generateAndAwaitMasterV2(
  token: string, opportunityId: string, onJob?: (job: JobResponse) => void, signal?: AbortSignal, language?: "en" | "de",
): Promise<MasterV2Status> {
  // BAP-12: naming the language makes the server refuse a V2 against a Discovery 2 in another language.
  const started = await apiFetch<MasterV2Started>(path(opportunityId, "/generate"), token, {
    method: "POST", signal, ...(language ? { body: JSON.stringify({ language }) } : {}),
  });
  if (started.product_version !== "V2" || !started.presentation_id) {
    throw new ApiRequestError("The API did not start a Master Presentation V2.", 409, "MASTER_V2_IDENTITY_MISMATCH");
  }
  if (started.job_id && started.status !== "ready" && started.status !== "COMPLETED") {
    await waitForJob(token, started.job_id, { onProgress: (job) => { signal?.throwIfAborted(); onJob?.(job); } });
  }
  signal?.throwIfAborted();
  const status = await loadMasterV2Status(token, opportunityId, signal);
  if (status.presentation_id !== started.presentation_id) {
    throw new ApiRequestError("Master Presentation V2 was generated for another presentation.", 409, "MASTER_V2_IDENTITY_MISMATCH");
  }
  if (status.state !== "ready") {
    throw new ApiRequestError(
      status.job?.error_message || "Master Presentation V2 was not generated. Retry the generation.", 409, status.job?.error_code ?? "MASTER_V2_NOT_READY",
    );
  }
  return status;
}

const pause = (ms: number, signal?: AbortSignal) => new Promise<void>((resolve, reject) => {
  const timer = setTimeout(resolve, ms);
  signal?.addEventListener("abort", () => { clearTimeout(timer); reject(signal.reason); }, { once: true });
});

/**
 * Follows a V2 generation that is ALREADY running - started in another tab, before a reload, or
 * before the user left the page - until the server reports a final state. It only reads: it never
 * starts a generation, so reopening the page cannot create a second one.
 */
export async function awaitRunningMasterV2(
  token: string, opportunityId: string, status: MasterV2Status, onJob?: (job: JobResponse) => void, signal?: AbortSignal, pollMs = 2000,
): Promise<MasterV2Status> {
  let current = status;
  while (current.state === "generating") {
    signal?.throwIfAborted();
    const jobId = current.job?.job_id;
    if (jobId) {
      // A failed job is not an error here: the status read below says what the server holds.
      try { await waitForJob(token, jobId, { onProgress: onJob, signal }); } catch (error) { signal?.throwIfAborted(); if (!(error instanceof ApiRequestError)) throw error; }
    } else {
      await pause(pollMs, signal);
    }
    signal?.throwIfAborted();
    const next = await loadMasterV2Status(token, opportunityId, signal);
    if (next.state === "generating" && next.job?.job_id === jobId) await pause(pollMs, signal);
    current = next;
  }
  return current;
}

const STAGE_TEXT: Record<string, string> = {
  QUEUED: "Waiting for the generation to start",
  SLIDE_GENERATING: "Building the post-meeting appendix",
  SLIDE_VALIDATING: "Checking the slides",
  PPTX_RENDERING: "Assembling the presentation and rendering the PDF",
  ARTIFACT_FILING: "Filing the presentation",
  PREVIEW_RENDERING: "Preparing the slide previews",
};

export const masterV2StageText = (stage: string | null | undefined) => STAGE_TEXT[String(stage ?? "")] ?? "Generating Master Presentation V2";

export function masterV2Error(error: unknown): string {
  if (error instanceof ApiRequestError && error.code === "MASTER_V2_NOT_READY") return error.message;
  if (error instanceof ApiRequestError && error.code === "MASTER_V2_SOURCES_CHANGED") {
    return "A source changed while the presentation was being prepared. Review the meeting information and generate again.";
  }
  if (error instanceof ApiRequestError && error.code === "PRESENTATION_GENERATION_IN_PROGRESS") {
    return "Another presentation is being generated for this pitch. Wait until it has finished, then retry.";
  }
  if (error instanceof ApiRequestError && error.code === "WORKFLOW_FINALIZED") return "This package is finalized. A new Master Presentation V2 cannot be generated.";
  return error instanceof Error ? `Master Presentation V2 could not be generated: ${error.message}` : "Master Presentation V2 could not be generated. Please retry.";
}

/** Label of a deck in the post-meeting slot: Master Presentation V2, or the earlier standalone PPT #2. */
export function postMeetingDeckLabel(productVersion: string | null | undefined) {
  return productVersion === "V2" ? "Master Presentation V2" : "PPT #2";
}
