"""Opportunity routes (AT-40 / AT-56 / v2 section 22.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.auth import get_current_user
from app.config import settings
from app.dependencies import AuthUserDep, DataStoreDep
from app.schemas.jobs import ActiveJobResponse, JobResponse
from app.schemas.opportunities import (
    ClientLogoMetadata,
    FiledArtifactResponse,
    OpportunityCreateRequest,
    OpportunityResponse,
    OpportunityUpdateRequest,
    RecentWorkSnapshot,
)
from app.schemas.stage1 import AdditionalInformationUpdate
from app.services.pitch_owner import employee_pitch_owner, parse_team_members
from app.services.recent_work import list_recent_work_snapshots
from app.services import job_service
from app.services.api_errors import not_found
from app.services.audit import AuditAction, AuditObjectType, record_audit_event
from app.services.client_logos import MAX_CLIENT_LOGO_BYTES, validate_client_logo
from app.services.api_errors import service_unavailable
from app.services.first_contact_inputs import require_first_contact_client_documents
from app.services.knowledge_access import resolve_active_corpus
from app.services.stage1 import get_company_research_provider
from services.framework.stage1_research import (
    CompanyResearchProvider,
    generate_stage1_research,
)
from services.observability.llm_logger import llm_observability_scope

router = APIRouter(dependencies=[Depends(get_current_user)])


def _to_response(row: dict) -> OpportunityResponse:
    return OpportunityResponse.model_validate(row)


@router.post("", response_model=OpportunityResponse, status_code=201)
def create_opportunity(
    body: OpportunityCreateRequest,
    user: AuthUserDep,
    store: DataStoreDep,
) -> OpportunityResponse:
    row = store.create_opportunity(
        user_id=user.id,
        client_name=body.client_name,
        opportunity_name=body.opportunity_name,
        department=body.department,
        language=body.language,
        pitch_owner=employee_pitch_owner(user.id),
        team_members=parse_team_members(body.team_members_text),
        pitch_description=body.pitch_description,
        pii_redaction_enabled=body.pii_redaction_enabled,
        additional_client_information=(
            body.additional_client_information.model_dump()
            if body.additional_client_information is not None
            else None
        ),
        followup_statics=(
            body.followup_statics.model_dump()
            if body.followup_statics is not None
            else None
        ),
        stage1_intake=(
            body.stage1_intake.model_dump() if body.stage1_intake is not None else None
        ),
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.OPPORTUNITY_CREATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=row["id"],
    )
    return _to_response(row)


@router.get("", response_model=list[OpportunityResponse])
def list_opportunities(user: AuthUserDep, store: DataStoreDep) -> list[OpportunityResponse]:
    rows = store.list_opportunities(user_id=user.id)
    return [_to_response(row) for row in rows]


@router.post("/{opportunity_id}/stage1-research")
def generate_company_research(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    provider: CompanyResearchProvider | None = Depends(get_company_research_provider),
) -> dict:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    client_document_sources = require_first_contact_client_documents(
        store,
        opportunity_id=opportunity_id,
        user_id=user.id,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.STAGE1_RESEARCH_GENERATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    try:
        with llm_observability_scope(opportunity_id=opportunity_id, store=store):
            return generate_stage1_research(
                opportunity,
                corpus=resolve_active_corpus(store),
                provider=provider,
                use_llm=settings.AI_EXECUTION_MODE == "live",
                client_document_sources=client_document_sources,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "STAGE1_RESEARCH_FAILED",
                "message": "Company research could not be validated. Please retry.",
            },
        ) from exc


@router.post("/{opportunity_id}/stage1-voice")
async def stage1_voice_intake(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    file: UploadFile | None = File(default=None),
) -> dict:
    store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.STAGE1_VOICE_REQUEST,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    if file is None or not await file.read(1):
        return {"status": "not_provided", "transcript": None}
    raise service_unavailable(
        "STAGE1_VOICE_UNAVAILABLE",
        "No approved voice transcription provider is configured. "
        "Save text intake without a recording. Downstream voice use also requires BT-36 summarization.",
    )


@router.get("/recent-work", response_model=list[RecentWorkSnapshot])
def list_recent_work(user: AuthUserDep, store: DataStoreDep) -> list[RecentWorkSnapshot]:
    return [
        RecentWorkSnapshot.model_validate(row)
        for row in list_recent_work_snapshots(store, user_id=user.id)
    ]


@router.put("/{opportunity_id}/client-logo", response_model=ClientLogoMetadata)
async def upload_or_replace_client_logo(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    file: UploadFile = File(...),
) -> ClientLogoMetadata:
    file_name = (file.filename or "client-logo").replace("\\", "/").rsplit("/", 1)[-1]
    content = await file.read(MAX_CLIENT_LOGO_BYTES + 1)
    validated = validate_client_logo(file_name, file.content_type, content)
    storage_path = (
        f"{opportunity_id}/client-logo/{uuid4()}{Path(file_name).suffix.lower()}"
    )
    row, replaced = store.upsert_client_logo(
        opportunity_id=opportunity_id,
        user_id=user.id,
        file_name=file_name,
        mime_type=validated.mime_type,
        size_bytes=len(content),
        width_px=validated.width_px,
        height_px=validated.height_px,
        storage_path=storage_path,
        content=content,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=(
            AuditAction.CLIENT_LOGO_REPLACE
            if replaced
            else AuditAction.CLIENT_LOGO_UPLOAD
        ),
        object_type=AuditObjectType.CLIENT_LOGO,
        object_id=row["id"],
    )
    return ClientLogoMetadata.model_validate(row)


@router.get("/{opportunity_id}/client-logo/content")
def get_client_logo_content(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> Response:
    row = store.get_client_logo(opportunity_id=opportunity_id, user_id=user.id)
    content = store.get_client_logo_content(
        opportunity_id=opportunity_id,
        user_id=user.id,
    )
    safe_name = str(row["file_name"]).replace('"', "")
    return Response(
        content=content,
        media_type=str(row["mime_type"]),
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/{opportunity_id}/client-logo", response_model=ClientLogoMetadata)
def get_client_logo_metadata(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> ClientLogoMetadata:
    row = store.get_client_logo(opportunity_id=opportunity_id, user_id=user.id)
    return ClientLogoMetadata.model_validate(row)


@router.delete("/{opportunity_id}/client-logo", status_code=204)
def delete_client_logo(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> None:
    row = store.delete_client_logo(opportunity_id=opportunity_id, user_id=user.id)
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.CLIENT_LOGO_DELETE,
        object_type=AuditObjectType.CLIENT_LOGO,
        object_id=row["id"],
    )


@router.get("/{opportunity_id}/filed-artifacts", response_model=list[FiledArtifactResponse])
def list_filed_artifacts(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> list[FiledArtifactResponse]:
    rows = store.list_filed_artifacts(opportunity_id=opportunity_id, user_id=user.id)
    return [FiledArtifactResponse.model_validate(row) for row in rows]


@router.get("/{opportunity_id}/jobs/active", response_model=ActiveJobResponse)
def get_active_job(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    stage_group: Literal["framework", "presentation"] | None = Query(default=None),
) -> ActiveJobResponse:
    store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    row = store.get_active_job_for_opportunity(opportunity_id, stage_group)
    if row is None:
        raise not_found(
            "ACTIVE_JOB_NOT_FOUND",
            f"No job found for opportunity {opportunity_id}",
        )
    job = job_service.get_job(row["id"], repository=store)
    if job is None:
        raise not_found(
            "ACTIVE_JOB_NOT_FOUND",
            f"No job found for opportunity {opportunity_id}",
        )
    payload = job_service.job_to_response(job)
    return ActiveJobResponse(
        job_id=payload.job_id,
        job_type=payload.job_type,
        status=payload.status,
        current_stage=payload.current_stage,
        started_at=payload.started_at,
        error=payload.error,
    )


@router.get("/{opportunity_id}", response_model=OpportunityResponse)
def get_opportunity(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> OpportunityResponse:
    row = store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    return _to_response(row)


@router.get("/{opportunity_id}/jobs/latest", response_model=JobResponse | None)
def get_latest_opportunity_job(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> JobResponse | None:
    store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    job = job_service.get_latest_job_for_opportunity(opportunity_id, repository=store)
    return job_service.job_to_response(job) if job is not None else None


@router.patch("/{opportunity_id}", response_model=OpportunityResponse)
def update_opportunity(
    opportunity_id: UUID,
    body: OpportunityUpdateRequest,
    user: AuthUserDep,
    store: DataStoreDep,
) -> OpportunityResponse:
    row = store.update_opportunity(
        opportunity_id=opportunity_id,
        user_id=user.id,
        updates=body.model_dump(exclude_unset=True),
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.OPPORTUNITY_UPDATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    return _to_response(row)


@router.put("/{opportunity_id}/additional-information", response_model=OpportunityResponse)
def update_additional_information(
    opportunity_id: UUID,
    body: AdditionalInformationUpdate,
    user: AuthUserDep,
    store: DataStoreDep,
) -> OpportunityResponse:
    """BAP-10: save only the "Additional information" of Client Information.

    A ``stage1_intake`` PATCH replaces every intake field (and clears the voice recording); this
    updates the one column through the same store method, so ownership checks, persistence and
    auditing are the existing ones and all other client fields keep their values. Approved
    Discovery versions are stored separately and are not touched; the next generation reads it.
    """
    row = store.update_opportunity(
        opportunity_id=opportunity_id,
        user_id=user.id,
        updates={"about_company": body.additional_information},
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.OPPORTUNITY_UPDATE,
        object_type=AuditObjectType.OPPORTUNITY,
        object_id=opportunity_id,
    )
    return _to_response(row)
