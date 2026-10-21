"use client";

import { useEffect, useState } from "react";
import type { WorkflowDeckLine } from "@/lib/api";
import {
  POST_MEETING_LABEL, PRE_MEETING_LABEL, documentDate, downloadPostMeetingDocx, downloadPreMeetingDocx, downloadPresentationVersion,
  loadDiscoveryDocuments, loadPresentationVersions, printPreMeetingDiscovery, type DiscoveryDocuments,
} from "@/lib/postMeetingDocuments";
import type { EarlierVersion } from "@/lib/presentationLive";
import styles from "./post-meeting.module.css";

function saveBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = fileName;
    anchor.click();
  } finally { URL.revokeObjectURL(url); }
}

function safeName(value: string) {
  return value.replace(/[^\w .-]+/g, "").trim().replace(/\s+/g, "-").toLowerCase() || "client";
}

/**
 * BAP-11: the client's saved documents after the first meeting - the Pre-Meeting Discovery Document,
 * Presentation V1 and V2 and, once approved, the Post-Meeting Discovery Document. Every entry
 * loads and downloads on its own: one missing or failing document never hides the others.
 */
export function PostMeetingDocuments({ opportunityId, accessToken, master, clientName, ppt1, ppt2 }: {
  opportunityId: string; accessToken: string | null; master: boolean; clientName: string;
  ppt1: WorkflowDeckLine | null | undefined; ppt2: WorkflowDeckLine | null | undefined;
}) {
  const [discovery, setDiscovery] = useState<{ state: "loading" } | { state: "failed" } | { state: "ready"; value: DiscoveryDocuments }>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [versions, setVersions] = useState<Record<string, EarlierVersion[]>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const presentationIds = [...new Set([ppt1?.presentation_id, ppt2?.presentation_id].filter((value): value is string => Boolean(value)))];
  const presentationKey = presentationIds.join(",");

  useEffect(() => {
    if (!accessToken) return;
    const controller = new AbortController();
    setDiscovery({ state: "loading" });
    void loadDiscoveryDocuments(accessToken, opportunityId, controller.signal)
      .then((value) => setDiscovery({ state: "ready", value }))
      .catch(() => { if (!controller.signal.aborted) setDiscovery({ state: "failed" }); });
    return () => controller.abort();
  }, [accessToken, opportunityId, attempt]);

  useEffect(() => {
    if (!accessToken || !presentationKey) return;
    const controller = new AbortController();
    // Numbers and dates only: the presentations stay downloadable if this list cannot be read.
    for (const presentationId of presentationKey.split(",")) {
      void loadPresentationVersions(accessToken, presentationId, controller.signal)
        .then((rows) => setVersions((current) => ({ ...current, [presentationId]: rows })))
        .catch(() => undefined);
    }
    return () => controller.abort();
  }, [accessToken, presentationKey]);

  async function run(key: string, action: () => Promise<void>) {
    if (!accessToken || busy) return;
    setBusy(key);
    setErrors((current) => { const next = { ...current }; delete next[key]; return next; });
    try { await action(); } catch (cause) {
      setErrors((current) => ({ ...current, [key]: cause instanceof Error && cause.message ? cause.message : "The download failed. Try again." }));
    } finally { setBusy(null); }
  }

  const pre = discovery.state === "ready" ? discovery.value.pre_meeting : null;
  const post = discovery.state === "ready" ? discovery.value.post_meeting : null;
  // Presentation V1 is the version the Pre-Meeting Discovery Document belongs to; otherwise the latest V1.
  const v1Version = (pre?.state === "available" && pre.presentation_version_id) || ppt1?.latest_ready_version_id || null;
  const presentations = [
    { key: "v1", label: master ? "Master Presentation V1" : "Presentation V1", line: ppt1, versionId: v1Version },
    { key: "v2", label: master ? "Master Presentation V2" : "Presentation V2", line: ppt2, versionId: ppt2?.latest_ready_version_id ?? null },
  ];
  const client = safeName(clientName);

  return <section className={styles.documents} aria-labelledby="post-meeting-documents-title" data-testid="post-meeting-documents">
    <h2 id="post-meeting-documents-title">Documents</h2>
    <ul>
      <li data-testid="document-pre-meeting-discovery">
        <div><strong>{PRE_MEETING_LABEL}</strong>
          <small>{discovery.state === "loading" ? "Loading..." : discovery.state === "failed" ? "The Discovery documents could not be loaded."
            : pre?.state === "available" ? [`Version ${pre.version_number ?? "-"}`, pre.approved_at ? `approved ${documentDate(pre.approved_at)}` : ""].filter(Boolean).join(" · ")
            : pre?.message}</small></div>
        <div className={styles.documentActions}>
          {discovery.state === "failed" ? <button type="button" className="btn btn-secondary" onClick={() => setAttempt((value) => value + 1)}>Retry</button> : null}
          {pre?.state === "available" ? <>
            <button type="button" className="btn btn-secondary" disabled={busy !== null} onClick={() => void run("pre-pdf", () => printPreMeetingDiscovery(accessToken!, opportunityId, pre, clientName))}>
              {busy === "pre-pdf" ? "Preparing PDF..." : "PDF"}</button>
            {pre.docx_available ? <button type="button" className="btn btn-secondary" disabled={busy !== null}
              onClick={() => void run("pre-docx", async () => saveBlob(await downloadPreMeetingDocx(accessToken!, pre), `${client}-pre-meeting-discovery-v${pre.version_number ?? ""}.docx`))}>
              {busy === "pre-docx" ? "Downloading..." : "Word (DOCX)"}</button> : null}
          </> : null}
        </div>
        {errors["pre-pdf"] || errors["pre-docx"] ? <p className={styles.error} role="alert">{errors["pre-pdf"] ?? errors["pre-docx"]}</p> : null}
      </li>
      {presentations.map(({ key, label, line, versionId }) => {
        const version = line && versionId ? versions[line.presentation_id]?.find((row) => row.versionId === versionId) : undefined;
        return <li key={key} data-testid={`document-presentation-${key}`}>
          <div><strong>{label}</strong>
            <small>{!line || !versionId ? "Not available yet." : version ? [`Revision ${version.versionNumber}`, version.createdAt ? `created ${documentDate(version.createdAt)}` : ""].filter(Boolean).join(" · ") : "Saved version"}</small></div>
          {line && versionId ? <div className={styles.documentActions}>
            {(["pptx", "pdf"] as const).map((format) => <button key={format} type="button" className="btn btn-secondary" disabled={busy !== null}
              onClick={() => void run(`${key}-${format}`, async () => saveBlob(
                await downloadPresentationVersion(accessToken!, line.presentation_id, versionId, format),
                `${client}-${master ? "master-presentation" : "presentation"}-${key}${version ? `-revision-${version.versionNumber}` : ""}.${format}`,
              ))}>{busy === `${key}-${format}` ? "Downloading..." : format.toUpperCase()}</button>)}
          </div> : null}
          {errors[`${key}-pptx`] || errors[`${key}-pdf`] ? <p className={styles.error} role="alert">{errors[`${key}-pptx`] ?? errors[`${key}-pdf`]}</p> : null}
        </li>;
      })}
      {post?.state === "available" ? <li data-testid="document-post-meeting-discovery">
        <div><strong>{POST_MEETING_LABEL}</strong>
          <small>{[`Version ${post.version_number ?? "-"}`, post.approved_at ? `approved ${documentDate(post.approved_at)}` : ""].filter(Boolean).join(" · ")}</small></div>
        <div className={styles.documentActions}>
          <button type="button" className="btn btn-secondary" disabled={busy !== null}
            onClick={() => void run("post-docx", async () => saveBlob(await downloadPostMeetingDocx(accessToken!, post), `${client}-post-meeting-discovery-v${post.version_number ?? ""}.docx`))}>
            {busy === "post-docx" ? "Downloading..." : "Word (DOCX)"}</button>
        </div>
        {errors["post-docx"] ? <p className={styles.error} role="alert">{errors["post-docx"]}</p> : null}
      </li> : null}
    </ul>
  </section>;
}
