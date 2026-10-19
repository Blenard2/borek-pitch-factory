"use client";

import { useEffect, useRef, useState } from "react";
import {
  ATTACHMENT_ARTIFACT_LABEL, EMAIL_LENGTH_LABEL, EMAIL_REVIEW_CHECKS, emailReviewWarnings, formatFileSize, personLabel,
  type EmailLength, type PostMeetingEmail,
} from "@/lib/postMeetingEmail";
import styles from "./post-meeting.module.css";

const dateTime = (value: string | null) => (value ? new Date(value).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "");

/**
 * Review & confirm: the saved email as it will be exported, with everything the owner has to check.
 *
 * The approval is recorded exactly as it is given here, nothing more:
 *   - every point to check has its own tick; only the review flags the owner ticked are reported
 *     as acknowledged, and the approval stays disabled until each one is ticked;
 *   - the six review checks are written out and confirmed with one tick that names them; without
 *     it no check is reported.
 * Nothing starts ticked. The API validates every check, flag, source and file again.
 *
 * ``readOnly`` shows a confirmed draft again, with what was acknowledged, and approves nothing.
 */
export function FollowUpReviewDialog({ draft, length, companyName, busy, readOnly, error, onApprove, onClose }: {
  draft: PostMeetingEmail; length: EmailLength; companyName: string; busy: boolean; readOnly: boolean; error: string | null;
  onApprove: (acknowledgedFlags: string[], checksConfirmed: boolean) => void; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement | null>(null);
  // The points the owner ticked, by key, and whether they confirmed the six review checks.
  const [acknowledged, setAcknowledged] = useState<string[]>([]);
  const [checksConfirmed, setChecksConfirmed] = useState(false);
  // A modal <dialog> keeps focus inside, closes on Escape and returns focus to the opening button.
  // It is closed by the user (which reports onClose) or by being unmounted - never from a cleanup,
  // which would report a close the owner did not make.
  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) element.showModal();
  }, []);

  const text = draft.lengths[length];
  const warnings = emailReviewWarnings(draft, length);
  const blocking = warnings.filter((item) => item.blocking);
  const toAcknowledge = warnings.filter((item) => !item.blocking);
  const open = toAcknowledge.filter((item) => !acknowledged.includes(item.key));
  const acknowledgedFlags = warnings.filter((item) => item.flag && acknowledged.includes(item.key)).map((item) => item.key);
  const files = draft.attachments.filter((item) => item.selected);
  const recipients = draft.recipients;
  const canApprove = !busy && blocking.length === 0 && open.length === 0 && checksConfirmed;

  return <dialog ref={dialog} className={styles.reviewDialog} aria-labelledby="email-review-dialog-title" aria-describedby="email-review-dialog-note" data-testid="email-review-dialog"
    onCancel={(event) => { if (busy) event.preventDefault(); }} onClose={onClose}>
    <header>
      <span className={styles.eyebrow}>Owner review</span>
      <h2 id="email-review-dialog-title">{readOnly ? "Reviewed and confirmed" : "Review & confirm"}</h2>
      <p id="email-review-dialog-note">{readOnly
        ? `Confirmed ${dateTime(draft.confirmedAt)} for revision ${draft.confirmedRevision}. Nothing has been sent. Editing the text or the attachments withdraws the confirmation.`
        : "This is the saved email as it will be exported. Read it, tick each point you have checked, then approve. Nothing is sent."}</p>
    </header>

    <div className={styles.reviewScroll}>
      <dl className={styles.emailFields} data-testid="email-review-summary">
        <div><dt>To</dt><dd>{recipients ? recipients.to.map(personLabel).join(", ") : "No recipient entered"}{companyName ? ` · ${companyName}` : ""}</dd></div>
        {recipients?.cc.length ? <div><dt>Cc</dt><dd>{recipients.cc.map(personLabel).join(", ")}</dd></div> : null}
        <div><dt>From</dt><dd>{recipients ? `${recipients.sender.name} <${recipients.sender.email}>` : "No sender entered"}</dd></div>
        <div><dt>Subject</dt><dd>{text.subject}</dd></div>
        <div><dt>Length</dt><dd>{EMAIL_LENGTH_LABEL[length]} · {text.wordCount} of {draft.wordLimits[length]} content words · revision {draft.revision}</dd></div>
        <div><dt>Attachments</dt><dd data-testid="email-review-attachments">{files.length
          ? files.map((item) => `${item.fileName} (${ATTACHMENT_ARTIFACT_LABEL[item.artifact]}, ${item.available ? `${formatFileSize(item.sizeBytes)}${item.origin === "generated" ? `, approved version ${item.versionNumber ?? ""}` : ""}` : "not available"})`).join(", ")
          : "None selected"}</dd></div>
      </dl>

      {/* The points to check come first, so they are on screen before the message is read. */}
      <section aria-labelledby="email-review-warnings-title" data-testid="email-review-warnings">
        <h3 id="email-review-warnings-title" className={styles.packageLabel}>{warnings.length
          ? readOnly ? `${warnings.length} ${warnings.length === 1 ? "point" : "points"} you acknowledged` : `${warnings.length} ${warnings.length === 1 ? "point" : "points"} to check — tick each one`
          : "Nothing was flagged for this email"}</h3>
        {warnings.length ? <ul className={styles.reviewWarnings}>
          {warnings.map((item) => <li key={item.key} data-testid={`email-warning-${item.key}`} data-blocking={item.blocking ? "true" : undefined}>
            {item.blocking || readOnly
              ? <span>{item.blocking ? <strong>Cannot be approved: </strong> : null}{item.text}</span>
              : <label className={styles.check}>
                <input type="checkbox" data-testid={`email-acknowledge-${item.key}`} checked={acknowledged.includes(item.key)} disabled={busy}
                  onChange={(event) => setAcknowledged(event.target.checked ? [...acknowledged, item.key] : acknowledged.filter((key) => key !== item.key))} />
                <span>{item.text}</span>
              </label>}
          </li>)}
        </ul> : null}
        {!readOnly && toAcknowledge.length ? <small data-testid="email-review-open" role="status">{open.length ? `${open.length} of ${toAcknowledge.length} still to tick` : "Every point is ticked"}</small> : null}
      </section>

      <section aria-labelledby="email-review-text-title">
        <h3 id="email-review-text-title" className={styles.packageLabel}>Message</h3>
        {/* Focusable so the text can be scrolled with the keyboard. */}
        <pre className={styles.reviewBody} tabIndex={0} data-testid="email-review-body">{text.body}</pre>
      </section>

      {!readOnly ? <fieldset className={styles.reviewCovers} disabled={busy} data-testid="email-review-covers">
        <legend className={styles.packageLabel}>The six review checks</legend>
        <ol>{EMAIL_REVIEW_CHECKS.map(([key, label]) => <li key={key}>{label}</li>)}</ol>
        <label className={styles.check}>
          <input type="checkbox" data-testid="email-review-checks" checked={checksConfirmed} onChange={(event) => setChecksConfirmed(event.target.checked)} />
          <span>I have checked all six points above and confirm each of them for this email.</span>
        </label>
      </fieldset> : null}
    </div>

    <footer>
      {blocking.length ? <p className={styles.error} role="alert">Resolve the {blocking.length === 1 ? "point" : "points"} marked “Cannot be approved” first, then review again.</p> : null}
      {error ? <p className={styles.error} role="alert" data-testid="email-review-error">{error}</p> : null}
      {!readOnly && !blocking.length && !canApprove && !busy ? <small data-testid="email-review-missing">{[
        open.length ? `tick the ${open.length === 1 ? "remaining point" : `${open.length} remaining points`} to check` : "",
        checksConfirmed ? "" : "confirm the six review checks",
      ].filter(Boolean).join(" and ").replace(/^./, (first) => first.toUpperCase())} to approve.</small> : null}
      <div className={styles.actions}>
        {!readOnly ? <button type="button" className="btn btn-primary" data-testid="email-approve" disabled={!canApprove} onClick={() => onApprove(acknowledgedFlags, checksConfirmed)}>{busy ? "Approving..." : "Approve email"}</button> : null}
        <button type="button" className="btn btn-secondary" data-testid="email-review-close" disabled={busy} onClick={() => dialog.current?.close()}>{readOnly ? "Close" : "Back to editing"}</button>
      </div>
      {!readOnly ? <small>Approving records your review of this saved revision. It does not deliver the email: you export it and send it yourself.</small> : null}
    </footer>
  </dialog>;
}
