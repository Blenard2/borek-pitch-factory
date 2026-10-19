"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useAuth } from "@/components/AuthProvider";
import { postMeetingError } from "@/lib/postMeeting";
import {
  MISSING_TEXT, SOURCE_TEXT, headlineRequestFor, loadPresentationHeadline, opportunityStatement,
  type PresentationHeadline as Headline, type PresentationSource,
} from "@/lib/presentationHeadline";

/**
 * BAP-2: the Client Presentation heading - the client-specific title and the main opportunity
 * from the approved Discovery the presentation is built from.
 *
 * The Presentation page reports which Discovery its deck uses (see PresentationSource). Until it
 * has, nothing is loaded: the latest approved Discovery is read only once it is known that no
 * deck exists, and a deck pinned to a Discovery version always shows that version.
 */
const SourceContext = createContext<{ source: PresentationSource | null; setSource: (value: PresentationSource | null) => void }>({
  source: null,
  setSource: () => {},
});

export function PresentationSourceProvider({ children }: { children: ReactNode }) {
  const [source, setSource] = useState<PresentationSource | null>(null);
  return <SourceContext.Provider value={{ source, setSource }}>{children}</SourceContext.Provider>;
}

/** Called by the Presentation page with what it knows about its deck's Discovery. */
export function useReportPresentationSource(source: PresentationSource | null) {
  const { setSource } = useContext(SourceContext);
  const signature = source ? JSON.stringify(source) : "";
  useEffect(() => {
    setSource(signature ? (JSON.parse(signature) as PresentationSource) : null);
    return () => setSource(null);
  }, [signature, setSource]);
}

type View = { state: "loading" } | Headline | { state: "preview" } | { state: "legacy" } | { state: "unavailable" };

export function PresentationHeadline({ opportunityId, kicker, fallbackTitle }: { opportunityId: string; kicker: string; fallbackTitle: string }) {
  const { accessToken, previewMode } = useAuth();
  const live = Boolean(accessToken) && !previewMode;
  const { source } = useContext(SourceContext);
  const [result, setResult] = useState<{ key: string; view: View } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const request = headlineRequestFor(source, opportunityId);
  // A loaded view belongs to exactly one opportunity, one Discovery request and one attempt. A
  // response for an earlier key (navigation, regeneration, a slower request) is never shown.
  const key = `${opportunityId}:${request.load === "version" ? `version:${request.versionId}` : request.load}:${attempt}`;

  useEffect(() => {
    if (!live || !accessToken || request.load === "none") return;
    const controller = new AbortController();
    const versionId = request.load === "version" ? request.versionId : null;
    void loadPresentationHeadline(accessToken, opportunityId, versionId, controller.signal)
      .then((view) => { if (!controller.signal.aborted) setResult({ key, view }); })
      .catch((cause) => { if (!controller.signal.aborted) setResult({ key, view: { state: "failed", message: postMeetingError(cause) } }); });
    return () => controller.abort();
    // `key` covers the opportunity, the request and the attempt.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accessToken, live, key]);

  const view: View = !live
    ? { state: "preview" }
    : request.load === "none"
      ? request.view === "loading" ? { state: "loading" } : { state: request.view }
      : result?.key === key ? result.view : { state: "loading" };

  return (
    <div className="presentation-headline" data-testid="presentation-headline" data-state={view.state} aria-busy={view.state === "loading"}>
      <p className="discovery-workflow-kicker">{kicker}</p>
      {view.state === "ready" ? (
        <>
          <h2 data-testid="presentation-title">{view.title}</h2>
          {view.opportunity ? (
            <p className="presentation-headline-opportunity" data-testid="presentation-opportunity">
              <span>Main opportunity</span> {opportunityStatement(view.opportunity)}
            </p>
          ) : (
            <p className="presentation-headline-note" data-testid="presentation-opportunity-missing">The approved Discovery names no main opportunity.</p>
          )}
          <p className="discovery-workflow-meta">From the approved Discovery analysis{view.versionNumber ? ` · version ${view.versionNumber}` : ""}</p>
        </>
      ) : (
        <>
          <h2 data-testid="presentation-title-fallback">{fallbackTitle}</h2>
          <p className="presentation-headline-note" role={view.state === "failed" ? "alert" : "status"}>
            {view.state === "loading"
              ? "Loading the client-specific title and main opportunity..."
              : view.state === "preview"
                ? "The client-specific title and main opportunity are shown in a live session, from the approved Discovery analysis."
                : view.state === "legacy" || view.state === "unavailable"
                  ? SOURCE_TEXT[view.state]
                  : view.state === "failed"
                    ? `The client-specific title could not be loaded: ${view.message}`
                    : MISSING_TEXT[view.reason]}
            {view.state === "failed" ? <> <button type="button" className="btn btn-secondary" onClick={() => setAttempt((value) => value + 1)}>Retry</button></> : null}
          </p>
        </>
      )}
    </div>
  );
}
