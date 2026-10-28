"""BT-36 / TSK-013 retrieval APIs for MS-35."""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth import get_current_user
from app.dependencies import AuthUserDep, DataStoreDep
from app.services.api_errors import bad_request, conflict
from app.services.audit import AuditAction, AuditObjectType, record_audit_event
from app.services import followup_email
from app.services.discovery_paper import (
    approve_discovery_paper,
    edit_discovery_paper,
    generate_discovery_paper,
    get_discovery_paper,
    get_discovery_paper_version,
    get_latest_approved_discovery_paper,
    list_discovery_paper_versions,
)
from app.services.stage1 import get_company_research_provider
from services.framework.stage1_research import CompanyResearchProvider
from services.language import ContentLanguage
from app.services.content_language import (
    approved_discovery1_language,
    approved_discovery2_language,
    require_source_language,
)
from app.services.journey_generation import (
    JOURNEY_STAGES,
    client_preparation_envelope,
    confirm_email_draft,
    empty_stage1,
    empty_stage2,
    envelope_from_stored,
    generate_client_preparation_email,
    generate_email_draft,
    generate_stage1_outputs,
    generate_stage2_outputs,
    regenerate_stage1_presentation,
    get_meeting_feedback,
)
from app.services.meeting_extraction import (
    empty_meeting_extraction,
    generate_meeting_extraction,
    get_meeting_extraction,
    personal_notes_view,
)
from app.services.master_presentation_v2 import build_master_v2_status, enqueue_master_presentation_v2
from app.services.post_meeting_discovery import (
    approve_post_meeting_discovery,
    discovery1_docx,
    discovery2_docx,
    edit_post_meeting_discovery,
    generate_post_meeting_discovery,
    get_post_meeting_discovery,
    get_post_meeting_discovery_version,
    list_post_meeting_discovery_versions,
)
from app.services.post_meeting_review import build_post_meeting_review, confirm_meeting_review
from app.services.ppt2_context import build_ppt2_context
from app.services.workflow_status import (
    build_workflow_status,
    mark_finalized,
    mark_first_meeting_completed,
    mark_owner_reviewed,
)
from app.services.use_case_selection import (
    get_selected_use_cases,
    list_available_use_cases,
    replace_selected_use_cases,
)

router = APIRouter(dependencies=[Depends(get_current_user)])


class MeetingFeedbackUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=20_000)


class PersonalNotesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=20_000)


class MeetingExtractionGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript_id: UUID


class MeetingReviewConfirmRequest(BaseModel):
    """The source revisions the owner reviewed, and the findings they exclude."""

    model_config = ConfigDict(extra="forbid")

    transcript_id: UUID
    extraction_generated_at: str
    # The fingerprint the review was loaded with; anything that changed since is refused (409).
    review_fingerprint: str = Field(pattern="^[0-9a-f]{64}$")
    excluded: dict[str, list[str]] = {}


class SelectedUseCasesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_case_ids: list[str]


class EmailGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    journey_stage: Literal["first_contact", "deepening", "concretisation"]
    # Regenerating replaces all three lengths; saved edits or a confirmation need this consent.
    overwrite_edits: bool = False
    # BAP-13: the email's language; without it, the language of the package it is written from.
    language: ContentLanguage | None = None


class GenerationLanguageRequest(BaseModel):
    """BAP-13: optional body of a generate call - the content language (``en`` or ``de``).

    Without it, the language of the approved source the artifact is built from applies (and the
    opportunity's language for Discovery 1), so a journey stays in one language.
    """

    model_config = ConfigDict(extra="forbid")

    language: ContentLanguage | None = None


class EmailLengthEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(..., min_length=1, max_length=400)
    body: str = Field(..., min_length=1, max_length=40000)


class EmailLengthEdits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    short: EmailLengthEdit | None = None
    medium: EmailLengthEdit | None = None
    extensive: EmailLengthEdit | None = None


_GENERATED_ATTACHMENT_KEYS = frozenset({"pptx", "pdf", "v1_pptx", "v1_pdf", "discovery1_docx", "discovery2_docx"})
# BAP-3: an uploaded file is selected by its stable id, never by its name.
_EXTERNAL_ATTACHMENT_KEY = re.compile(r"^external:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _checked_selection(value: dict[str, bool] | None) -> dict[str, bool] | None:
    if value is None:
        return None
    if len(value) > 30:
        raise ValueError("Too many attachments in one request")
    unknown = [key for key in value if key not in _GENERATED_ATTACHMENT_KEYS and not _EXTERNAL_ATTACHMENT_KEY.match(key)]
    if unknown:
        raise ValueError(f"Unknown attachment: {', '.join(sorted(unknown))}")
    return value


class EmailUpdateRequest(BaseModel):
    """Save edits to one draft. ``expected_revision`` is the revision the edit was made on."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(..., ge=1)
    selected_length: Literal["short", "medium", "extensive"] | None = None
    lengths: EmailLengthEdits | None = None
    # The six generated files (pptx, pdf, v1_pptx, v1_pdf, discovery1_docx, discovery2_docx) and
    # the uploaded files as "external:<id>"; whether an uploaded id exists is checked by the service.
    attachments: dict[str, bool] | None = None

    @model_validator(mode="after")
    def _attachment_keys(self) -> "EmailUpdateRequest":
        _checked_selection(self.attachments)
        return self


class DiscoveryPaperPageEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Literal[
        "cover",
        "client_context",
        "opportunity",
        "borek_approach",
        "relevant_use_case",
        "pilot_proposal",
        "next_steps",
    ]
    content: dict


class DiscoveryAnalysisSectionEdit(BaseModel):
    """One logical section of an AI Opportunity Analysis, e.g. ``opportunity:<area>/<id>``."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(pattern=r"^[a-z_]+(:[a-z0-9_]+(/[a-z0-9_]+)?)?$", max_length=160)
    value: dict


class DiscoveryPaperEditRequest(BaseModel):
    """``edits`` for an AI Opportunity Analysis (schema 2.0), ``pages`` for a stored v1 paper."""

    model_config = ConfigDict(extra="forbid")

    expected_document_id: UUID | None = None
    pages: list[DiscoveryPaperPageEdit] | None = Field(default=None, min_length=1)
    edits: list[DiscoveryAnalysisSectionEdit] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_kind(self) -> "DiscoveryPaperEditRequest":
        if (self.pages is None) == (self.edits is None):
            raise ValueError("Provide either pages or edits")
        return self


class DiscoveryPaperGenerateRequest(BaseModel):
    """Parts 5 and 6 of the analysis are optional and off unless explicitly requested."""

    model_config = ConfigDict(extra="forbid")

    roles_employees: bool = False
    decision_map: bool = False
    system_interfaces: bool = False
    # BAP-13: the content language; without it, the opportunity's language (English by default).
    language: ContentLanguage | None = None


class Discovery2ItemEdit(BaseModel):
    """One item in its new position. Without ``id`` it is a new item written by the owner."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=80)
    text: str = Field(max_length=4000)
    # Only for an item from the owner's personal notes: share it with the client (true) or keep
    # it internal (false). Omitted: unchanged.
    share_with_client: bool | None = None


class Discovery2SectionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Literal[
        "context", "requirements", "challenges", "priorities", "opportunities", "discussed_solutions", "decisions", "follow_ups", "open_questions"
    ]
    items: list[Discovery2ItemEdit] = Field(max_length=60)


class Discovery2EditRequest(BaseModel):
    """``expected_version_id`` and ``expected_content_hash`` name the version the edit was made on."""

    model_config = ConfigDict(extra="forbid")

    expected_version_id: UUID
    expected_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sections: list[Discovery2SectionEdit] = Field(min_length=1, max_length=9)


class Discovery2ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version_id: UUID
    expected_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _docx_response(exported: dict) -> Response:
    ascii_name = exported["file_name"].encode("ascii", "ignore").decode() or "discovery.docx"
    return Response(
        content=exported["content"],
        media_type=exported["media_type"],
        headers={"Content-Disposition": f'attachment; filename="{ascii_name}"', "Cache-Control": "no-store"},
    )


class EmailConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_length: Literal["short", "medium", "extensive"]
    # Required for a draft written from a finalized Master Presentation V2 package.
    expected_revision: int | None = Field(default=None, ge=1)
    review_checks: list[str] | None = Field(default=None, max_length=20)
    acknowledged_flags: list[str] | None = Field(default=None, max_length=50)


@router.get("/{opportunity_id}/stage1-outputs")
def get_stage1_outputs(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    return opportunity.get("stage1_outputs") or empty_stage1(opportunity_id)


@router.post("/{opportunity_id}/stage1-outputs/generate")
def post_stage1_outputs(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    body: GenerationLanguageRequest | None = None,
) -> dict:
    _require_v1_language(store, opportunity_id=opportunity_id, user_id=user.id, body=body)
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.STAGE1_OUTPUTS_GENERATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    return generate_stage1_outputs(store, opportunity_id=opportunity_id, user_id=user.id)


@router.post("/{opportunity_id}/stage1-outputs/presentation/regenerate")
def post_regenerate_stage1_presentation(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    body: GenerationLanguageRequest | None = None,
) -> dict:
    _require_v1_language(store, opportunity_id=opportunity_id, user_id=user.id, body=body)
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.STAGE1_PRESENTATION_REGENERATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    return regenerate_stage1_presentation(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


def _require_v1_language(store, *, opportunity_id: UUID, user_id: UUID, body: GenerationLanguageRequest | None) -> None:
    """BAP-13: Master Presentation V1 is built in the language of the approved Discovery 1."""
    if body is None or body.language is None:
        return
    source = approved_discovery1_language(store, opportunity_id=opportunity_id, user_id=user_id)
    if source is not None:
        require_source_language(body.language, source, artifact="Master Presentation V1", source="Discovery document")


@router.get("/{opportunity_id}/discovery-paper")
def get_opportunity_discovery_paper(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return get_discovery_paper(store, opportunity_id=opportunity_id, user_id=user.id)


@router.post("/{opportunity_id}/discovery-paper/generate")
def post_opportunity_discovery_paper(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    provider: CompanyResearchProvider | None = Depends(get_company_research_provider),
    body: DiscoveryPaperGenerateRequest | None = None,
) -> dict:
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.DISCOVERY_PAPER_GENERATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    return generate_discovery_paper(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
        provider=provider,
        optional_parts=body.model_dump(exclude={"language"}) if body else None,
        language=body.language if body else None,
    )


@router.patch("/{opportunity_id}/discovery-paper")
def patch_opportunity_discovery_paper(
    opportunity_id: UUID,
    body: DiscoveryPaperEditRequest,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    paper, version_id = edit_discovery_paper(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
        pages=[page.model_dump() for page in body.pages] if body.pages else None,
        edits=[edit.model_dump() for edit in body.edits] if body.edits else None,
        expected_document_id=str(body.expected_document_id) if body.expected_document_id else None,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.DISCOVERY_PAPER_EDIT,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
        document_id=str(paper["document_id"]),
        version_id=version_id,
    )
    return paper


@router.post("/{opportunity_id}/discovery-paper/approve")
def post_opportunity_discovery_paper_approve(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    approved = approve_discovery_paper(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.DISCOVERY_PAPER_APPROVE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
        document_id=str(approved["document_id"]),
        version_id=UUID(approved["id"]),
    )
    return approved


@router.get("/{opportunity_id}/discovery-paper/approved")
def get_opportunity_approved_discovery_paper(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return get_latest_approved_discovery_paper(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


@router.get("/{opportunity_id}/discovery-paper/versions")
def get_opportunity_discovery_paper_versions(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return {
        "versions": list_discovery_paper_versions(
            store,
            opportunity_id=opportunity_id,
            user_id=user.id,
        )
    }


@router.get("/{opportunity_id}/discovery-paper/versions/{version_id}")
def get_opportunity_discovery_paper_version(
    opportunity_id: UUID,
    version_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return get_discovery_paper_version(
        store,
        opportunity_id=opportunity_id,
        version_id=version_id,
        user_id=user.id,
    )


@router.get("/{opportunity_id}/discovery-paper/versions/{version_id}/download/docx")
def get_opportunity_discovery_paper_docx(
    opportunity_id: UUID,
    version_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> Response:
    """BAP-7: the Discovery 1 version as a Word file (attachable to the follow-up email)."""
    return _docx_response(discovery1_docx(store, opportunity_id=opportunity_id, version_id=version_id, user_id=user.id))


@router.get("/{opportunity_id}/pre-meeting-discovery")
def get_opportunity_pre_meeting_discovery(opportunity_id: UUID, user: AuthUserDep, store: DataStoreDep) -> dict:
    """BAP-11: the saved Discovery documents of the post-meeting document list (read-only).

    The Pre-Meeting Discovery Document is the approved version Presentation V1 was built from;
    the Post-Meeting Discovery Document is the approved Discovery 2. Both are downloaded through
    their existing version endpoints; nothing is generated here.
    """
    from app.services.pre_meeting_discovery import discovery_documents

    return discovery_documents(store, opportunity_id=opportunity_id, user_id=user.id)


# ------------------------------------------------------------------ BAP-7 Discovery 2


@router.get("/{opportunity_id}/post-meeting-discovery")
def get_opportunity_post_meeting_discovery(opportunity_id: UUID, user: AuthUserDep, store: DataStoreDep) -> dict:
    return get_post_meeting_discovery(store, opportunity_id=opportunity_id, user_id=user.id)


@router.post("/{opportunity_id}/post-meeting-discovery/generate")
def post_opportunity_post_meeting_discovery_generate(
    opportunity_id: UUID, user: AuthUserDep, store: DataStoreDep, body: GenerationLanguageRequest | None = None
) -> dict:
    result = generate_post_meeting_discovery(store, opportunity_id=opportunity_id, user_id=user.id, language=body.language if body else None)
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.DISCOVERY2_GENERATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
        version_id=UUID(result["generated_version_id"]),
    )
    return result


@router.patch("/{opportunity_id}/post-meeting-discovery")
def patch_opportunity_post_meeting_discovery(
    opportunity_id: UUID, body: Discovery2EditRequest, user: AuthUserDep, store: DataStoreDep
) -> dict:
    result = edit_post_meeting_discovery(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
        expected_version_id=body.expected_version_id,
        expected_content_hash=body.expected_content_hash,
        sections=[section.model_dump() for section in body.sections],
    )
    current = result.get("current_version") or {}
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.DISCOVERY2_EDIT,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
        version_id=UUID(current["id"]) if current.get("id") else None,
    )
    return result


@router.post("/{opportunity_id}/post-meeting-discovery/approve")
def post_opportunity_post_meeting_discovery_approve(
    opportunity_id: UUID, body: Discovery2ApproveRequest, user: AuthUserDep, store: DataStoreDep
) -> dict:
    result = approve_post_meeting_discovery(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
        expected_version_id=body.expected_version_id,
        expected_content_hash=body.expected_content_hash,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.DISCOVERY2_APPROVE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
        version_id=body.expected_version_id,
    )
    return result


@router.get("/{opportunity_id}/post-meeting-discovery/versions")
def get_opportunity_post_meeting_discovery_versions(opportunity_id: UUID, user: AuthUserDep, store: DataStoreDep) -> dict:
    return {"versions": list_post_meeting_discovery_versions(store, opportunity_id=opportunity_id, user_id=user.id)}


@router.get("/{opportunity_id}/post-meeting-discovery/versions/{version_id}")
def get_opportunity_post_meeting_discovery_version(
    opportunity_id: UUID, version_id: UUID, user: AuthUserDep, store: DataStoreDep
) -> dict:
    return get_post_meeting_discovery_version(store, opportunity_id=opportunity_id, version_id=version_id, user_id=user.id)


@router.get("/{opportunity_id}/post-meeting-discovery/versions/{version_id}/download/docx")
def get_opportunity_post_meeting_discovery_docx(
    opportunity_id: UUID, version_id: UUID, user: AuthUserDep, store: DataStoreDep
) -> Response:
    return _docx_response(discovery2_docx(store, opportunity_id=opportunity_id, version_id=version_id, user_id=user.id))


@router.get("/{opportunity_id}/stage2-outputs")
def get_stage2_outputs(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    return opportunity.get("stage2_outputs") or empty_stage2(opportunity_id)


@router.post("/{opportunity_id}/stage2-outputs/generate")
def post_stage2_outputs(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.STAGE2_OUTPUTS_GENERATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    return generate_stage2_outputs(store, opportunity_id=opportunity_id, user_id=user.id)


@router.get("/{opportunity_id}/meeting-feedback")
def read_meeting_feedback(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    return get_meeting_feedback(opportunity)


@router.put("/{opportunity_id}/meeting-feedback")
def write_meeting_feedback(
    opportunity_id: UUID,
    body: MeetingFeedbackUpdate,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    from datetime import UTC, datetime

    text = None if body.text is None else body.text.strip() or None
    store.update_opportunity(
        opportunity_id=opportunity_id,
        user_id=user.id,
        updates={
            "meeting_feedback_text": text,
            "meeting_feedback_updated_at": datetime.now(UTC),
        },
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.MEETING_FEEDBACK_UPDATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    return get_meeting_feedback(opportunity)


@router.get("/{opportunity_id}/personal-notes")
def read_personal_notes(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    return personal_notes_view(opportunity)


@router.put("/{opportunity_id}/personal-notes")
def write_personal_notes(
    opportunity_id: UUID,
    body: PersonalNotesUpdate,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    from datetime import UTC, datetime

    text = None if body.text is None else body.text.strip() or None
    store.update_opportunity(
        opportunity_id=opportunity_id,
        user_id=user.id,
        updates={
            "personal_notes": text,
            "personal_notes_updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.PERSONAL_NOTES_UPDATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    return personal_notes_view(opportunity)


@router.get("/{opportunity_id}/meeting-extraction")
def read_meeting_extraction(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    stored = get_meeting_extraction(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )
    return stored if stored is not None else empty_meeting_extraction(opportunity_id)


@router.post("/{opportunity_id}/meeting-extraction/generate")
def post_meeting_extraction(
    opportunity_id: UUID,
    body: MeetingExtractionGenerateRequest,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    stored = generate_meeting_extraction(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
        transcript_id=body.transcript_id,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.MEETING_EXTRACTION_GENERATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
        document_id=str(body.transcript_id),
    )
    return stored


@router.get("/{opportunity_id}/post-meeting-review")
def read_post_meeting_review(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return build_post_meeting_review(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


@router.post("/{opportunity_id}/post-meeting-review/confirm")
def post_meeting_review_confirm(
    opportunity_id: UUID,
    body: MeetingReviewConfirmRequest,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    review = confirm_meeting_review(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
        transcript_id=body.transcript_id,
        extraction_generated_at=body.extraction_generated_at,
        review_fingerprint=body.review_fingerprint,
        excluded=body.excluded,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.MEETING_REVIEW_CONFIRM,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
        document_id=str(body.transcript_id),
    )
    return review


@router.get("/{opportunity_id}/master-presentation/v2")
def read_master_presentation_v2(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return build_master_v2_status(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


@router.post("/{opportunity_id}/master-presentation/v2/generate")
def post_master_presentation_v2_generate(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    body: GenerationLanguageRequest | None = None,
) -> dict:
    """Master Presentation V2: a new version of the opportunity's Master Presentation.

    Readiness is decided here from the stored Post Meeting review, never by the caller.
    BAP-13: V2 is built in the language of the approved Discovery 2.
    """
    if body is not None and body.language is not None:
        source = approved_discovery2_language(store, opportunity_id=opportunity_id, user_id=user.id)
        if source is not None:
            require_source_language(body.language, source, artifact="Master Presentation V2", source="Post-Meeting Discovery Document")
    started = enqueue_master_presentation_v2(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.MASTER_PRESENTATION_V2_GENERATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
        document_id=started["presentation_id"],
    )
    return started


@router.get("/{opportunity_id}/available-use-cases")
def read_available_use_cases(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return list_available_use_cases(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


@router.get("/{opportunity_id}/selected-use-cases")
def read_selected_use_cases(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return get_selected_use_cases(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


@router.put("/{opportunity_id}/selected-use-cases")
def write_selected_use_cases(
    opportunity_id: UUID,
    body: SelectedUseCasesUpdate,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    stored = replace_selected_use_cases(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
        use_case_ids=body.use_case_ids,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.SELECTED_USE_CASES_UPDATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
        document_id=",".join(stored["use_case_ids"]) if stored["use_case_ids"] else "0",
    )
    return stored


@router.get("/{opportunity_id}/ppt2-context")
def read_ppt2_context(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return build_ppt2_context(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


@router.get("/{opportunity_id}/workflow-status")
def read_workflow_status(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return build_workflow_status(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


@router.post("/{opportunity_id}/workflow/first-meeting-completed")
def post_first_meeting_completed(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return mark_first_meeting_completed(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


@router.post("/{opportunity_id}/workflow/owner-reviewed")
def post_owner_reviewed(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return mark_owner_reviewed(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


def _require_ppt2_language(body: GenerationLanguageRequest | None) -> None:
    """BAP-12: the earlier standalone PPT #2 is English only; German is refused, never written in English."""
    if body is not None and body.language not in (None, "en"):
        raise conflict(
            "LANGUAGE_UNSUPPORTED",
            "The earlier standalone post-meeting presentation (PPT #2) is generated in English only. "
            "German post-meeting presentations are Master Presentation V2.",
        )


@router.post("/{opportunity_id}/ppt2/generate")
def post_ppt2_generate(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    body: GenerationLanguageRequest | None = None,
) -> dict:
    from app.services.presentation_generation import (
        enqueue_post_meeting_presentation_generate,
    )

    _require_ppt2_language(body)

    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.PPT2_PRESENTATION_GENERATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    return enqueue_post_meeting_presentation_generate(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


@router.post("/{opportunity_id}/ppt2/{presentation_id}/regenerate")
def post_ppt2_regenerate(
    opportunity_id: UUID,
    presentation_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    body: GenerationLanguageRequest | None = None,
) -> dict:
    from app.services.presentation_generation import (
        enqueue_post_meeting_presentation_generate,
    )

    _require_ppt2_language(body)

    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.PPT2_PRESENTATION_REGENERATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    return enqueue_post_meeting_presentation_generate(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
        presentation_id=presentation_id,
    )


@router.post("/{opportunity_id}/workflow/finalize")
def post_workflow_finalized(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    return mark_finalized(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


@router.get("/{opportunity_id}/client-preparation-email")
def get_client_preparation_email(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    stored = opportunity.get("client_preparation_email")
    return client_preparation_envelope(opportunity_id, stored)


@router.post("/{opportunity_id}/client-preparation-email/generate")
def post_client_preparation_email_generate(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.CLIENT_PREPARATION_EMAIL_GENERATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    return generate_client_preparation_email(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )


@router.get("/{opportunity_id}/email-drafts")
def get_email_drafts(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    journey_stage: Literal["first_contact", "deepening", "concretisation"],
) -> dict:
    stored = store.get_email_draft(
        opportunity_id=opportunity_id,
        user_id=user.id,
        journey_stage=journey_stage,
    )
    return followup_email.envelope(
        store, opportunity_id=opportunity_id, user_id=user.id, journey_stage=journey_stage, stored=stored
    )


@router.post("/{opportunity_id}/email-drafts/generate")
def post_email_drafts(
    opportunity_id: UUID,
    body: EmailGenerateRequest,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    from app.services.journey_stage import reject_owner_concretisation

    reject_owner_concretisation(body.journey_stage)
    generated = generate_email_draft(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
        journey_stage=body.journey_stage,
        overwrite_edits=body.overwrite_edits,
        language=body.language,
    )
    # Recorded once the draft exists: a refused request generated nothing.
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.EMAIL_DRAFT_GENERATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    return generated


@router.patch("/{opportunity_id}/email-drafts/{draft_id}")
def patch_email_draft(
    opportunity_id: UUID,
    draft_id: UUID,
    body: EmailUpdateRequest,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    """Save edited subject and message, the chosen length and the attachment selection."""
    saved = followup_email.update_draft(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
        draft_id=draft_id,
        expected_revision=body.expected_revision,
        selected_length=body.selected_length,
        lengths=body.lengths.model_dump(exclude_none=True) if body.lengths else None,
        attachments=dict(body.attachments) if body.attachments is not None else None,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.EMAIL_DRAFT_UPDATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    return saved


@router.get("/{opportunity_id}/email-drafts/{draft_id}/export")
def get_email_export(
    opportunity_id: UUID,
    draft_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    revision: int = Query(..., ge=1),
) -> Response:
    """The confirmed draft as an unsent .eml file with the selected approved files. Sends nothing."""
    exported = followup_email.export_eml(
        store, opportunity_id=opportunity_id, user_id=user.id, draft_id=draft_id, revision=revision
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.EMAIL_DRAFT_EXPORT,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    ascii_name = exported["file_name"].encode("ascii", "ignore").decode() or "follow-up-email.eml"
    return Response(
        content=exported["content"],
        media_type="message/rfc822",
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_name}"',
            "X-Email-Draft-Id": exported["draft_id"],
            "X-Email-Draft-Revision": str(exported["revision"]),
            "X-Email-Attachment-Count": str(len(exported["attachments"])),
            **({"X-Email-Presentation-Version-Id": exported["presentation_version_id"]} if exported["presentation_version_id"] else {}),
            "Cache-Control": "no-store",
        },
    )


# ------------------------------------------------------------------ BAP-3 uploaded email attachments


@router.post("/{opportunity_id}/email-drafts/{draft_id}/attachments")
async def post_email_attachment(
    opportunity_id: UUID,
    draft_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    file: UploadFile = File(...),
    expected_revision: int = Form(..., ge=1),
) -> dict:
    """Upload one additional attachment and select it. The draft's revision moves on."""
    from services.followup.attachments import MAX_EXTERNAL_ATTACHMENT_BYTES

    # At most one byte past the limit is read, so an oversized upload is never held whole.
    content = await file.read(MAX_EXTERNAL_ATTACHMENT_BYTES + 1)
    saved = followup_email.upload_attachment(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
        draft_id=draft_id,
        expected_revision=expected_revision,
        file_name=file.filename,
        mime_type=file.content_type,
        content=content,
    )
    record_audit_event(
        store, actor_id=user.id, action=AuditAction.EMAIL_ATTACHMENT_UPLOAD, object_type=AuditObjectType.OPPORTUNITY, object_id=opportunity_id
    )
    return saved


@router.get("/{opportunity_id}/email-drafts/{draft_id}/attachments")
def get_email_attachments(opportunity_id: UUID, draft_id: UUID, user: AuthUserDep, store: DataStoreDep) -> dict:
    return {"attachments": followup_email.list_attachments(store, opportunity_id=opportunity_id, user_id=user.id, draft_id=draft_id)}


@router.get("/{opportunity_id}/email-drafts/{draft_id}/attachments/{attachment_id}/download")
def get_email_attachment_download(
    opportunity_id: UUID, draft_id: UUID, attachment_id: UUID, user: AuthUserDep, store: DataStoreDep
) -> Response:
    exported = followup_email.download_attachment(
        store, opportunity_id=opportunity_id, user_id=user.id, draft_id=draft_id, attachment_id=attachment_id
    )
    ascii_name = exported["file_name"].encode("ascii", "ignore").decode().replace('"', "") or "attachment"
    return Response(
        content=exported["content"],
        media_type=exported["media_type"],
        headers={"Content-Disposition": f'attachment; filename="{ascii_name}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{opportunity_id}/email-drafts/{draft_id}/attachments/{attachment_id}")
def delete_email_attachment(
    opportunity_id: UUID,
    draft_id: UUID,
    attachment_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    expected_revision: int = Query(..., ge=1),
) -> dict:
    saved = followup_email.remove_attachment(
        store, opportunity_id=opportunity_id, user_id=user.id, draft_id=draft_id, attachment_id=attachment_id, expected_revision=expected_revision
    )
    record_audit_event(
        store, actor_id=user.id, action=AuditAction.EMAIL_ATTACHMENT_REMOVE, object_type=AuditObjectType.OPPORTUNITY, object_id=opportunity_id
    )
    return saved


@router.post("/{opportunity_id}/email-drafts/{draft_id}/confirm")
def post_email_confirm(
    opportunity_id: UUID,
    draft_id: UUID,
    body: EmailConfirmRequest,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    confirmed = confirm_email_draft(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
        draft_id=draft_id,
        selected_length=body.selected_length,
        expected_revision=body.expected_revision,
        review_checks=body.review_checks,
        acknowledged_flags=body.acknowledged_flags,
    )
    # Recorded once the confirmation is stored: a refused review confirmed nothing.
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.EMAIL_DRAFT_CONFIRM,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    return confirmed


@router.post("/{opportunity_id}/email-drafts/{draft_id}/send")
def post_email_send(
    opportunity_id: UUID,
    draft_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> dict:
    store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    store.get_email_draft_by_id(
        opportunity_id=opportunity_id,
        user_id=user.id,
        draft_id=draft_id,
    )
    raise bad_request(
        "EMAIL_SEND_FORBIDDEN",
        "Confirm stores the review state only. This API never sends mail.",
    )


__all__ = ["router", "JOURNEY_STAGES"]
