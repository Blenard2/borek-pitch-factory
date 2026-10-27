import type { EmployeeMe } from "./employeeRoles";
import { getSupabaseBrowserClient } from "./supabase";
import { getAuthOwnerId } from "./authSession";
import { BACKEND_UUID, backendOpportunityMapKey, readBackendOpportunityMap as readScopedBackendMap } from "./backendOpportunityMap";

const DEFAULT_API_URL = "http://localhost:8000";

export function getApiBaseUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || DEFAULT_API_URL;
}

async function resolveAccessToken(fallback: string): Promise<string> {
  const client = getSupabaseBrowserClient();
  const session = client ? (await client.auth.getSession()).data.session : null;
  return session?.access_token ?? fallback;
}

export interface ApiErrorBody {
  error?: {
    code?: string;
    message?: string;
  };
}

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly retryable?: boolean;
  readonly jobId?: string;
  readonly stage?: string;

  constructor(
    message: string,
    status: number,
    code?: string,
    extras?: { retryable?: boolean; jobId?: string; stage?: string },
  ) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
    this.retryable = extras?.retryable;
    this.jobId = extras?.jobId;
    this.stage = extras?.stage;
  }
}

export interface JobErrorDetail {
  code: string;
  message: string;
  stage: string;
  retryable: boolean;
}

export interface JobResponse {
  job_id: string;
  job_type: string;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
  current_stage: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  result: Record<string, unknown>;
  error: JobErrorDetail | null;
}

export interface ActiveJobResponse {
  job_id: string;
  job_type: string;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
  current_stage: string;
  started_at: string | null;
  error: JobErrorDetail | null;
}

export interface JobEnqueueResponse {
  job_id: string;
  status: string;
  is_existing_job?: boolean;
}

async function parseError(response: Response): Promise<ApiRequestError> {
  let message = `Request failed (${response.status})`;
  let code: string | undefined;
  try {
    const body = (await response.json()) as ApiErrorBody;
    if (body.error?.message) message = body.error.message;
    code = body.error?.code;
  } catch {
    // Keep the status message when the body is not JSON.
  }
  return new ApiRequestError(message, response.status, code);
}

export async function apiFetch<T>(
  path: string,
  accessToken: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${await resolveAccessToken(accessToken)}`);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${getApiBaseUrl()}${path}`, { ...init, headers });
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function apiFetchBlob(
  path: string,
  accessToken: string,
  init: RequestInit = {},
): Promise<Blob> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${await resolveAccessToken(accessToken)}`);
  const response = await fetch(`${getApiBaseUrl()}${path}`, { ...init, headers });
  if (!response.ok) throw await parseError(response);
  return response.blob();
}

export async function getJob(accessToken: string, jobId: string): Promise<JobResponse> {
  return apiFetch<JobResponse>(`/jobs/${jobId}`, accessToken);
}

export async function getActiveJob(
  accessToken: string,
  opportunityId: string,
  stageGroup?: "framework" | "presentation",
): Promise<ActiveJobResponse | null> {
  const query = stageGroup ? `?stage_group=${stageGroup}` : "";
  try {
    return await apiFetch<ActiveJobResponse>(
      `/opportunities/${resolveBackendOpportunityId(opportunityId)}/jobs/active${query}`,
      accessToken,
    );
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 404) return null;
    throw error;
  }
}

export const JOB_POLL_INTERVAL_MS = 500;
export const JOB_TIMEOUT_MS = 720_000;

export interface WaitForJobOptions {
  timeoutMs?: number;
  pollIntervalMs?: number;
  onProgress?: (job: JobResponse) => void;
  /** Stops polling when the page that follows the job goes away. */
  signal?: AbortSignal;
}

export async function waitForJob(
  accessToken: string,
  jobId: string,
  options: number | WaitForJobOptions = {},
): Promise<JobResponse> {
  const { timeoutMs = JOB_TIMEOUT_MS, pollIntervalMs = JOB_POLL_INTERVAL_MS, onProgress, signal } =
    typeof options === "number" ? { timeoutMs: options, signal: undefined } : options;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    signal?.throwIfAborted();
    const job = await getJob(accessToken, jobId);
    signal?.throwIfAborted();
    onProgress?.(job);
    if (job.status === "COMPLETED") return job;
    if (job.status === "FAILED") {
      throw new ApiRequestError(job.error?.message || "Generation failed.", 422, job.error?.code, {
        retryable: Boolean(job.error?.retryable),
        jobId: job.job_id,
        stage: job.error?.stage,
      });
    }
    await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
  }
  throw new ApiRequestError("Generation job timed out", 408, "JOB_TIMEOUT", { jobId });
}

export interface Stage1PresentationRef {
  status: string;
  presentation_id: string | null;
  code?: string | null;
}

export interface Stage1OutputsEnvelope {
  outputs?: {
    presentation?: Stage1PresentationRef;
  };
}

export interface WorkflowDeckLine {
  presentation_id: string;
  latest_ready_version_id: string | null;
  journey_stage: string;
  status: string;
  /** Set for a Master Presentation: "V1" (pre-meeting) or "V2" (post-meeting), versions of one presentation. */
  product_version?: "V1" | "V2";
}

export interface WorkflowStatusResponse {
  current_status?: string;
  documents?: {
    ppt1: WorkflowDeckLine | null;
    ppt2: WorkflowDeckLine | null;
  };
}

export interface Ppt2GenerateResponse {
  job_id: string;
  status: string;
  presentation_id: string;
  presentation_version_id: string | null;
  journey_stage: "post_meeting";
  is_existing_job?: boolean;
}

export interface TranscriptUploadResult {
  transcript: { id: string };
}

export interface AvailableUseCase {
  fact_id: string;
  statement: string | null;
}

function opportunityPath(opportunityId: string): string {
  return `/opportunities/${resolveBackendOpportunityId(opportunityId)}`;
}

/** BAP-12: the language client documents are generated in ("en" | "de"); see documentLanguage.ts. */
export type GenerationLanguage = "en" | "de";

/** The JSON body naming the generation language, or no body when none is given. */
function generationBody(language: GenerationLanguage | null | undefined, extra: Record<string, unknown> = {}): string | undefined {
  const body = language === "en" || language === "de" ? { ...extra, language } : extra;
  return Object.keys(body).length ? JSON.stringify(body) : undefined;
}

export interface PreviewClientSeed {
  company_name: string;
  contact_person?: string;
  website_url?: string;
  meeting_purpose?: string;
  additional_information?: string;
}

const UUID_RE = BACKEND_UUID;

function readBackendOpportunityMap(): Record<string, string> {
  const owner = getAuthOwnerId();
  if (typeof window === "undefined" || !owner) return {};
  try {
    return readScopedBackendMap(window.localStorage, owner, getApiBaseUrl());
  } catch {
    return {};
  }
}

/**
 * The web app creates local preview opportunities with ids like "opp-acme-1a2b3c4d".
 * The API only knows UUID opportunities, so create the backend row once and remember it.
 */
export async function ensureBackendOpportunityId(
  accessToken: string,
  opportunityId: string,
  seed?: PreviewClientSeed | null,
  language?: GenerationLanguage | null,
): Promise<string> {
  if (UUID_RE.test(opportunityId)) return opportunityId;
  const owner = getAuthOwnerId();
  if (!owner) throw new ApiRequestError("Sign in before synchronizing this opportunity.", 401, "AUTH_OWNER_REQUIRED");
  const map = readBackendOpportunityMap();
  if (map[opportunityId]) return map[opportunityId];
  // The company name is optional; only a missing record (nothing to send at all) blocks the sync.
  if (!seed) {
    throw new ApiRequestError(
      "This opportunity exists only in the local preview and has no client information to send to the API.",
      422,
      "PREVIEW_OPPORTUNITY_NOT_SYNCED",
    );
  }
  const website = (seed.website_url ?? "").trim();
  const validWebsite = /^https?:\/\//i.test(website) ? website : null;
  const created = await apiFetch<{ id: string }>("/opportunities", accessToken, {
    method: "POST",
    body: JSON.stringify({
      client_name: seed.company_name,
      opportunity_name: seed.meeting_purpose?.trim() || seed.company_name,
      department: "Sales",
      // BAP-12: the document language chosen for this client is stored with it from the start.
      ...(language === "en" || language === "de" ? { language } : {}),
      stage1_intake: {
        client_web_page: validWebsite,
        poc_name: seed.contact_person?.trim() || null,
        sales_topic_description: seed.meeting_purpose?.trim() || null,
        about_company: seed.additional_information?.trim() || null,
      },
    }),
  });
  if (!UUID_RE.test(created.id)) throw new ApiRequestError("The API returned an invalid opportunity ID.", 502);
  if (owner !== getAuthOwnerId()) throw new ApiRequestError("The signed-in owner changed during synchronization.", 401);
  map[opportunityId] = created.id;
  window.localStorage.setItem(backendOpportunityMapKey(owner, getApiBaseUrl()), JSON.stringify(map));
  return created.id;
}

/** The local preview id this browser created for a backend opportunity, if it created it. */
export function previewAliasForBackendId(backendId: string): string | null {
  const entry = Object.entries(readBackendOpportunityMap()).find(([, value]) => value === backendId);
  return entry ? entry[0] : null;
}

/**
 * The path with the backend opportunity id in it, or null when it already has one (or the
 * opportunity does not exist on the server yet). A path with a local preview id only works in the
 * browser that created it; the backend id works for every signed-in user who may open it.
 */
export function canonicalOpportunityPath(pathname: string, opportunityId: string): string | null {
  if (UUID_RE.test(opportunityId)) return null;
  const backendId = resolveBackendOpportunityId(opportunityId);
  const prefix = `/opportunities/${opportunityId}`;
  if (!UUID_RE.test(backendId) || !(pathname === prefix || pathname.startsWith(`${prefix}/`))) return null;
  return `/opportunities/${backendId}${pathname.slice(prefix.length)}`;
}

/** Map a local preview id to its backend UUID (created during Discovery generation). */
export function resolveBackendOpportunityId(opportunityId: string): string {
  if (UUID_RE.test(opportunityId)) return opportunityId;
  return readBackendOpportunityMap()[opportunityId] ?? opportunityId;
}

/**
 * BAP-10: save the "Additional information" of a live client (stage1_intake.about_company) on
 * its own; the other client fields keep their stored values. Blank clears it. Returns the stored
 * value as the API saved it.
 */
export async function saveAdditionalInformation(
  accessToken: string,
  opportunityId: string,
  text: string,
): Promise<string> {
  const row = await apiFetch<{ stage1_intake?: { about_company?: string | null } | null }>(
    `${opportunityPath(opportunityId)}/additional-information`,
    accessToken,
    { method: "PUT", body: JSON.stringify({ additional_information: text.trim() || null }) },
  );
  return row.stage1_intake?.about_company ?? "";
}

export async function generateDiscoveryPaper(
  accessToken: string,
  opportunityId: string,
  seed?: PreviewClientSeed | null,
  language?: GenerationLanguage | null,
): Promise<Record<string, unknown>> {
  const backendId = await ensureBackendOpportunityId(accessToken, opportunityId, seed, language);
  // BAP-12: Discovery 1 is written in the chosen language; the server also stores it on the client.
  return apiFetch(`${opportunityPath(backendId)}/discovery-paper/generate`, accessToken, {
    method: "POST",
    body: generationBody(language),
  });
}

export async function approveDiscoveryPaper(
  accessToken: string,
  opportunityId: string,
  seed?: PreviewClientSeed | null,
): Promise<Record<string, unknown>> {
  const backendId = await ensureBackendOpportunityId(accessToken, opportunityId, seed);
  return apiFetch(`${opportunityPath(backendId)}/discovery-paper/approve`, accessToken, {
    method: "POST",
  });
}

export async function getStage1Outputs(
  accessToken: string,
  opportunityId: string,
): Promise<Stage1OutputsEnvelope> {
  return apiFetch(`${opportunityPath(opportunityId)}/stage1-outputs`, accessToken);
}

/** Explicitly build a new presentation version from the latest approved Discovery. */
export async function regenerateStage1Presentation(
  accessToken: string,
  opportunityId: string,
  language?: GenerationLanguage | null,
): Promise<Stage1OutputsEnvelope> {
  // BAP-12: naming the language lets the server refuse a deck against a Discovery in another language.
  return apiFetch(`${opportunityPath(opportunityId)}/stage1-outputs/presentation/regenerate`, accessToken, {
    method: "POST",
    body: generationBody(language),
  });
}

export async function generateStage1Outputs(
  accessToken: string,
  opportunityId: string,
  language?: GenerationLanguage | null,
): Promise<Stage1OutputsEnvelope> {
  return apiFetch(`${opportunityPath(opportunityId)}/stage1-outputs/generate`, accessToken, {
    method: "POST",
    body: generationBody(language),
  });
}

export async function getWorkflowStatus(
  accessToken: string,
  opportunityId: string,
): Promise<WorkflowStatusResponse> {
  return apiFetch(`${opportunityPath(opportunityId)}/workflow-status`, accessToken);
}

export async function markFirstMeetingCompleted(
  accessToken: string,
  opportunityId: string,
): Promise<WorkflowStatusResponse> {
  return apiFetch(`${opportunityPath(opportunityId)}/workflow/first-meeting-completed`, accessToken, {
    method: "POST",
  });
}

export async function uploadTranscript(
  accessToken: string,
  opportunityId: string,
  file: File,
): Promise<TranscriptUploadResult> {
  const body = new FormData();
  body.set("file", file);
  return apiFetch(`${opportunityPath(opportunityId)}/transcripts`, accessToken, {
    method: "POST",
    body,
  });
}

export async function savePersonalNotes(
  accessToken: string,
  opportunityId: string,
  text: string,
): Promise<{ text: string | null }> {
  return apiFetch(`${opportunityPath(opportunityId)}/personal-notes`, accessToken, {
    method: "PUT",
    body: JSON.stringify({ text }),
  });
}

export async function generateMeetingExtraction(
  accessToken: string,
  opportunityId: string,
  transcriptId: string,
): Promise<Record<string, unknown>> {
  return apiFetch(`${opportunityPath(opportunityId)}/meeting-extraction/generate`, accessToken, {
    method: "POST",
    body: JSON.stringify({ transcript_id: transcriptId }),
  });
}

export async function listAvailableUseCases(
  accessToken: string,
  opportunityId: string,
): Promise<{ use_cases: AvailableUseCase[] }> {
  return apiFetch(`${opportunityPath(opportunityId)}/available-use-cases`, accessToken);
}

export async function saveSelectedUseCases(
  accessToken: string,
  opportunityId: string,
  useCaseIds: string[],
): Promise<Record<string, unknown>> {
  return apiFetch(`${opportunityPath(opportunityId)}/selected-use-cases`, accessToken, {
    method: "PUT",
    body: JSON.stringify({ use_case_ids: useCaseIds }),
  });
}

export async function generatePpt2(
  accessToken: string,
  opportunityId: string,
): Promise<Ppt2GenerateResponse> {
  return apiFetch(`${opportunityPath(opportunityId)}/ppt2/generate`, accessToken, { method: "POST" });
}

export async function regeneratePpt2(
  accessToken: string,
  opportunityId: string,
  presentationId: string,
): Promise<Ppt2GenerateResponse> {
  return apiFetch(
    `${opportunityPath(opportunityId)}/ppt2/${presentationId}/regenerate`,
    accessToken,
    { method: "POST" },
  );
}

export async function markOwnerReviewed(
  accessToken: string,
  opportunityId: string,
): Promise<WorkflowStatusResponse> {
  return apiFetch(`${opportunityPath(opportunityId)}/workflow/owner-reviewed`, accessToken, {
    method: "POST",
  });
}

export async function finalizeWorkflow(
  accessToken: string,
  opportunityId: string,
): Promise<WorkflowStatusResponse> {
  return apiFetch(`${opportunityPath(opportunityId)}/workflow/finalize`, accessToken, {
    method: "POST",
  });
}

export type OwnerEmailStage = "first_contact" | "deepening";

export async function generateEmailDraft(
  accessToken: string,
  opportunityId: string,
  journeyStage: OwnerEmailStage,
): Promise<Record<string, unknown>> {
  return apiFetch(`${opportunityPath(opportunityId)}/email-drafts/generate`, accessToken, {
    method: "POST",
    body: JSON.stringify({ journey_stage: journeyStage }),
  });
}

export async function downloadPresentationFile(
  accessToken: string,
  downloadPath: string,
): Promise<Blob> {
  return apiFetchBlob(downloadPath, accessToken);
}

export async function fetchSlidePreviewBlob(
  accessToken: string,
  previewPath: string,
): Promise<Blob> {
  return apiFetchBlob(previewPath, accessToken);
}

async function employeeRequest(path: string, token: string, method = "GET"): Promise<EmployeeMe> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    method,
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${await resolveAccessToken(token)}`,
    },
  });
  if (!response.ok) throw await parseError(response);
  return response.json() as Promise<EmployeeMe>;
}

export function getEmployeeMe(token: string): Promise<EmployeeMe> {
  return employeeRequest("/employees/me", token);
}

export function recordEmployeeSession(token: string): Promise<EmployeeMe> {
  return employeeRequest("/employees/session", token, "POST");
}
