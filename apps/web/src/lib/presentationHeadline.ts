import { ApiRequestError, apiFetch, resolveBackendOpportunityId } from "./api";

/**
 * BAP-2: the client-specific title and main opportunity of the Client Presentation.
 *
 * Both come from the approved Discovery analysis the presentation is built from - its AI-written
 * document title (analysis.framing.document.title) and its first priority opportunity
 * (presentation_brief.priority_opportunities[0]). No other model is asked and nothing is made up:
 * without an approved analysis of this opportunity there is no generated title.
 */

export interface MainOpportunity { area: string; title: string; result: string }

export type PresentationHeadline =
  | { state: "ready"; title: string; opportunity: MainOpportunity | null; versionId: string; versionNumber: number | null; clientName: string }
  /** No generated title exists yet: no approved Discovery, or one in the earlier format without it. */
  | { state: "missing"; reason: "not_approved" | "not_generated" | "earlier_format" }
  | { state: "failed"; message: string };

type Paper = Record<string, unknown>;
const text = (value: unknown) => (typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "");

/**
 * Title and main opportunity of one approved Discovery version. A version of another
 * opportunity is refused, never shown.
 */
export function headlineFromVersion(version: unknown, opportunityId: string): PresentationHeadline {
  const backendId = resolveBackendOpportunityId(opportunityId);
  const row = (version && typeof version === "object" ? version : {}) as Record<string, unknown>;
  const paper = (row.paper_json && typeof row.paper_json === "object" ? row.paper_json : {}) as Paper;
  if (row.opportunity_id !== backendId || (paper.opportunity_id !== undefined && paper.opportunity_id !== backendId)) {
    return { state: "failed", message: "The Discovery returned for this presentation belongs to another client. Reload the page." };
  }
  if (row.status !== "approved") return { state: "missing", reason: "not_approved" };
  if (paper.schema_version !== "2.0") return { state: "missing", reason: "earlier_format" };
  const analysis = (paper.analysis && typeof paper.analysis === "object" ? paper.analysis : null) as Paper | null;
  const framing = (analysis?.framing ?? {}) as Paper;
  const title = text((framing.document as Paper | undefined)?.title);
  if (!title) return { state: "missing", reason: "not_generated" };
  const brief = (paper.presentation_brief ?? {}) as Paper;
  const first = Array.isArray(brief.priority_opportunities) ? brief.priority_opportunities[0] as Paper | undefined : undefined;
  const opportunity = first && text(first.title)
    ? { area: text(first.area), title: text(first.title), result: text(first.result) }
    : null;
  return {
    state: "ready",
    title,
    opportunity,
    versionId: String(row.id ?? ""),
    versionNumber: typeof row.version_number === "number" ? row.version_number : null,
    clientName: text(brief.client_name),
  };
}

/**
 * The headline of the approved Discovery version ``versionId`` (the one the presentation was
 * built from), or of the latest approved version when the presentation does not exist yet.
 */
export async function loadPresentationHeadline(token: string, opportunityId: string, versionId: string | null, signal?: AbortSignal): Promise<PresentationHeadline> {
  const base = `/opportunities/${encodeURIComponent(resolveBackendOpportunityId(opportunityId))}/discovery-paper`;
  const path = versionId ? `${base}/versions/${encodeURIComponent(versionId)}` : `${base}/approved`;
  try {
    return headlineFromVersion(await apiFetch(path, token, { signal, cache: "no-store" }), opportunityId);
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 404) return { state: "missing", reason: "not_approved" };
    throw error;
  }
}

/** The statement shown under the title. */
export function opportunityStatement(opportunity: MainOpportunity): string {
  const head = opportunity.area ? `${opportunity.area}: ${opportunity.title}` : opportunity.title;
  return opportunity.result ? `${head} — ${opportunity.result}` : head;
}

export const MISSING_TEXT: Record<Extract<PresentationHeadline, { state: "missing" }>["reason"], string> = {
  not_approved: "The client-specific title and main opportunity appear once the Discovery analysis is approved.",
  not_generated: "The approved Discovery has no generated title. Regenerate and approve the Discovery analysis.",
  earlier_format: "This Discovery is stored in the earlier format, which has no generated title. Generate a new Discovery analysis to show it.",
};
