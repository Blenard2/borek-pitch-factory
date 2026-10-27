import { ApiRequestError, apiFetch } from "./api";
import { BACKEND_UUID } from "./backendOpportunityMap";
import { WORKFLOW_STATUS_CATALOG, isMasterJourney } from "./discoveryFirst";
import { normalizeDocumentLanguage } from "./documentLanguage";
import { emptyLiveDiscovery } from "./liveDiscovery";
import type { PreviewOpportunity } from "./previewJourney";

export function opportunityAccessMode(id: string, knownPreview: boolean, live: boolean, backendId: string): "preview" | "live" | "missing" {
  if (id === "new") return "missing";
  if (live && BACKEND_UUID.test(backendId)) return "live";
  return knownPreview ? "preview" : "missing";
}

export async function loadLiveOpportunity(token: string, routeId: string, backendId: string, signal?: AbortSignal): Promise<PreviewOpportunity> {
  const row = await apiFetch<Record<string, unknown>>(`/opportunities/${backendId}`, token, { signal });
  if (row.id !== backendId || typeof row.client_name !== "string") {
    throw new ApiRequestError("The API returned a different or incomplete opportunity.", 502);
  }
  const intake = row.stage1_intake && typeof row.stage1_intake === "object" && !Array.isArray(row.stage1_intake)
    ? row.stage1_intake as Record<string, unknown> : {};
  const text = (value: unknown) => typeof value === "string" ? value : "";
  const workflow = await apiFetch<{
    current_status: string; steps?: Array<{ key: string; state: string }>;
    documents?: { ppt1?: { product_version?: string } | null; ppt2?: { product_version?: string } | null };
  }>(`/opportunities/${backendId}/workflow-status`, token, { signal });
  const uiStatus = (key: string) => key === "ppt1_ready" ? "ppt_1_ready" : key === "ppt2_generated" ? "ppt_2_generated" : key;
  const current = WORKFLOW_STATUS_CATALOG.find((status) => status.id === uiStatus(workflow.current_status));
  if (!current) throw new ApiRequestError("The API returned an unknown workflow status.", 502);
  return {
    opportunity_id: routeId,
    created_at: text(row.created_at),
    updated_at: text(row.updated_at),
    client: {
      opportunity_id: routeId, revision: 0, source: "live",
      values: {
        company_name: row.client_name,
        contact_person: text(intake.poc_name),
        website_url: text(intake.client_web_page),
        meeting_purpose: text(intake.sales_topic_description) || text(row.opportunity_name),
        additional_information: text(intake.about_company),
      },
    },
    discovery: emptyLiveDiscovery(routeId),
    presentation: { state: "waiting", version_id: null, source_discovery_version_id: null, slide_count: 0 },
    // BAP-12: the server's document language is the truth on every load (refresh, reopen, Pre/Post).
    document_language: normalizeDocumentLanguage(row.language),
    workflow: {
      revision: 0, current_status: current.id,
      completed_statuses: WORKFLOW_STATUS_CATALOG.filter((status) => workflow.steps?.some((step) => uiStatus(step.key) === status.id && step.state === "completed")).map((status) => status.id),
      available_actions: [], blocked_reason: null,
      master_journey: isMasterJourney(workflow.documents),
    },
  };
}
