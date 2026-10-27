"use client";

import { createContext, useContext, useEffect, useState } from "react";

import { useAuth } from "@/components/AuthProvider";
import type { ClientDirectoryItem } from "@/lib/clientDirectory";
import { normalizeClientInformation, normalizeClientInformationExtras, type ClientInformationExtras, type ClientInformationRecord } from "@/lib/clientInformation";
import type { ClientInformationViewModel, WorkflowSnapshotViewModel } from "@/lib/discoveryFirst";
import { createDiscoveryWorkspaceFixture, type DiscoveryWorkspaceVersion } from "@/lib/discoveryWorkspace";
import { DEFAULT_DOCUMENT_LANGUAGE, normalizeDocumentLanguage, type DocumentLanguage } from "@/lib/documentLanguage";
import {
  EMPTY_PREVIEW_JOURNEY,
  parsePreviewJourney,
  previewClientRecord,
  previewDirectoryItem,
  previewOpportunityId,
  previewJourneyStorageKey,
  withApprovedDiscovery,
  withPresentationAdvanced,
  withPresentationCompleted,
  withPresentationStarted,
  withUpdatedDiscovery,
  type PreviewJourneyState,
  type PreviewOpportunity,
} from "@/lib/previewJourney";
import { isLocalUiPreviewAvailable } from "@/lib/uiPreview";

interface PreviewJourneyContextValue {
  hydrated: boolean;
  opportunities: PreviewOpportunity[];
  directoryItems: ClientDirectoryItem[];
  getOpportunity: (opportunityId: string) => PreviewOpportunity | null;
  createOpportunity: (values: ClientInformationViewModel, extras: ClientInformationExtras, documentLanguage?: DocumentLanguage) => PreviewOpportunity;
  /** BAP-12: records the document language of a client held here (the server copy is saved separately). */
  setDocumentLanguage: (opportunityId: string, language: DocumentLanguage) => void;
  updateClient: (record: ClientInformationRecord, extras?: ClientInformationExtras) => void;
  registerLiveOpportunity: (opportunity: PreviewOpportunity) => void;
  updateDiscovery: (version: DiscoveryWorkspaceVersion) => void;
  approveDiscovery: (version: DiscoveryWorkspaceVersion) => void;
  startPresentation: (opportunityId: string) => void;
  advancePresentation: (opportunityId: string) => void;
  completePresentation: (opportunityId: string) => void;
}

const PreviewJourneyContext = createContext<PreviewJourneyContextValue | null>(null);

function workflow(
  revision: number,
  current_status: WorkflowSnapshotViewModel["current_status"],
  completed_statuses: WorkflowSnapshotViewModel["completed_statuses"],
): WorkflowSnapshotViewModel {
  return { revision, current_status, completed_statuses, blocked_reason: null, available_actions: [] };
}

export function PreviewJourneyProvider({ children }: { children: React.ReactNode }) {
  const { ownerId, loading } = useAuth();
  const [state, setState] = useState<PreviewJourneyState>(EMPTY_PREVIEW_JOURNEY);
  const [hydratedOwner, setHydratedOwner] = useState<string | null | undefined>(undefined);
  const hydrated = !loading && hydratedOwner === ownerId;

  useEffect(() => {
    if (loading) return;
    if (!ownerId || !isLocalUiPreviewAvailable()) {
      setState(EMPTY_PREVIEW_JOURNEY);
      setHydratedOwner(ownerId);
      return;
    }
    try {
      setState(parsePreviewJourney(JSON.parse(window.localStorage.getItem(previewJourneyStorageKey(ownerId)) ?? "null")));
    } catch {
      setState(EMPTY_PREVIEW_JOURNEY);
    }
    setHydratedOwner(ownerId);
  }, [ownerId, loading]);

  function commit(update: (current: PreviewJourneyState) => PreviewJourneyState) {
    if (!hydrated || !ownerId) return;
    setState((current) => {
      const next = update(current);
      if (ownerId && isLocalUiPreviewAvailable()) {
        try {
          window.localStorage.setItem(previewJourneyStorageKey(ownerId), JSON.stringify(next));
        } catch {
          // The in-memory workspace remains usable if browser persistence is unavailable.
        }
      }
      return next;
    });
  }

  function replaceOpportunity(opportunity: PreviewOpportunity) {
    commit((current) => ({
      ...current,
      opportunities: { ...current.opportunities, [opportunity.opportunity_id]: opportunity },
    }));
  }

  function getOpportunity(opportunityId: string) {
    return hydrated && ownerId ? state.opportunities[opportunityId] ?? null : null;
  }

  function createOpportunity(values: ClientInformationViewModel, extras: ClientInformationExtras, documentLanguage: DocumentLanguage = DEFAULT_DOCUMENT_LANGUAGE): PreviewOpportunity {
    const normalized = normalizeClientInformation(values);
    const opportunityId = previewOpportunityId(normalized.company_name);
    const now = new Date().toISOString();
    const baseDiscovery = createDiscoveryWorkspaceFixture(opportunityId);
    const discovery: DiscoveryWorkspaceVersion = {
      ...baseDiscovery,
      pages: baseDiscovery.pages.map((page) => {
        if (page.id === "cover") {
          return normalized.company_name
            ? { ...page, title: `${normalized.company_name} Discovery Paper`, body: `Prepared for the first conversation with ${normalized.company_name}.` }
            : { ...page, title: "Discovery Paper", body: "Prepared for the first conversation." };
        }
        if (page.id === "client_context") {
          return { ...page, body: normalized.additional_information || `${normalized.company_name || "Client"} context will be validated during the first meeting.` };
        }
        if (page.id === "opportunity") {
          return { ...page, body: normalized.meeting_purpose };
        }
        return page;
      }),
    };
    const opportunity: PreviewOpportunity = {
      opportunity_id: opportunityId,
      created_at: now,
      updated_at: now,
      client: previewClientRecord(opportunityId, normalized),
      client_extras: normalizeClientInformationExtras(extras),
      discovery,
      presentation: { state: "waiting", version_id: null, source_discovery_version_id: null, slide_count: 0 },
      workflow: workflow(1, "client_information", []),
      document_language: normalizeDocumentLanguage(documentLanguage),
    };
    replaceOpportunity(opportunity);
    return opportunity;
  }

  function setDocumentLanguage(opportunityId: string, language: DocumentLanguage) {
    const current = getOpportunity(opportunityId);
    if (!current || current.document_language === language) return;
    replaceOpportunity({ ...current, document_language: normalizeDocumentLanguage(language), updated_at: new Date().toISOString() });
  }

  function updateClient(record: ClientInformationRecord, extras?: ClientInformationExtras) {
    const current = getOpportunity(record.opportunity_id);
    if (!current) return;
    replaceOpportunity({ ...current, client: record, client_extras: extras ? normalizeClientInformationExtras(extras) : current.client_extras, updated_at: new Date().toISOString() });
  }

  function updateDiscovery(version: DiscoveryWorkspaceVersion) {
    const current = getOpportunity(version.opportunity_id);
    if (!current) return;
    replaceOpportunity({ ...withUpdatedDiscovery(current, version), updated_at: new Date().toISOString() });
  }

  function approveDiscovery(version: DiscoveryWorkspaceVersion) {
    const current = getOpportunity(version.opportunity_id);
    if (!current) return;
    replaceOpportunity({ ...withApprovedDiscovery(current, version), updated_at: new Date().toISOString() });
  }

  function startPresentation(opportunityId: string) {
    const current = getOpportunity(opportunityId);
    if (!current) return;
    replaceOpportunity({ ...withPresentationStarted(current), updated_at: new Date().toISOString() });
  }

  function completePresentation(opportunityId: string) {
    const current = getOpportunity(opportunityId);
    if (!current || current.presentation.state !== "generating") return;
    replaceOpportunity({ ...withPresentationCompleted(current), updated_at: new Date().toISOString() });
  }

  function advancePresentation(opportunityId: string) {
    const current = getOpportunity(opportunityId);
    if (!current || current.presentation.state !== "generating") return;
    replaceOpportunity({ ...withPresentationAdvanced(current), updated_at: new Date().toISOString() });
  }

  const opportunities = hydrated && ownerId ? Object.values(state.opportunities).sort((a, b) => b.updated_at.localeCompare(a.updated_at)) : [];
  return (
    <PreviewJourneyContext.Provider value={{
      hydrated,
      opportunities,
      directoryItems: opportunities.map(previewDirectoryItem),
      getOpportunity,
      createOpportunity,
      setDocumentLanguage,
      updateClient,
      registerLiveOpportunity: replaceOpportunity,
      updateDiscovery,
      approveDiscovery,
      startPresentation,
      advancePresentation,
      completePresentation,
    }}>
      {children}
    </PreviewJourneyContext.Provider>
  );
}

export function usePreviewJourney(): PreviewJourneyContextValue {
  const value = useContext(PreviewJourneyContext);
  if (!value) throw new Error("PreviewJourneyProvider is missing.");
  return value;
}
