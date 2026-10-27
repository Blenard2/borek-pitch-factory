import type { ClientDirectoryItem } from "./clientDirectory";
import type { ClientInformationExtras, ClientInformationRecord } from "./clientInformation";
import type { ClientInformationViewModel, WorkflowSnapshotViewModel } from "./discoveryFirst";
import { canDownloadDiscoveryPdf, type DiscoveryWorkspaceVersion } from "./discoveryWorkspace";
import { normalizeDocumentLanguage, type DocumentLanguage } from "./documentLanguage";

export type PreviewPresentationState = "waiting" | "generating" | "ready" | "failed";

export interface PreviewPresentation {
  state: PreviewPresentationState;
  version_id: string | null;
  source_discovery_version_id: string | null;
  slide_count: number;
  source_discovery?: DiscoveryWorkspaceVersion;
  version_number?: number;
}

export interface PreviewOpportunity {
  opportunity_id: string;
  created_at: string;
  updated_at: string;
  client: ClientInformationRecord;
  client_extras?: ClientInformationExtras;
  discovery: DiscoveryWorkspaceVersion;
  approved_discovery?: DiscoveryWorkspaceVersion | null;
  presentation: PreviewPresentation;
  workflow: WorkflowSnapshotViewModel;
  /**
   * BAP-12: the language client documents are generated in (not the interface language). For a
   * server opportunity this mirrors the API's `language`; a client that exists only here carries
   * it until the server record is created with it. Missing means English.
   */
  document_language?: DocumentLanguage;
}

export interface PreviewJourneyState {
  schema_version: "1.0";
  opportunities: Record<string, PreviewOpportunity>;
}

export const PREVIEW_JOURNEY_STORAGE_KEY = "borek-preview-journey-v1";

export function previewJourneyStorageKey(ownerId: string): string {
  return `${PREVIEW_JOURNEY_STORAGE_KEY}:${ownerId}`;
}

export const EMPTY_PREVIEW_JOURNEY: PreviewJourneyState = {
  schema_version: "1.0",
  opportunities: {},
};

export function previewOpportunityId(companyName: string): string {
  const slug = companyName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 32) || "client";
  const suffix = globalThis.crypto?.randomUUID?.().slice(0, 8) ?? Date.now().toString(36);
  return `opp-${slug}-${suffix}`;
}

export function previewDirectoryItem(opportunity: PreviewOpportunity): ClientDirectoryItem {
  return {
    opportunity_id: opportunity.opportunity_id,
    company_name: opportunity.client.values.company_name,
    engagement_name: opportunity.client.values.meeting_purpose,
    contact_person: opportunity.client.values.contact_person,
    contact_role: "Primary contact",
    workflow_status: opportunity.workflow.current_status,
    phase: "pre_meeting",
    last_activity: "Today",
  };
}

export function previewClientRecord(
  opportunityId: string,
  values: ClientInformationViewModel,
): ClientInformationRecord {
  return {
    opportunity_id: opportunityId,
    revision: 1,
    values,
    source: "fixture",
  };
}

export function parsePreviewJourney(value: unknown): PreviewJourneyState {
  if (!value || typeof value !== "object" || Array.isArray(value)) return EMPTY_PREVIEW_JOURNEY;
  const row = value as Partial<PreviewJourneyState>;
  if (row.schema_version !== "1.0" || !row.opportunities || typeof row.opportunities !== "object") {
    return EMPTY_PREVIEW_JOURNEY;
  }
  const parsed = structuredClone(row as PreviewJourneyState);
  for (const opportunity of Object.values(parsed.opportunities)) {
    opportunity.document_language = normalizeDocumentLanguage(opportunity.document_language);
    const discovery = opportunity.discovery;
    if (discovery.pdf_artifact_id && discovery.pdf_download_url && discovery.pdf_source_revision == null) {
      discovery.pdf_source_revision = discovery.revision;
    }
    if (!opportunity.approved_discovery && discovery.document_state === "approved") {
      opportunity.approved_discovery = structuredClone(discovery);
    }
    const source = presentationSource(opportunity);
    if (source && opportunity.presentation.source_discovery_version_id) {
      opportunity.presentation.source_discovery = structuredClone(source);
    }
  }
  return parsed;
}

function nextWorkflow(
  opportunity: PreviewOpportunity,
  current_status: WorkflowSnapshotViewModel["current_status"],
  completed_statuses: WorkflowSnapshotViewModel["completed_statuses"],
): WorkflowSnapshotViewModel {
  return {
    revision: opportunity.workflow.revision + 1,
    current_status,
    completed_statuses,
    blocked_reason: null,
    available_actions: [],
  };
}

export function withUpdatedDiscovery(
  opportunity: PreviewOpportunity,
  discovery: DiscoveryWorkspaceVersion,
): PreviewOpportunity {
  const prepared = discovery.pages.every((page) => page.state === "ready") &&
    discovery.pdf_source_revision === discovery.revision;
  return {
    ...opportunity,
    discovery: structuredClone(discovery),
    workflow: prepared && !opportunity.approved_discovery
      ? nextWorkflow(opportunity, "discovery_prepared", ["client_information"])
      : opportunity.workflow,
  };
}

export function withApprovedDiscovery(
  opportunity: PreviewOpportunity,
  discovery: DiscoveryWorkspaceVersion,
): PreviewOpportunity {
  if (discovery.document_state !== "approved" || !canDownloadDiscoveryPdf(discovery)) {
    return opportunity;
  }
  const approved = structuredClone(discovery);
  const source = presentationSource(opportunity);
  return {
    ...opportunity,
    discovery: approved,
    approved_discovery: structuredClone(approved),
    presentation: source && opportunity.presentation.source_discovery_version_id
      ? { ...opportunity.presentation, source_discovery: structuredClone(source) }
      : opportunity.presentation,
    workflow: opportunity.presentation.state === "ready" ? opportunity.workflow
      : nextWorkflow(opportunity, "discovery_prepared", ["client_information", "discovery_prepared"]),
  };
}

export function presentationSource(opportunity: PreviewOpportunity): DiscoveryWorkspaceVersion | undefined {
  const presentation = opportunity.presentation;
  const source = presentation.source_discovery ?? opportunity.approved_discovery;
  return source?.document_state === "approved" &&
    (source.version_id === presentation.source_discovery_version_id ||
      (presentation.state === "waiting" && !presentation.source_discovery_version_id))
    ? source : undefined;
}

export function withPresentationStarted(opportunity: PreviewOpportunity): PreviewOpportunity {
  if (opportunity.presentation.state === "generating") return opportunity;
  const approved = opportunity.approved_discovery;
  if (!approved || approved.document_state !== "approved" || !canDownloadDiscoveryPdf(approved)) {
    return opportunity;
  }
  const versionNumber = Math.max(opportunity.presentation.version_number ?? 0,
    Number(opportunity.presentation.version_id?.match(/^ppt-1-v(\d+)$/)?.[1] ?? 0)) + 1;
  return {
    ...opportunity,
    presentation: {
      state: "generating",
      version_id: `ppt-1-v${versionNumber}`,
      version_number: versionNumber,
      source_discovery_version_id: approved.version_id,
      source_discovery: structuredClone(approved),
      slide_count: 0,
    },
  };
}

export function withPresentationFailed(opportunity: PreviewOpportunity): PreviewOpportunity {
  if (opportunity.presentation.state !== "generating") return opportunity;
  return { ...opportunity, presentation: { ...opportunity.presentation, state: "failed" } };
}

export function withPresentationAdvanced(opportunity: PreviewOpportunity): PreviewOpportunity {
  if (opportunity.presentation.state !== "generating") return opportunity;
  const nextSlideCount = Math.min(opportunity.presentation.slide_count + 1, 7);
  if (nextSlideCount < 7) {
    return {
      ...opportunity,
      presentation: { ...opportunity.presentation, slide_count: nextSlideCount },
    };
  }
  return withPresentationCompleted({
    ...opportunity,
    presentation: { ...opportunity.presentation, slide_count: nextSlideCount },
  });
}

export function withPresentationCompleted(opportunity: PreviewOpportunity): PreviewOpportunity {
  const source = presentationSource(opportunity);
  if (!source || opportunity.presentation.state !== "generating" ||
      opportunity.presentation.source_discovery_version_id !== source.version_id) return opportunity;
  return {
    ...opportunity,
    presentation: {
      ...opportunity.presentation,
      state: "ready",
      version_id: opportunity.presentation.version_id ?? `ppt-1-v${opportunity.presentation.version_number ?? 1}`,
      version_number: opportunity.presentation.version_number ?? 1,
      source_discovery_version_id: source.version_id,
      source_discovery: structuredClone(source),
      slide_count: 7,
    },
    workflow: nextWorkflow(opportunity, "ppt_1_ready", ["client_information", "discovery_prepared", "ppt_1_ready"]),
  };
}
