"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useAuth } from "@/components/AuthProvider";
import { postMeetingError } from "@/lib/postMeeting";
import { MISSING_TEXT, loadPresentationHeadline, opportunityStatement, type PresentationHeadline as Headline } from "@/lib/presentationHeadline";

/**
 * BAP-2: the Client Presentation heading - the client-specific title and the main opportunity
 * from the approved Discovery the presentation is built from.
 *
 * The Presentation page reports the Discovery version its deck was built from; until a deck
 * exists the latest approved Discovery is used.
 */
const SourceContext = createContext<{ versionId: string | null; setVersionId: (value: string | null) => void }>({
  versionId: null,
  setVersionId: () => {},
});

export function PresentationSourceProvider({ children }: { children: ReactNode }) {
  const [versionId, setVersionId] = useState<string | null>(null);
  return <SourceContext.Provider value={{ versionId, setVersionId }}>{children}</SourceContext.Provider>;
}

/** Called by the Presentation page with the approved Discovery version of the shown deck. */
export function useReportPresentationSource(versionId: string | null) {
  const { setVersionId } = useContext(SourceContext);
  useEffect(() => {
    setVersionId(versionId);
    return () => setVersionId(null);
  }, [versionId, setVersionId]);
}

type View = { state: "loading" } | Headline | { state: "preview" };

export function PresentationHeadline({ opportunityId, kicker, fallbackTitle }: { opportunityId: string; kicker: string; fallbackTitle: string }) {
  const { accessToken, previewMode } = useAuth();
  const live = Boolean(accessToken) && !previewMode;
  const { versionId } = useContext(SourceContext);
  const [result, setResult] = useState<{ key: string; view: View } | null>(null);
  const [attempt, setAttempt] = useState(0);
  // A view belongs to one opportunity and one source version; another one is never shown, not even briefly.
  const key = `${opportunityId}:${versionId ?? "latest"}:${attempt}`;

  useEffect(() => {
    if (!live || !accessToken) return;
    const controller = new AbortController();
    void loadPresentationHeadline(accessToken, opportunityId, versionId, controller.signal)
      .then((view) => { if (!controller.signal.aborted) setResult({ key, view }); })
      .catch((cause) => { if (!controller.signal.aborted) setResult({ key, view: { state: "failed", message: postMeetingError(cause) } }); });
    return () => controller.abort();
  }, [accessToken, live, opportunityId, versionId, key]);

  const view: View = !live ? { state: "preview" } : result?.key === key ? result.view : { state: "loading" };

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
