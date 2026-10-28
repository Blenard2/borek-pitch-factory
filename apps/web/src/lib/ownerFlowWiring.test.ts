import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), "utf8");
}

test("discovery approval and generation call the discovery-paper endpoints", () => {
  const discovery = source("../components/DiscoveryWorkspace.tsx");
  assert.match(discovery, /approveLiveDiscovery/);
  assert.match(discovery, /generateLiveDiscovery/);
  const liveDiscovery = source("./liveDiscovery.ts");
  assert.match(liveDiscovery, /approveDiscoveryPaper/);
  assert.match(liveDiscovery, /generateDiscoveryPaper/);
  assert.doesNotMatch(discovery, /concretisation/);
});

test("meeting input reaches transcript, notes, analysis, review and confirmation; PPT #2 stays a separate previous flow", () => {
  const meeting = source("../components/MeetingEvidencePanel.tsx");
  assert.match(source("./firstMeetingHandoff.ts"), /workflow\/first-meeting-completed/);
  assert.match(meeting, /uploadTranscript/);
  assert.match(meeting, /prepareMeetingEvidence/);
  assert.match(source("./postMeeting.ts"), /savePersonalNotes/);
  assert.match(source("./postMeeting.ts"), /generateMeetingExtraction/);
  assert.match(meeting, /savePersonalNotesChecked/);
  // The transcript selected on screen when "Analyse" is pressed is the one analysed.
  assert.match(meeting, /const selected = transcriptId;[\s\S]*?generateMeetingExtraction\(token, opportunityId, selected\)/);
  assert.match(meeting, /confirmMeetingReview\(token, opportunityId, review, excluded, signal\)/);
  assert.match(meeting, /saveSelectedUseCases/);
  assert.match(meeting, /Previous document flow/);
  assert.match(meeting, /generateAndAwaitPostMeetingPresentation/);
  assert.match(meeting, /regeneratePresentationId/);
  assert.match(source("./ppt2Generation.ts"), /regeneratePpt2/);
  assert.doesNotMatch(meeting, /DeckCenterPanel|MeetingPreparationPanel/);
  assert.doesNotMatch(meeting, /concretisation/);
});

test("owner review can finalize and follow-up prepares a deepening draft only", () => {
  const review = source("../components/OwnerCheckpointPanel.tsx");
  const followUp = source("../components/FollowUpDraftPanel.tsx");
  const api = source("./api.ts");
  assert.match(review, /markOwnerReviewed/);
  assert.match(review, /finalizeWorkflow/);
  assert.match(followUp, /generatePostMeetingEmail\(token, opportunityId, overwrite, gate\.language\)/);
  assert.match(source("./postMeetingEmail.ts"), /journey_stage: "deepening"/);
  assert.doesNotMatch(followUp, /concretisation/);
  assert.doesNotMatch(api, /gamma|GAMMA|PRESENTATION_ENGINE/);
  assert.match(api, /\/ppt2\/generate/);
  assert.match(api, /\/ppt2\/\$\{presentationId\}\/regenerate/);
  assert.match(api, /\/stage1-outputs\/generate/);
  assert.doesNotMatch(api, /\/presentation\/generate/);
});
