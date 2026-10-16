"""Transcript routes (AT-40 / v2 section 22.1)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, UploadFile

from app.auth import get_current_user
from app.dependencies import AuthUserDep, DataStoreDep
from app.schemas.transcripts import TranscriptResponse, TranscriptUploadResponse
from app.services.audit import AuditAction, AuditObjectType, record_audit_event
from app.services.api_errors import bad_request
from app.services.data.supabase_store import validate_transcript_upload
from services.transcript.conversation_ids import next_conversation_id
from services.transcript.ingestion import (
    MAX_TRANSCRIPT_BYTES,
    TranscriptIngestionError,
    transcript_too_large_message,
)
from services.transcript.speaker_turns import split_speaker_turns

router = APIRouter(dependencies=[Depends(get_current_user)])


def _to_response(row: dict) -> TranscriptResponse:
    return TranscriptResponse.model_validate(row)


@router.post("/{opportunity_id}/transcripts", response_model=TranscriptUploadResponse, status_code=201)
async def upload_transcript(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
    file: UploadFile = File(...),
) -> TranscriptUploadResponse:
    file_name = file.filename or "upload.txt"
    validate_transcript_upload(file_name, file.content_type)
    # Read at most one byte past the limit, so an oversized upload is never held in memory whole.
    content = await file.read(MAX_TRANSCRIPT_BYTES + 1)
    if len(content) > MAX_TRANSCRIPT_BYTES:
        raise bad_request("TRANSCRIPT_TOO_LARGE", transcript_too_large_message())
    try:
        turns = split_speaker_turns(file_name, content)
    except TranscriptIngestionError as exc:
        raise bad_request("INVALID_TRANSCRIPT_CONTENT", exc.user_message) from exc

    store.get_opportunity(opportunity_id=opportunity_id, user_id=user.id)
    existing = store.list_transcripts(
        opportunity_id=opportunity_id,
        user_id=user.id,
        verify_owner=False,
    )
    conversation_id = next_conversation_id(
        [str(row.get("conversation_id") or "") for row in existing]
    )
    sections = [
        {
            "section_index": turn.turn_index,
            "speaker_role": turn.speaker,
            "content": turn.text,
            "metadata": {"conversation_id": conversation_id},
        }
        for turn in turns
    ]
    storage_path = f"{opportunity_id}/{uuid4()}{Path(file_name).suffix.lower()}"
    row = store.create_transcript(
        opportunity_id=opportunity_id,
        user_id=user.id,
        file_name=file_name,
        mime_type=file.content_type or "text/plain",
        storage_path=storage_path,
        conversation_id=conversation_id,
        content=content,
        sections=sections,
        verify_owner=False,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.TRANSCRIPT_UPLOAD,
        object_type=AuditObjectType.TRANSCRIPT,
        object_id=row["id"],
    )
    transcript = _to_response(row)
    return TranscriptUploadResponse(
        transcript=transcript,
        processing_status=transcript.processing_status,
    )


@router.get("/{opportunity_id}/transcripts", response_model=list[TranscriptResponse])
def list_transcripts(
    opportunity_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> list[TranscriptResponse]:
    rows = store.list_transcripts(opportunity_id=opportunity_id, user_id=user.id)
    return [_to_response(row) for row in rows]


@router.get("/{opportunity_id}/transcripts/{transcript_id}", response_model=TranscriptResponse)
def get_transcript(
    opportunity_id: UUID,
    transcript_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> TranscriptResponse:
    row = store.get_transcript(
        opportunity_id=opportunity_id,
        transcript_id=transcript_id,
        user_id=user.id,
    )
    return _to_response(row)


@router.post(
    "/{opportunity_id}/transcripts/{transcript_id}/regenerate",
    response_model=TranscriptResponse,
)
def regenerate_transcript(
    opportunity_id: UUID,
    transcript_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> TranscriptResponse:
    row = store.regenerate_transcript(
        opportunity_id=opportunity_id,
        transcript_id=transcript_id,
        user_id=user.id,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.TRANSCRIPT_REGENERATE,
        object_type=AuditObjectType.TRANSCRIPT,
        object_id=transcript_id,
    )
    return _to_response(row)


@router.delete("/{opportunity_id}/transcripts/{transcript_id}", status_code=204)
def delete_transcript(
    opportunity_id: UUID,
    transcript_id: UUID,
    user: AuthUserDep,
    store: DataStoreDep,
) -> None:
    store.delete_transcript(
        opportunity_id=opportunity_id,
        transcript_id=transcript_id,
        user_id=user.id,
    )
    record_audit_event(
        store,
        actor_id=user.id,
        action=AuditAction.TRANSCRIPT_DELETE,
        object_type=AuditObjectType.TRANSCRIPT,
        object_id=transcript_id,
    )
