// Discovery v2: the AI Opportunity Analysis. The server stores the content once and derives the
// printed pages from it (page_manifest), so the page count is whatever the content needs.
// Papers stored with schema 1.0 (seven fixed pages) are not handled here: see liveDiscovery.ts.
import { ApiRequestError, apiFetch, generateDiscoveryPaper, resolveBackendOpportunityId, type PreviewClientSeed } from "./api";

export const ANALYSIS_SCHEMA_VERSION = "2.0";
export const PAGE_TYPES = ["M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9"] as const;
export type PageType = (typeof PAGE_TYPES)[number];
export type AnalysisStatus = "not_generated" | "generating" | "ready" | "failed";
export type StageStatus = "waiting" | "generating" | "ready" | "failed" | "skipped";

export interface AnalysisPage {
  id: string;
  number: number;
  type: PageType;
  dark: boolean;
  chapter: { number: number; name: string } | null;
  nav_label: string;
  edit_targets: string[];
  content: Record<string, unknown>;
}

export interface AnalysisStage {
  key: string;
  label: string;
  status: StageStatus;
}

export interface KnownFact {
  label: string;
  value: string;
  origin: "USER_INPUT" | "SOURCE_FACT";
  /** SOURCE_FACT only: where the statement is quoted from (BAP-5: a page of the client's website). */
  source_refs?: { source_id: string; locator: string; excerpt: string }[];
}

/** A cited source that can be opened: only absolute http(s) addresses, never other schemes. */
export function sourceLink(ref: { source_id?: string } | undefined): string | null {
  const value = String(ref?.source_id ?? "");
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : null;
  } catch {
    return null;
  }
}

export interface DiscoveryAnalysis {
  schema_version: typeof ANALYSIS_SCHEMA_VERSION;
  opportunity_id: string;
  document_id: string | null;
  latest_approved_version_id: string | null;
  status: AnalysisStatus;
  generated_at: string | null;
  language: string;
  intake_context: { client_name: string; meeting_purpose: string };
  generation: {
    mode: "fixture" | "live";
    specificity: "company" | "context" | "generic";
    research_mode: "user_context_only" | "provider";
    optional_parts: Record<string, boolean>;
    stages: AnalysisStage[];
  };
  analysis: Record<string, unknown> | null;
  page_manifest: AnalysisPage[];
}

export interface AnalysisVersion {
  id: string;
  version_number: number;
  document_id: string;
  status: "draft" | "approved";
}

export interface AnalysisView {
  paper: DiscoveryAnalysis;
  /** The stored version of the current document: its draft, or the approved version. */
  version: AnalysisVersion | null;
}

export class DiscoveryAnalysisError extends Error {}

type Obj = Record<string, unknown>;

function object(value: unknown, what: string): Obj {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new DiscoveryAnalysisError(`The server returned an invalid ${what}.`);
  }
  return value as Obj;
}

/** True for an AI Opportunity Analysis; false for a stored schema 1.0 paper. */
export function isOpportunityAnalysis(payload: unknown): boolean {
  return Boolean(payload) && typeof payload === "object" && (payload as Obj).schema_version === ANALYSIS_SCHEMA_VERSION;
}

/** Checks what the workspace relies on. The page count is not part of the contract. */
export function parseDiscoveryAnalysis(payload: unknown, backendId?: string): DiscoveryAnalysis {
  const paper = object(payload, "Discovery analysis");
  if (paper.schema_version !== ANALYSIS_SCHEMA_VERSION) {
    throw new DiscoveryAnalysisError("This Discovery document uses an older format.");
  }
  if (backendId !== undefined && paper.opportunity_id !== backendId) {
    throw new DiscoveryAnalysisError("Discovery belongs to another opportunity.");
  }
  if (!["not_generated", "generating", "ready", "failed"].includes(String(paper.status))) {
    throw new DiscoveryAnalysisError("The server returned an unknown Discovery status.");
  }
  const generation = object(paper.generation, "generation state");
  if (!Array.isArray(generation.stages) || !Array.isArray(paper.page_manifest)) {
    throw new DiscoveryAnalysisError("The server returned an incomplete Discovery analysis.");
  }
  const manifest = paper.page_manifest.map((raw, index) => {
    const page = object(raw, "Discovery page");
    if (typeof page.id !== "string" || page.number !== index + 1 || !(PAGE_TYPES as readonly unknown[]).includes(page.type)) {
      throw new DiscoveryAnalysisError(`Discovery page ${index + 1} is invalid.`);
    }
    object(page.content, "Discovery page");
    return page as unknown as AnalysisPage;
  });
  if (new Set(manifest.map((page) => page.id)).size !== manifest.length) {
    throw new DiscoveryAnalysisError("Discovery page ids are not unique.");
  }
  if ((paper.status === "ready") !== (manifest.length > 0 && paper.analysis !== null)) {
    throw new DiscoveryAnalysisError("Discovery status and content do not agree.");
  }
  object(paper.intake_context, "Discovery context");
  return { ...(paper as unknown as DiscoveryAnalysis), page_manifest: manifest };
}

export interface ChapterGroup {
  key: string;
  label: string;
  pages: AnalysisPage[];
}

/** Pages grouped for navigation: front matter, then one group per chapter of the document. */
export function chapterGroups(manifest: readonly AnalysisPage[]): ChapterGroup[] {
  const groups: ChapterGroup[] = [];
  for (const page of manifest) {
    const key = page.chapter ? `chapter-${page.chapter.number}` : "front";
    const label = page.chapter ? `${String(page.chapter.number).padStart(2, "0")} · ${page.chapter.name}` : "Front matter";
    const last = groups[groups.length - 1];
    if (last?.key === key) last.pages.push(page);
    else groups.push({ key, label, pages: [page] });
  }
  return groups;
}

/** Progress of generation: stages are the unit, because pages only exist once layout is done. */
export function stageProgress(stages: readonly AnalysisStage[]): { done: number; total: number; current: AnalysisStage | null; failed: AnalysisStage | null } {
  const counted = stages.filter((stage) => stage.status !== "skipped");
  return {
    done: counted.filter((stage) => stage.status === "ready").length,
    total: counted.length,
    current: counted.find((stage) => stage.status === "generating") ?? null,
    failed: counted.find((stage) => stage.status === "failed") ?? null,
  };
}

export function pagePosition(manifest: readonly AnalysisPage[], pageId: string): string {
  const page = manifest.find((item) => item.id === pageId);
  return page ? `Page ${page.number} of ${manifest.length}` : `${manifest.length} pages`;
}

const SECTION_NAMES: Record<string, string> = {
  thesis: "Core thesis",
  framing: "Titles, chapter texts and notes",
  area: "Area and business case",
  opportunity: "Opportunity",
  shadow_process: "Shadow process",
  shadow_findings: "Findings on shadow processes",
  target_workflow: "Workflow chain",
  target_picture: "Target picture",
  closing: "Conclusion",
  optional: "Optional deep dive",
};

function byId(items: unknown, id: string): Obj | undefined {
  return Array.isArray(items) ? (items as Obj[]).find((item) => item?.id === id) : undefined;
}

const FIELDS: Record<string, readonly string[]> = {
  thesis: ["text"],
  framing: ["document", "chapters", "leads"],
  area: ["name", "lead", "business_case"],
  opportunity: ["title", "solution", "ai_technology", "how_it_works", "result", "discovery_questions", "opportunity_signal"],
  shadow_process: ["decision", "handled_today_via", "ai_approach"],
  shadow_findings: ["cards", "points"],
  target_workflow: ["name", "stages", "autonomous_flow", "human_decision_gate"],
  target_picture: ["diagram_title", "diagram_lead", "dark_processing_pattern", "maturity_levels", "learning_loop", "points"],
  closing: ["name", "headline", "paragraphs", "steps"],
  optional: ["chapter", "lead", "columns", "rows", "footnote"],
};

/** The logical section behind an edit target, with a readable label and its editable fields. */
export function editableSection(analysis: Obj | null, target: string): { label: string; value: Obj } | null {
  if (!analysis) return null;
  const [kind, ref = ""] = target.split(":");
  const [first, second] = ref.split("/");
  let section: Obj | undefined;
  let name = "";
  if (kind === "thesis") section = (analysis.research as Obj | undefined)?.core_thesis as Obj | undefined;
  else if (["framing", "shadow_findings", "target_picture", "closing"].includes(kind)) section = analysis[kind] as Obj | undefined;
  else if (kind === "area") section = byId(analysis.areas, first);
  else if (kind === "opportunity") section = byId(byId(analysis.areas, first)?.opportunities, second);
  else if (kind === "shadow_process") section = byId(analysis.shadow_processes, first);
  else if (kind === "target_workflow") section = byId(analysis.target_workflows, first);
  else if (kind === "optional") section = ((analysis.optional_deep_dives as Obj | undefined)?.[first] ?? undefined) as Obj | undefined;
  if (!section || !FIELDS[kind]) return null;
  name = String(section.title ?? section.name ?? section.decision ?? "");
  const value: Obj = {};
  for (const field of FIELDS[kind]) value[field] = structuredClone(section[field]);
  return { label: name && name !== SECTION_NAMES[kind] ? `${SECTION_NAMES[kind]}: ${name}` : SECTION_NAMES[kind], value };
}

export function fieldLabel(key: string): string {
  return key.replace(/_/g, " ").replace(/^./, (letter) => letter.toUpperCase());
}

// ---------------------------------------------------------------------------------- API

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

interface ReadOptions {
  signal?: AbortSignal;
}

async function currentVersion(token: string, backendId: string, paper: DiscoveryAnalysis, options: ReadOptions): Promise<AnalysisVersion | null> {
  if (!paper.document_id || paper.status !== "ready") return null;
  const envelope = await apiFetch<{ versions: unknown[] }>(`/opportunities/${backendId}/discovery-paper/versions`, token, { signal: options.signal });
  if (!Array.isArray(envelope.versions)) throw new DiscoveryAnalysisError("Discovery version list is invalid.");
  const rows = envelope.versions
    .map((row) => object(row, "Discovery version"))
    .filter((row) => row.document_id === paper.document_id)
    .sort((a, b) => Number(b.version_number) - Number(a.version_number));
  const row = rows[0];
  if (!row) return null;
  return {
    id: String(row.id),
    version_number: Number(row.version_number),
    document_id: String(row.document_id),
    status: row.status === "approved" ? "approved" : "draft",
  };
}

/** The stored paper as the server returned it: an analysis (2.0), a v1 paper (1.0) or nothing. */
export async function loadDiscoveryPayload(token: string, opportunityId: string, options: ReadOptions = {}): Promise<unknown | null> {
  const backendId = resolveBackendOpportunityId(opportunityId);
  // Loading must never create an opportunity just to look for a paper.
  if (!UUID.test(backendId)) return null;
  try {
    return await apiFetch(`/opportunities/${backendId}/discovery-paper`, token, { signal: options.signal });
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 404) return null;
    throw error;
  }
}

export async function viewOf(token: string, opportunityId: string, payload: unknown, options: ReadOptions = {}): Promise<AnalysisView> {
  const backendId = resolveBackendOpportunityId(opportunityId);
  const paper = parseDiscoveryAnalysis(payload, backendId);
  return { paper, version: await currentVersion(token, backendId, paper, options) };
}

export async function generateAnalysis(token: string, opportunityId: string, seed: PreviewClientSeed | undefined): Promise<unknown> {
  return generateDiscoveryPaper(token, opportunityId, seed);
}

export async function saveAnalysisSection(
  token: string, opportunityId: string, documentId: string, target: string, value: Obj,
): Promise<unknown> {
  const backendId = resolveBackendOpportunityId(opportunityId);
  return apiFetch(`/opportunities/${backendId}/discovery-paper`, token, {
    method: "PATCH",
    body: JSON.stringify({ expected_document_id: documentId, edits: [{ target, value }] }),
  });
}

export async function approveAnalysis(token: string, opportunityId: string): Promise<AnalysisVersion> {
  const backendId = resolveBackendOpportunityId(opportunityId);
  const row = object(await apiFetch(`/opportunities/${backendId}/discovery-paper/approve`, token, { method: "POST" }), "approval");
  if (row.status !== "approved" || typeof row.id !== "string") {
    throw new DiscoveryAnalysisError("The server did not return an approved Discovery version.");
  }
  return { id: row.id, version_number: Number(row.version_number), document_id: String(row.document_id), status: "approved" };
}
