import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { ApiRequestError } from "./api";
import {
  generateAndAwaitMasterV2, loadMasterV2Status, masterV2Error, masterV2StageText, parseMasterV2Status, postMeetingDeckLabel,
  type MasterV2Status,
} from "./masterPresentationV2";
import { isMasterJourney, MASTER_JOURNEY_STEP_LABELS, WORKFLOW_STATUS_CATALOG, workflowStepLabel } from "./discoveryFirst";
import { adaptEarlierVersions, versionLabel, type PresentationVersionSummary } from "./presentationLive";

const opportunityId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const presentationId = "11111111-1111-4111-8111-111111111111";
const v1 = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa";
const v2 = "bbbbbbbb-2222-4bbb-8bbb-bbbbbbbbbbbb";
const status = (overrides: Partial<MasterV2Status> = {}): MasterV2Status => ({
  opportunity_id: opportunityId, product_version: "V2", presentation_id: presentationId, state: "none", can_generate: true,
  ready_for_v2: true, blockers: [], finalized: false, latest_ready: null, job: null, ...overrides,
});
const ready = status({ state: "ready", can_generate: false, latest_ready: { presentation_version_id: v2, version_number: 2, generation_fingerprint: "a".repeat(64) } });

function mock(routes: (method: string, path: string) => unknown) {
  const original = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = async (input, init) => {
    const method = init?.method ?? "GET";
    const path = new URL(String(input)).pathname;
    calls.push(`${method} ${path}`);
    const value = routes(method, path);
    if (value instanceof Response) return value;
    if (value === undefined) throw new Error(`Unexpected request: ${method} ${path}`);
    return Response.json(value);
  };
  return { calls, restore: () => { globalThis.fetch = original; } };
}

test("the V2 status is validated and never accepted for another opportunity", () => {
  assert.equal(parseMasterV2Status(ready, opportunityId).latest_ready?.presentation_version_id, v2);
  assert.throws(() => parseMasterV2Status({ ...ready, opportunity_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" }, opportunityId), /another opportunity/);
  assert.throws(() => parseMasterV2Status({ ...ready, latest_ready: null }, opportunityId), /incomplete/, "ready without a version is not ready");
  assert.throws(() => parseMasterV2Status({ ...ready, state: "done" }, opportunityId), /incomplete/);
  assert.throws(() => parseMasterV2Status({ ...ready, product_version: "V1" }, opportunityId), /incomplete/);
  assert.throws(() => parseMasterV2Status(null, opportunityId), /incomplete/);
});

test("generation uses the dedicated V2 endpoint, follows the job and reports what the API says afterwards", async () => {
  const stages: string[] = [];
  let polls = 0;
  const { calls, restore } = mock((method, path) => {
    if (method === "POST" && path === `/opportunities/${opportunityId}/master-presentation/v2/generate`) {
      return { presentation_id: presentationId, presentation_version_id: null, product_version: "V2", status: "QUEUED", job_id: "job-1", is_existing_job: false, is_existing_version: false };
    }
    if (path === "/jobs/job-1") {
      polls += 1;
      return { job_id: "job-1", job_type: "presentation_generation", status: "COMPLETED", current_stage: "PREVIEW_RENDERING", created_at: "", started_at: null, completed_at: null, result: {}, error: null };
    }
    if (path === `/opportunities/${opportunityId}/master-presentation/v2`) return ready;
    return undefined;
  });
  try {
    const result = await generateAndAwaitMasterV2("token", opportunityId, (job) => stages.push(job.current_stage));
    assert.equal(result.state, "ready");
    assert.equal(polls, 1);
    assert.deepEqual(stages, ["PREVIEW_RENDERING"]);
    assert.equal(calls[0], `POST /opportunities/${opportunityId}/master-presentation/v2/generate`);
    assert.ok(calls.every((call) => !/ppt2|stage2|owner-reviewed|finalize/.test(call)), "the earlier PPT #2 generator is never involved");
  } finally { restore(); }
});

test("an existing ready V2 is returned without waiting for a job, and a failure is an error, not a ready deck", async () => {
  let server = mock((method, path) => method === "POST"
    ? { presentation_id: presentationId, presentation_version_id: v2, product_version: "V2", status: "ready", job_id: null, is_existing_job: false, is_existing_version: true }
    : path.endsWith("/master-presentation/v2") ? ready : undefined);
  try {
    assert.equal((await generateAndAwaitMasterV2("token", opportunityId)).latest_ready?.presentation_version_id, v2);
    assert.ok(server.calls.every((call) => !call.includes("/jobs/")));
  } finally { server.restore(); }

  server = mock((method, path) => method === "POST"
    ? { presentation_id: presentationId, presentation_version_id: null, product_version: "V2", status: "QUEUED", job_id: "job-2", is_existing_job: true, is_existing_version: false }
    : path === "/jobs/job-2" ? { job_id: "job-2", job_type: "presentation_generation", status: "FAILED", current_stage: "PPTX_RENDERING", created_at: "", started_at: null, completed_at: null, result: {}, error: { code: "MASTER_PRESENTATION_RENDER_FAILED", message: "LibreOffice produced no PDF for the deck", retryable: false } }
    : undefined);
  try {
    const failure = await generateAndAwaitMasterV2("token", opportunityId).then(() => null, (error: unknown) => error);
    assert.ok(failure instanceof ApiRequestError);
    assert.match(masterV2Error(failure), /could not be generated: LibreOffice produced no PDF/);
  } finally { server.restore(); }

  // The job completed but the API does not report a ready V2: never shown as ready.
  server = mock((method, path) => method === "POST"
    ? { presentation_id: presentationId, presentation_version_id: null, product_version: "V2", status: "COMPLETED", job_id: "job-3", is_existing_job: false, is_existing_version: false }
    : path.endsWith("/master-presentation/v2") ? status({ state: "failed", job: { job_id: "job-3", status: "FAILED", error_code: "MASTER_V2_SNAPSHOT_INVALID", error_message: "The frozen source snapshot does not match the generation manifest" } }) : undefined);
  try {
    await assert.rejects(generateAndAwaitMasterV2("token", opportunityId), /frozen source snapshot does not match/);
  } finally { server.restore(); }

  server = mock(() => Response.json({ error: { code: "MASTER_V2_NOT_READY", message: "Master Presentation V2 cannot be generated yet. Go back to Post Meeting and confirm the meeting findings." } }, { status: 409 }));
  try {
    const refused = await generateAndAwaitMasterV2("token", opportunityId).then(() => null, (error: unknown) => error);
    assert.match(masterV2Error(refused), /Go back to Post Meeting and confirm the meeting findings/);
    await assert.rejects(loadMasterV2Status("token", opportunityId), /MASTER_V2_NOT_READY|cannot be generated yet/);
  } finally { server.restore(); }
});

test("labels tell Master Presentation V1, V2 and the earlier PPT #2 apart without using the revision number", () => {
  assert.equal(postMeetingDeckLabel("V2"), "Master Presentation V2");
  assert.equal(postMeetingDeckLabel(undefined), "PPT #2");
  assert.equal(masterV2StageText("PPTX_RENDERING"), "Assembling the presentation and rendering the PDF");
  assert.equal(masterV2StageText("SOMETHING_NEW"), "Generating Master Presentation V2");
  const source = (product: string, revision: number) => ({
    kind: "x", product_version: product, product_stage: "x", revision, master_id: "m", master_version: "1", canonical_slide_count: 26,
    appendix_slide_count: 7, approved_discovery_version_id: "d", discovery_schema_version: "2.0",
  });
  // Revision 3 can be a V1 and revision 2 a V2: the label follows the product version of the source.
  assert.equal(versionLabel({ versionNumber: 3, source: source("V1", 3) }), "Master Presentation V1 · revision 3");
  assert.equal(versionLabel({ versionNumber: 2, source: source("V2", 2) }), "Master Presentation V2 · revision 2");
  assert.equal(versionLabel({ versionNumber: 4, source: null }), "revision 4");
  const row = (id: string, number: number, product: string, latest = false): PresentationVersionSummary => ({
    presentation_version_id: id, version_number: number, status: "ready", is_latest: latest, source: source(product, number),
    pptx_download_url: `/presentations/${presentationId}/versions/${id}/download/pptx`,
    pdf_download_url: `/presentations/${presentationId}/versions/${id}/download/pdf`,
  });
  const rows = [row(v2, 2, "V2", true), row(v1, 1, "V1")];
  // Seen from V1, the newer V2 is listed; seen from V2, V1 is. The version on screen is never listed.
  assert.deepEqual(adaptEarlierVersions(presentationId, rows, v1).map((version) => versionLabel(version)), ["Master Presentation V2 · revision 2"]);
  assert.deepEqual(adaptEarlierVersions(presentationId, rows, v2).map((version) => versionLabel(version)), ["Master Presentation V1 · revision 1"]);
  assert.deepEqual(adaptEarlierVersions(presentationId, rows).map((version) => version.versionId), [v1]);
});

test("the V2 action is offered only when the API allows it, and owner review stays a separate step", () => {
  const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
  const meeting = read("../components/MeetingEvidencePanel.tsx");
  // BAP-12: the language gate is added next to the server's gate.
  assert.match(meeting, /data-testid="generate-v2" disabled=\{editingDisabled \|\| !v2.can_generate \|\| v2LanguageGate.blocked\} onClick=\{\(\) => void generateV2\(\)\}/);
  assert.match(meeting, /"Retry Master Presentation V2" : v2.state === "outdated" \? "Generate a new V2 revision" : "Generate Master Presentation V2"/);
  assert.match(meeting, /data-testid="v2-progress">\{masterV2StageText\(v2Stage\)\}/);
  assert.match(meeting, /data-testid="v2-error"/);
  assert.ok(meeting.includes('<Link className="btn btn-primary" data-testid="open-v2" href={`${root}/post-meeting-presentation`}>Open Master Presentation V2</Link>'));
  assert.ok(meeting.includes("const canGenerateV2 = Boolean(review?.readiness.ready_for_v2 && confirmedCurrent);") && meeting.includes('{canGenerateV2 ? <div data-testid="v2-ready">'), "no V2 action before the findings are confirmed");
  const generate = meeting.slice(meeting.indexOf("async function generateV2("), meeting.indexOf("// Previous document flow (standalone PPT #2)"));
  assert.match(generate, /generateAndAwaitMasterV2\(accessToken, opportunityId/);
  assert.doesNotMatch(generate, /generateAndAwaitPostMeetingPresentation|prepareMeetingEvidence|ppt2|markOwnerReviewed|finalizeWorkflow/i);
  assert.doesNotMatch(meeting, /Generating V2 is not available yet/);

  const workspace = read("../components/PostMeetingPresentationWorkspace.tsx");
  assert.match(workspace, /\{live && master && deck \? <OwnerCheckpointPanel opportunityId=\{opportunityId\} compact \/> : null\}/);
  assert.match(workspace, /data-testid="master-presentation-v2-summary"/);
  assert.match(workspace, /data-testid="other-presentation-versions"/);
  assert.match(workspace, /loadEarlierVersions\(accessToken, deck.presentationId, controller.signal, deck.presentationVersionId\)/);
  assert.doesNotMatch(workspace, /generateAndAwait|ppt2\/generate/, "the workspace shows a deck; it does not generate one");
  const checkpoint = read("../components/OwnerCheckpointPanel.tsx");
  assert.match(checkpoint, /separate from confirming the meeting findings/);
  assert.match(checkpoint, /a newer version has to be reviewed again/);
  assert.match(checkpoint, /disabled=\{disabled \|\| !identity \|\| confirmation !== identity \|\| !reviewed\} onClick=\{\(\) => void act\(true\)\}>Finalize documents/);
  // Both deck libraries read one pinned version; neither uses the unversioned "latest" routes.
  for (const library of ["./presentationLive.ts", "./postMeetingPresentation.ts"]) {
    const source = read(library);
    assert.match(source, /`\/presentations\/\$\{identity.presentationId\}\/versions\/\$\{identity.presentationVersionId\}`/);
    assert.doesNotMatch(source, /`\/presentations\/\$\{[a-zA-Z.]+\}\/(deck|slides|download|preview)/);
  }
});

test("the Master journey names its steps after the presentation; earlier pitches keep PPT #1 / PPT #2", () => {
  assert.equal(isMasterJourney({ ppt1: { product_version: "V1" }, ppt2: null }), true);
  assert.equal(isMasterJourney({ ppt1: null, ppt2: { product_version: "V2" } }), true);
  assert.equal(isMasterJourney({ ppt1: {}, ppt2: {} }), false, "a deck without a product version is an earlier PPT #1 / PPT #2");
  assert.equal(isMasterJourney(null), false);
  const labels = (master: boolean) => WORKFLOW_STATUS_CATALOG.map((step) => workflowStepLabel(step.id, step.label, master));
  assert.deepEqual(labels(false), WORKFLOW_STATUS_CATALOG.map((step) => step.label));
  assert.deepEqual(labels(true), [
    "Client Information", "Discovery Prepared", "Master Presentation V1 Ready", "First Meeting Completed", "Transcript Added",
    "Master Presentation V2 Ready", "Owner Review", "Finalized",
  ]);
  assert.deepEqual(Object.keys(MASTER_JOURNEY_STEP_LABELS), ["ppt_1_ready", "ppt_2_generated"], "owner review and finalization keep their names");
  assert.equal(workflowStepLabel("ppt_2_generated", "PPT #2 erstellt", true, { ppt_2_generated: "Master Presentation V2 bereit" }), "Master Presentation V2 bereit");

  const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
  const shell = read("../components/PostMeetingShell.tsx");
  assert.match(shell, /workflowStepLabel\(step.id, step.label, isMasterJourney\(workflow\?.documents\)\)/);
  // BAP-7: Discovery 2 is step 2; the presentation follows as step 3, never the earlier document flow.
  assert.match(shell, /master\s+\? <><Link href=\{`\$\{root\}\/post-meeting-discovery`\}/, "step 2 of the Master journey is Discovery 2");
  assert.match(shell, /<Link href=\{`\$\{root\}\/post-meeting-presentation`\}><span>3<\/span><div><small>Master Presentation V2<\/small>/, "step 3 of the Master journey is the presentation, not the earlier document flow");
  const stepper = read("../components/DiscoveryWorkflowStepper.tsx");
  assert.match(stepper, /workflowStepLabel\(step.id, copy.workflow.statuses\[step.id\], workflow.master_journey, copy.workflow.masterStatuses\)/);
  const language = read("../components/LanguageProvider.tsx");
  assert.equal(language.match(/masterStatuses: \{ ppt_1_ready: "Master Presentation V1 /g)?.length, 2, "both languages");
  assert.match(read("./opportunityResolution.ts"), /master_journey: isMasterJourney\(workflow.documents\)/);

  // The earlier standalone PPT #2 action is rendered only for a pitch without a Master Presentation.
  const meeting = read("../components/MeetingEvidencePanel.tsx");
  assert.match(meeting, /const legacyJourney = Boolean\(review\) && review!.master_presentation.status !== "ready" && !isMasterJourney\(workflow\?.documents\);/);
  assert.match(meeting, /\{legacyJourney \? <details data-testid="previous-document-flow">/);
  const legacy = meeting.slice(meeting.indexOf("{legacyJourney ? <details"), meeting.indexOf("</details> : null}"));
  assert.match(legacy, /onClick=\{\(\) => void generateDocuments\(\)\}>Generate documents/);
  assert.equal(meeting.match(/generateDocuments\(\)/g)?.length, 2, "defined once, offered once - inside the legacy section");
  // Findings confirmation and presentation owner review stay two separate actions.
  assert.match(meeting, /onClick=\{\(\) => void confirm\(\)\}/);
  assert.doesNotMatch(meeting, /markOwnerReviewed|finalizeWorkflow/);
  assert.match(read("../components/OwnerCheckpointPanel.tsx"), /markOwnerReviewed\(accessToken, opportunityId\)/);
});
