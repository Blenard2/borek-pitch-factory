"""Shared audit logging utility — actor + action + timestamp on state changes (AT-52)."""

from __future__ import annotations

import logging
from enum import StrEnum
from uuid import UUID

from app.services.data import DataStore

logger = logging.getLogger(__name__)


class AuditAction(StrEnum):
    OPPORTUNITY_CREATE = "opportunity.create"
    OPPORTUNITY_UPDATE = "opportunity.update"
    CLIENT_LOGO_UPLOAD = "client_logo.upload"
    CLIENT_LOGO_REPLACE = "client_logo.replace"
    CLIENT_LOGO_DELETE = "client_logo.delete"
    TRANSCRIPT_UPLOAD = "transcript.upload"
    TRANSCRIPT_REGENERATE = "transcript.regenerate"
    TRANSCRIPT_DELETE = "transcript.delete"
    FRAMEWORK_GENERATE = "framework.generate"
    FRAMEWORK_REGENERATE_CHAPTER = "framework.regenerate_chapter"
    FRAMEWORK_CONFIRM = "framework.confirm"
    FRAMEWORK_UPDATE = "framework.update"
    FRAMEWORK_RENDER = "framework.render"
    PRESENTATION_PLAN_GENERATE = "presentation_plan.generate"
    PRESENTATION_GENERATE = "presentation.generate"
    SLIDE_REGENERATE = "slide.regenerate"
    SLIDE_CHANGE_LAYOUT = "slide.change_layout"
    ARTIFACT_FILE = "artifact.file"
    AUTH_LOGIN = "auth.login"
    ROLE_ASSIGN = "role.assign"
    STAGE1_RESEARCH_GENERATE = "stage1_research.generate"
    STAGE1_VOICE_REQUEST = "stage1_voice.request"
    STAGE1_OUTPUTS_GENERATE = "stage1_outputs.generate"
    STAGE1_PRESENTATION_REGENERATE = "stage1_presentation.regenerate"
    PPT2_PRESENTATION_GENERATE = "ppt2_presentation.generate"
    PPT2_PRESENTATION_REGENERATE = "ppt2_presentation.regenerate"
    STAGE2_OUTPUTS_GENERATE = "stage2_outputs.generate"
    MEETING_FEEDBACK_UPDATE = "meeting_feedback.update"
    PERSONAL_NOTES_UPDATE = "personal_notes.update"
    MEETING_EXTRACTION_GENERATE = "meeting_extraction.generate"
    MEETING_REVIEW_CONFIRM = "meeting_review.confirm"
    MASTER_PRESENTATION_V2_GENERATE = "master_presentation_v2.generate"
    SELECTED_USE_CASES_UPDATE = "selected_use_cases.update"
    WORKFLOW_FIRST_MEETING_COMPLETED = "workflow.first_meeting_completed"
    WORKFLOW_OWNER_REVIEWED = "workflow.owner_reviewed"
    WORKFLOW_FINALIZED = "workflow.finalized"
    EMAIL_DRAFT_GENERATE = "email_draft.generate"
    EMAIL_DRAFT_CONFIRM = "email_draft.confirm"
    EMAIL_DRAFT_UPDATE = "email_draft.update"
    EMAIL_DRAFT_EXPORT = "email_draft.export"
    CLIENT_PREPARATION_EMAIL_GENERATE = "client_preparation_email.generate"
    DISCOVERY_PAPER_GENERATE = "discovery_paper.generate"
    DISCOVERY_PAPER_EDIT = "discovery_paper.edit"
    DISCOVERY_PAPER_APPROVE = "discovery_paper.approve"
    DISCOVERY2_GENERATE = "discovery2.generate"
    DISCOVERY2_EDIT = "discovery2.edit"
    DISCOVERY2_APPROVE = "discovery2.approve"
    CLIENT_DOCUMENT_UPLOAD = "client_document.upload"
    CLIENT_DOCUMENT_DELETE = "client_document.delete"


class AuditObjectType(StrEnum):
    OPPORTUNITY = "opportunity"
    CLIENT_LOGO = "client_logo"
    TRANSCRIPT = "transcript"
    FRAMEWORK_VERSION = "framework_version"
    PRESENTATION_PLAN = "presentation_plan"
    PRESENTATION = "presentation"
    SLIDE = "slide"
    FILED_ARTIFACT = "filed_artifact"
    CLIENT_DOCUMENT = "client_document"
    EMPLOYEE = "employee"
    SESSION = "session"


def record_audit_event(
    store: DataStore,
    *,
    actor_id: UUID,
    action: AuditAction | str,
    object_type: AuditObjectType | str,
    object_id: UUID,
    document_id: str | None = None,
    version_id: UUID | None = None,
    actor_email: str | None = None,
) -> None:
    """Persist actor, action, object reference, and timestamp for a state change.

    Audit writes must not fail the originating 202. A timeout on the audit
    insert after a successful enqueue is what produced "Failed to fetch" in
    the live UI.
    """
    try:
        reference = document_id or str(object_id)
        if version_id is not None:
            reference = f"{document_id}:{version_id}" if document_id else str(version_id)
        store.append_audit_log(
            actor_id=actor_id,
            action=str(action),
            object_type=str(object_type),
            object_id=object_id,
            document_id=reference,
            actor_email=actor_email,
        )
    except Exception:
        logger.warning(
            "Audit log write failed for %s on %s %s; continuing",
            action,
            object_type,
            object_id,
            exc_info=True,
        )
