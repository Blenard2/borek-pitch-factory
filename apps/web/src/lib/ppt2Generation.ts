import {
  ApiRequestError,
  generatePpt2,
  regeneratePpt2,
  getJob,
  getWorkflowStatus,
  waitForJob,
  type JobResponse,
  type Ppt2GenerateResponse,
} from "./api";

export interface PostMeetingPresentationResult {
  presentationId: string;
  presentationVersionId: string;
  jobId: string | null;
}

function versionIdFrom(started: Ppt2GenerateResponse, job: JobResponse | null): string {
  const fromJob = job?.result?.presentation_version_id;
  const value = fromJob ?? started.presentation_version_id ?? "";
  return String(value);
}

/**
 * Start or resume the dedicated PPT #2 generation and poll the job until ready.
 * A workflow status that already names one ready PPT #2 is returned as-is.
 */
export async function generateAndAwaitPostMeetingPresentation(
  accessToken: string,
  opportunityId: string,
  onJob?: (job: JobResponse) => void,
  options?: { regeneratePresentationId?: string; language?: "en" | "de" },
): Promise<PostMeetingPresentationResult> {
  const current = await getWorkflowStatus(accessToken, opportunityId);
  const ready = current.documents?.ppt2;
  if (!options?.regeneratePresentationId && ready?.presentation_id && ready.latest_ready_version_id) {
    return {
      presentationId: ready.presentation_id,
      presentationVersionId: ready.latest_ready_version_id,
      jobId: null,
    };
  }

  if (options?.regeneratePresentationId && ready?.presentation_id !== options.regeneratePresentationId) {
    throw new ApiRequestError("PPT #2 changed before regeneration. Reload and review the current version.", 409, "PPT2_IDENTITY_AMBIGUOUS");
  }
  const started = options?.regeneratePresentationId
    ? await regeneratePpt2(accessToken, opportunityId, options.regeneratePresentationId, options.language)
    : await generatePpt2(accessToken, opportunityId, options?.language);
  let job: JobResponse | null = null;
  if (started.job_id) {
    if (started.status === "COMPLETED") {
      job = await getJob(accessToken, started.job_id);
      onJob?.(job);
    } else {
      job = await waitForJob(accessToken, started.job_id, { onProgress: onJob });
    }
  }

  const confirmed = await getWorkflowStatus(accessToken, opportunityId);
  const ppt2 = confirmed.documents?.ppt2;
  if (ppt2?.presentation_id !== started.presentation_id || started.journey_stage !== "post_meeting" ||
      (options?.regeneratePresentationId && started.presentation_id !== options.regeneratePresentationId)) {
    throw new ApiRequestError(
      "PPT #2 workflow identity does not match the generated presentation.",
      409,
      "PPT2_IDENTITY_AMBIGUOUS",
    );
  }

  const presentationVersionId = versionIdFrom(started, job);
  if (!presentationVersionId || presentationVersionId !== ppt2?.latest_ready_version_id ||
      (job ? job.status !== "COMPLETED" : !["ready", "COMPLETED"].includes(started.status)) ||
      (options?.regeneratePresentationId && presentationVersionId === ready?.latest_ready_version_id)) {
    throw new ApiRequestError(
      "PPT #2 generation finished without a ready version.",
      409,
      "PPT2_NOT_READY",
    );
  }
  if (JSON.stringify(current.documents?.ppt1 ?? null) !== JSON.stringify(confirmed.documents?.ppt1 ?? null)) {
    throw new ApiRequestError("PPT #1 changed while PPT #2 was generated. Review both artifacts before continuing.", 409, "PPT1_CHANGED_DURING_PPT2");
  }
  return {
    presentationId: started.presentation_id,
    presentationVersionId,
    jobId: started.job_id,
  };
}
