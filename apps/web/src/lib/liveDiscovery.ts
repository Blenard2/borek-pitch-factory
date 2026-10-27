import {
  ApiRequestError,
  apiFetch,
  approveDiscoveryPaper,
  generateDiscoveryPaper,
  resolveBackendOpportunityId,
  type PreviewClientSeed,
} from "./api";
import { DISCOVERY_PAGE_CATALOG, DISCOVERY_PAGE_STATES } from "./discoveryFirst";
import {
  DiscoveryWorkspaceError,
  type DiscoveryContentValue,
  type DiscoveryWorkspaceVersion,
} from "./discoveryWorkspace";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new DiscoveryWorkspaceError("validation", "The server returned an invalid Discovery payload.");
  }
  return value as Record<string, unknown>;
}

export function discoveryContentLabel(key: string): string {
  return key.replace(/_/g, " ").replace(/^./, (letter) => letter.toUpperCase());
}

// Keep every supplied field (including provenance and explicit unknowns), not just a summary.
export function formatDiscoveryContent(value: DiscoveryContentValue): string {
  if (value === null) return "Not supplied";
  if (Array.isArray(value)) return value.map((item) => `- ${formatDiscoveryContent(item)}`).join("\n");
  if (typeof value === "object") {
    return Object.entries(value).map(([key, item]) => `${discoveryContentLabel(key)}: ${formatDiscoveryContent(item)}`).join("\n\n");
  }
  return String(value);
}

export function emptyLiveDiscovery(opportunityId: string): DiscoveryWorkspaceVersion {
  return {
    opportunity_id: opportunityId,
    version_id: "No server version",
    revision: 0,
    document_state: "draft",
    source: "live",
    pdf_artifact_id: null,
    pdf_download_url: null,
    live: { status: "not_generated", document_id: null, server_version_id: null, version_number: null, client_name: "" },
    pages: DISCOVERY_PAGE_CATALOG.map((page) => ({
      ...page, state: "waiting", title: "", body: "", source_references: [], failure_message: null,
    })),
  };
}

/** GET/generate/PATCH return a paper; approval/version GET return a public version with paper_json. */
export function adaptLiveDiscovery(
  payload: unknown,
  opportunityId: string,
  backendId: string,
  versionMetadata?: unknown,
): DiscoveryWorkspaceVersion {
  const envelope = object(payload);
  const paper = envelope.paper_json === undefined ? envelope : object(envelope.paper_json);
  const metadata = versionMetadata === undefined
    ? (envelope.paper_json === undefined ? null : envelope)
    : object(versionMetadata);
  if (paper.opportunity_id !== backendId) {
    throw new DiscoveryWorkspaceError("authorization", "Discovery belongs to another opportunity.");
  }
  if (!Array.isArray(paper.pages) || paper.pages.length !== 7 ||
      !["not_generated", "generating", "ready", "failed"].includes(String(paper.status)) ||
      (paper.document_id !== null && (typeof paper.document_id !== "string" || !UUID.test(paper.document_id)))) {
    throw new DiscoveryWorkspaceError("validation", "The server returned an incomplete Discovery paper.");
  }
  if (metadata && (metadata.opportunity_id !== backendId || metadata.document_id !== paper.document_id ||
      typeof metadata.id !== "string" || !UUID.test(metadata.id) ||
      !Number.isInteger(metadata.version_number) || Number(metadata.version_number) < 1 ||
      !["draft", "approved"].includes(String(metadata.status)))) {
    throw new DiscoveryWorkspaceError("validation", "Discovery version metadata does not match this paper.");
  }
  const pages = DISCOVERY_PAGE_CATALOG.map((catalog, index) => {
    const matches = (paper.pages as unknown[]).map(object).filter((page) => page.key === catalog.id);
    const page = matches[0];
    if (matches.length !== 1 || page.order !== index + 1 || typeof page.title !== "string" ||
        !(DISCOVERY_PAGE_STATES as readonly unknown[]).includes(page.status)) {
      throw new DiscoveryWorkspaceError("validation", `Discovery page ${index + 1} is invalid.`);
    }
    const content = page.status === "ready" ? object(page.content) as Record<string, DiscoveryContentValue> : undefined;
    const refs = content?.source_refs;
    return {
      ...catalog,
      state: page.status as DiscoveryWorkspaceVersion["pages"][number]["state"],
      title: typeof content?.title === "string" ? content.title : page.title,
      body: content ? formatDiscoveryContent(content) : "",
      content,
      source_references: Array.isArray(refs) ? refs.map((value, refIndex) => {
        const ref = object(value);
        if (typeof ref.source_id !== "string" || typeof ref.locator !== "string" || typeof ref.excerpt !== "string") {
          throw new DiscoveryWorkspaceError("validation", "Discovery source metadata is invalid.");
        }
        return { id: `${catalog.id}-${refIndex}-${ref.source_id}`, label: ref.source_id, detail: `${ref.locator}\n${ref.excerpt}` };
      }) : [],
      failure_message: page.status === "failed" ? `The server reported that ${catalog.label} failed. No page retry endpoint is available.` : null,
    };
  });
  if ((paper.status === "ready") !== pages.every((page) => page.state === "ready") ||
      (pages.some((page) => page.state === "failed") && paper.status !== "failed") ||
      (paper.status !== "not_generated" && paper.document_id === null)) {
    throw new DiscoveryWorkspaceError("validation", "Discovery paper and page statuses do not agree.");
  }
  const context = object(paper.intake_context);
  return {
    ...emptyLiveDiscovery(opportunityId),
    version_id: metadata ? String(metadata.id) : String(paper.document_id ?? "No server version"),
    // The backend has version numbers, not optimistic edit revisions.
    revision: metadata ? Number(metadata.version_number) : 0,
    document_state: metadata?.status === "approved" ? "approved" : "draft",
    pages,
    live: {
      status: paper.status as NonNullable<DiscoveryWorkspaceVersion["live"]>["status"],
      document_id: paper.document_id as string | null,
      server_version_id: metadata ? String(metadata.id) : null,
      version_number: metadata ? Number(metadata.version_number) : null,
      client_name: typeof context.client_name === "string" ? context.client_name : "",
    },
  };
}

interface ReadOptions {
  signal?: AbortSignal;
  onPaper?: (version: DiscoveryWorkspaceVersion) => void;
}

async function withVersionMetadata(
  token: string, opportunityId: string, backendId: string, paper: unknown, options: ReadOptions,
): Promise<DiscoveryWorkspaceVersion> {
  options.signal?.throwIfAborted();
  const version = adaptLiveDiscovery(paper, opportunityId, backendId);
  options.onPaper?.(version);
  if (!version.live?.document_id || version.live.status === "generating") return version;
  const envelope = await apiFetch<{ versions: unknown[] }>(
    `/opportunities/${backendId}/discovery-paper/versions`, token, { signal: options.signal },
  );
  options.signal?.throwIfAborted();
  if (!Array.isArray(envelope.versions)) throw new DiscoveryWorkspaceError("validation", "Discovery version list is invalid.");
  const matching = envelope.versions.map(object)
    .filter((row) => row.document_id === version.live!.document_id)
    .sort((a, b) => Number(b.version_number) - Number(a.version_number));
  return matching.length ? adaptLiveDiscovery(paper, opportunityId, backendId, matching[0]) : version;
}

export async function loadLiveDiscovery(token: string, opportunityId: string, options: ReadOptions = {}): Promise<DiscoveryWorkspaceVersion> {
  options.signal?.throwIfAborted();
  const backendId = resolveBackendOpportunityId(opportunityId);
  // Loading must never create an opportunity just to replace a local fixture.
  if (!UUID.test(backendId)) return emptyLiveDiscovery(opportunityId);
  let paper: unknown;
  try {
    paper = await apiFetch(`/opportunities/${backendId}/discovery-paper`, token, { signal: options.signal });
  } catch (error) {
    options.signal?.throwIfAborted();
    if (error instanceof ApiRequestError && error.status === 404) return emptyLiveDiscovery(opportunityId);
    throw error;
  }
  return withVersionMetadata(token, opportunityId, backendId, paper, options);
}

export async function generateLiveDiscovery(
  token: string, opportunityId: string, seed: PreviewClientSeed | undefined, options: ReadOptions = {},
  language?: "en" | "de",
): Promise<DiscoveryWorkspaceVersion> {
  options.signal?.throwIfAborted();
  // This backend persists progressive snapshots during its synchronous POST. Poll GET only;
  // never issue another generate request to "continue" an in-flight paper.
  let finished = false;
  const polling = new AbortController();
  let timer: ReturnType<typeof setTimeout>;
  const stopPolling = () => {
    finished = true;
    clearTimeout(timer);
    polling.abort();
  };
  const poll = async () => {
    if (finished || options.signal?.aborted) return;
    try {
      const version = await loadLiveDiscovery(token, opportunityId, { signal: polling.signal });
      if (!finished && !options.signal?.aborted && version.live?.status !== "not_generated") options.onPaper?.(version);
    } catch { /* The POST result and subsequent explicit refresh report failures. */ }
    if (!finished && !options.signal?.aborted) timer = setTimeout(() => void poll(), 1000);
  };
  timer = setTimeout(() => void poll(), 750);
  options.signal?.addEventListener("abort", stopPolling, { once: true });
  let paper: unknown;
  try {
    // BAP-12: the client's document language, named explicitly (it also sets the journey language).
    paper = await generateDiscoveryPaper(token, opportunityId, seed, language);
  } finally {
    stopPolling();
    options.signal?.removeEventListener("abort", stopPolling);
  }
  if (options.signal?.aborted) throw new DOMException("Discovery request cancelled", "AbortError");
  return withVersionMetadata(token, opportunityId, resolveBackendOpportunityId(opportunityId), paper, options);
}

export async function approveLiveDiscovery(token: string, opportunityId: string): Promise<DiscoveryWorkspaceVersion> {
  const response = await approveDiscoveryPaper(token, opportunityId);
  const approved = adaptLiveDiscovery(response, opportunityId, resolveBackendOpportunityId(opportunityId));
  if (approved.document_state !== "approved" || !approved.live?.server_version_id) {
    throw new DiscoveryWorkspaceError("validation", "The server did not return an approved Discovery version.");
  }
  return approved;
}
