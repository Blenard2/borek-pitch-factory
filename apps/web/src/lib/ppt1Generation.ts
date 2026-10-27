import {
  ApiRequestError,
  generateStage1Outputs,
  getActiveJob,
  getJob,
  getStage1Outputs,
  getWorkflowStatus,
  regenerateStage1Presentation,
  resolveBackendOpportunityId,
  waitForJob,
  type GenerationLanguage,
  type JobResponse,
  type Stage1OutputsEnvelope,
} from "./api";

export interface FirstPitchResult {
  presentationId: string;
  presentationVersionId: string;
  jobId: string | null;
}

function presentationOf(envelope: Stage1OutputsEnvelope) {
  return envelope.outputs?.presentation;
}

const PRODUCT = "Master Presentation V1";
const RUNNING = new Set(["queued", "generating"]);

/**
 * Follows a pre-meeting presentation generation that is ALREADY running - started before a reload
 * or before the user left the page - until it is ready or has failed. It only reads the job and
 * the status and never starts a generation. Resolves with null when nothing is running.
 */
export async function resumeFirstPitch(
  accessToken: string,
  previewOrBackendId: string,
  onJob?: (job: JobResponse) => void,
  signal?: AbortSignal,
  pollMs = 2000,
): Promise<FirstPitchResult | null> {
  const opportunityId = resolveBackendOpportunityId(previewOrBackendId);
  let jobId: string | null = null;
  for (;;) {
    signal?.throwIfAborted();
    const active = await getActiveJob(accessToken, opportunityId, "presentation");
    if (active?.job_id && active.status !== "COMPLETED" && active.status !== "FAILED") {
      jobId = active.job_id;
      await waitForJob(accessToken, active.job_id, { onProgress: onJob, signal });
    }
    signal?.throwIfAborted();
    const presentation = presentationOf(await getStage1Outputs(accessToken, opportunityId));
    const workflow = await getWorkflowStatus(accessToken, opportunityId);
    const ppt1 = workflow.documents?.ppt1;
    if (presentation?.status === "failed" || ppt1?.status === "failed") {
      throw new ApiRequestError(`${PRODUCT} generation failed: ${presentation?.code ?? "unknown error"}`, 409, presentation?.code ?? "PPT1_FAILED");
    }
    if (ppt1?.presentation_id && ppt1.latest_ready_version_id && !RUNNING.has(String(ppt1.status))) {
      return { presentationId: ppt1.presentation_id, presentationVersionId: ppt1.latest_ready_version_id, jobId };
    }
    if (!RUNNING.has(String(ppt1?.status)) && !RUNNING.has(String(presentation?.status))) return null;
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, pollMs);
      signal?.addEventListener("abort", () => { clearTimeout(timer); reject(signal.reason); }, { once: true });
    });
  }
}

/**
 * Generate Master Presentation V1 from the approved Discovery paper and poll until that deck is ready.
 * This uses the Stage 1 output path, not the retired intake-stamp presentation generate.
 */
export async function generateAndAwaitFirstPitch(
  accessToken: string,
  previewOrBackendId: string,
  onJob?: (job: JobResponse) => void,
  options: { regenerate?: boolean; language?: GenerationLanguage } = {},
): Promise<FirstPitchResult> {
  const opportunityId = resolveBackendOpportunityId(previewOrBackendId);
  // Regenerating asks for a new version from the latest approved Discovery; the server still
  // returns the existing ready deck when the source has not changed.
  const started = options.regenerate
    ? await regenerateStage1Presentation(accessToken, opportunityId, options.language)
    : await generateStage1Outputs(accessToken, opportunityId, options.language);
  let presentation = presentationOf(started);
  let jobId: string | null = null;
  let completedJob: JobResponse | undefined;

  if (presentation?.status === "failed") {
    throw new ApiRequestError(
      `${PRODUCT} generation failed: ${presentation.code ?? "unknown error"}`,
      409,
      presentation.code ?? "PPT1_FAILED",
    );
  }

  if (presentation?.status !== "ready") {
    const active = await getActiveJob(accessToken, opportunityId, "presentation");
    if (!active?.job_id) {
      throw new ApiRequestError(
        `${PRODUCT} generation did not return a job to poll.`,
        409,
        "PPT1_NOT_READY",
      );
    }
    jobId = active.job_id;
    if (active.status === "COMPLETED") {
      completedJob = await getJob(accessToken, active.job_id);
      onJob?.(completedJob);
    } else {
      completedJob = await waitForJob(accessToken, active.job_id, { onProgress: onJob });
    }
    presentation = presentationOf(await getStage1Outputs(accessToken, opportunityId));
  }

  const workflow = await getWorkflowStatus(accessToken, opportunityId);
  const ppt1 = workflow.documents?.ppt1;
  if (ppt1?.presentation_id && presentation?.presentation_id && ppt1.presentation_id !== presentation.presentation_id) {
    throw new ApiRequestError(
      `${PRODUCT} workflow identity does not match the generated presentation.`,
      409,
      "PPT1_IDENTITY_MISMATCH",
    );
  }
  const presentationId = ppt1?.presentation_id || presentation?.presentation_id || "";
  const presentationVersionId = ppt1?.latest_ready_version_id || "";
  if (!presentationId || !presentationVersionId || presentation?.status !== "ready" ||
      (completedJob && completedJob.status !== "COMPLETED")) {
    throw new ApiRequestError(
      `${PRODUCT} generation finished without a ready version.`,
      409,
      "PPT1_NOT_READY",
    );
  }
  if ((completedJob?.result.presentation_id && completedJob.result.presentation_id !== presentationId) ||
      (completedJob?.result.presentation_version_id && completedJob.result.presentation_version_id !== presentationVersionId)) {
    throw new ApiRequestError(
      `${PRODUCT} workflow version does not match the completed generation job.`,
      409,
      "PPT1_IDENTITY_MISMATCH",
    );
  }
  return { presentationId, presentationVersionId, jobId };
}
