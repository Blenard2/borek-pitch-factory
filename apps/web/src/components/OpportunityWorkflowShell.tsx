"use client";

import type { ReactNode } from "react";
import { usePathname } from "next/navigation";

import { DiscoveryWorkflowStepper } from "@/components/DiscoveryWorkflowStepper";
import { RequireAuth } from "@/components/RequireAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { useLanguage } from "@/components/LanguageProvider";
import { usePreviewJourney } from "@/components/PreviewJourneyProvider";
import { useAuth } from "@/components/AuthProvider";
import { displayClientName } from "@/lib/clientInformation";
import type { DiscoveryFirstWorkspaceFixture } from "@/lib/discoveryFirst";
import { PostMeetingShell } from "@/components/PostMeetingShell";

interface OpportunityWorkflowShellProps {
  fixture: DiscoveryFirstWorkspaceFixture;
  children: ReactNode;
}

export function OpportunityWorkflowShell({ fixture, children }: OpportunityWorkflowShellProps) {
  const { copy } = useLanguage();
  const pathname = usePathname();
  const { getOpportunity } = usePreviewJourney();
  const { accessToken, previewMode } = useAuth();
  const live = Boolean(accessToken) && !previewMode;
  const preview = getOpportunity(fixture.opportunity_id);
  const workflowSnapshot = preview?.workflow ?? fixture.workflow;
  const preMeetingStatuses = new Set(["client_information", "discovery_prepared", "ppt_1_ready"]);
  const preMeetingPage = /\/(client-information|discovery|presentations)$/.test(pathname);
  const activeSection = preMeetingPage || preMeetingStatuses.has(workflowSnapshot.current_status)
    ? "pre_meeting"
    : "post_meeting";
  // Neither the analysis nor the deck has a fixed length, so progress is never "n of 7".
  const workspaceProgress = pathname.endsWith("/presentations")
    ? `${preview?.presentation.slide_count ?? 0} slides ready`
    : preview?.discovery.document_state === "approved" ? "Discovery approved" : "Discovery in review";
  if (/\/(meeting|review|follow-up|post-meeting-discovery|post-meeting-presentation)$/.test(pathname)) {
    return <RequireAuth><PostMeetingShell opportunityId={fixture.opportunity_id}
      companyName={preview?.client.values.company_name ?? fixture.client_information.company_name}
      contactPerson={preview?.client.values.contact_person ?? fixture.client_information.contact_person}>
      {children}
    </PostMeetingShell></RequireAuth>;
  }
  return (
    <RequireAuth>
      <div className="app-workspace discovery-workflow-shell">
        <SiteHeader opportunityId={fixture.opportunity_id} activeSection={activeSection} />
        <main className="discovery-workflow-main">
          <div className="discovery-workflow-heading">
            <div>
              <p className="discovery-workflow-kicker">{activeSection === "pre_meeting" ? copy.sidebar.preMeeting : copy.sidebar.postMeeting} · {displayClientName(preview?.client.values.company_name ?? fixture.client_information.company_name, copy.clients.unnamed)}</p>
              <h2>{copy.workflow.creating}</h2>
              <p className="discovery-workflow-meta">{live ? "Live workspace · Progress is shown with each artifact below." : workspaceProgress}</p>
            </div>
          </div>
          {activeSection === "post_meeting" ? (
            <DiscoveryWorkflowStepper
              opportunityId={fixture.opportunity_id}
              workflow={workflowSnapshot}
            />
          ) : null}
          {workflowSnapshot.blocked_reason ? (
            <p className="discovery-contract-notice" role="status">
              <strong>{copy.workflow.blocked}</strong> {workflowSnapshot.blocked_reason}
            </p>
          ) : null}
          {children}
        </main>
      </div>
    </RequireAuth>
  );
}
