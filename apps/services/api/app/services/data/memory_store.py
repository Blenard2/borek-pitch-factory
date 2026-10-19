"""In-memory data store for unit tests and local development (AT-40 / AT-41)."""

from __future__ import annotations

import copy
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from app.config import settings
from app.services.api_errors import bad_request, conflict, not_found
from app.services.deck_assets import materialize_fixture_deck_assets
from app.services.framework_status import require_reviewable_framework
from app.services.framework_stub_template import load_framework_stub_template
from services.framework.stage1_intake import apply_persisted_intake_to_framework
from app.services.stage_b_orchestration import (
    build_slide_spec_for_planned_slide,
    plan_json_from_confirmed_framework,
    planned_slides_with_generators,
)
from app.services.journey_outputs_store import (
    JOURNEY_OUTPUT_DB_COLUMNS,
    reject_finalization_snapshot_change,
)
from app.services.stage1_intake_store import (
    STAGE1_DB_COLUMNS,
    apply_intake_columns,
    expand_opportunity_updates,
    present_opportunity,
)

ALLOWED_TRANSCRIPT_EXTENSIONS = {".txt", ".vtt", ".srt", ".docx"}
ALLOWED_TRANSCRIPT_MIME_TYPES = {
    "text/plain",
    "text/vtt",
    "application/x-subrip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[6]
    / "packages"
    / "contracts"
    / "fixtures"
    / "framework_object.minimal.json"
)
_RICH_FIXTURE_PATH = (
    Path(__file__).resolve().parents[6]
    / "tests"
    / "fixtures"
    / "framework_object.confirmed.group_a.json"
)


def _now() -> datetime:
    return datetime.now(UTC)


_GENERATION_LOCK_GUARD = threading.Lock()
# One email draft write at a time: the revision check and the write are one step.
_EMAIL_DRAFT_GUARD = threading.Lock()
# Discovery 2 version numbers and conditional draft writes (BAP-7).
_DISCOVERY2_GUARD = threading.Lock()


def _version_lineage_sort(row: dict[str, Any]) -> tuple:
    return (str(row.get("created_at") or ""), int(row.get("version_number") or 0))


def _optional_uuid(value: Any) -> UUID | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def _llm_call_row(record: Any) -> dict[str, Any]:
    payload = (
        {key: getattr(record, key) for key in getattr(record, "__dataclass_fields__", {})}
        if getattr(record, "__dataclass_fields__", None)
        else dict(record)
    )
    created = payload.get("created_at") or payload.get("timestamp") or _now()
    return {
        "id": _optional_uuid(payload.get("id")) or uuid.uuid4(),
        "request_id": str(payload.get("request_id") or uuid.uuid4()),
        "job_id": _optional_uuid(payload.get("job_id")),
        "opportunity_id": _optional_uuid(payload.get("opportunity_id")),
        "stage": str(payload.get("stage") or ""),
        "provider": str(payload.get("provider") or "unknown"),
        "model": str(payload.get("model") or ""),
        "prompt_version": str(payload.get("prompt_version") or ""),
        "input_tokens": int(payload.get("input_tokens") or 0),
        "output_tokens": int(payload.get("output_tokens") or 0),
        "total_tokens": int(
            payload.get("total_tokens")
            or (int(payload.get("input_tokens") or 0) + int(payload.get("output_tokens") or 0))
        ),
        "latency_ms": int(round(float(payload.get("latency_ms") or 0))),
        "retry_count": int(payload.get("retry_count") or 0),
        "status": str(payload.get("status") or "success"),
        "error_category": payload.get("error_category"),
        "estimated_cost_eur": float(payload.get("estimated_cost_eur") or 0),
        "created_at": created,
    }


def _load_framework_template(opportunity_id: UUID) -> dict[str, Any]:
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    payload = copy.deepcopy(payload)
    if _RICH_FIXTURE_PATH.is_file():
        rich = json.loads(_RICH_FIXTURE_PATH.read_text(encoding="utf-8"))
        for index, chapter in enumerate(payload.get("chapters", [])):
            rich_chapter = rich.get("chapters", [])[index] if index < len(rich.get("chapters", [])) else None
            if rich_chapter and rich_chapter.get("source_refs"):
                chapter["body"] = copy.deepcopy(rich_chapter.get("body", chapter.get("body")))
                chapter["source_refs"] = copy.deepcopy(rich_chapter.get("source_refs", []))
        payload["quality_scores"] = copy.deepcopy(rich.get("quality_scores", payload["quality_scores"]))
        payload["kpis"] = copy.deepcopy(rich.get("kpis", payload["kpis"]))
        payload["rules"] = copy.deepcopy(rich.get("rules", payload["rules"]))
    payload["opportunity_id"] = str(opportunity_id)
    payload["updated_at"] = _now().isoformat().replace("+00:00", "Z")
    payload["created_at"] = payload["updated_at"]
    return payload


@dataclass
class MemoryDataStore:
    opportunities: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    transcripts: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    client_documents: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    client_logos: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    framework_versions: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    discovery_paper_versions: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    post_meeting_discovery_versions: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    presentation_plans: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    presentations: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    presentation_versions: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    slides: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    generation_jobs: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    audit_logs: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    egress_audits: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    llm_calls: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    filed_artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    knowledge_corpus_versions: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    knowledge_documents: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    knowledge_facts: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    knowledge_model_checkpoints: dict[tuple[UUID, UUID], dict[str, Any]] = field(default_factory=dict)
    user_roles: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    transcript_summaries: dict[UUID, dict[str, Any]] = field(default_factory=dict)

    def list_job_knowledge_models(
        self,
        *,
        job_id: UUID,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        job = self.generation_jobs.get(job_id)
        if job is None or job["opportunity_id"] != opportunity_id:
            raise bad_request("KNOWLEDGE_CHECKPOINT_INVALID", "Generation job does not belong to this opportunity")
        return [
            copy.deepcopy(row)
            for (checkpoint_job_id, _transcript_id), row in self.knowledge_model_checkpoints.items()
            if checkpoint_job_id == job_id and row["opportunity_id"] == opportunity_id
        ]

    def upsert_job_knowledge_model(
        self,
        *,
        job_id: UUID,
        transcript_id: UUID,
        opportunity_id: UUID,
        user_id: UUID,
        conversation_id: str,
        knowledge_model_json: dict[str, Any],
        schema_version: str,
        prompt_version: str,
    ) -> dict[str, Any]:
        job = self.generation_jobs.get(job_id)
        if job is None or job["opportunity_id"] != opportunity_id:
            raise bad_request("KNOWLEDGE_CHECKPOINT_INVALID", "Generation job does not belong to this opportunity")
        self.get_transcript(
            opportunity_id=opportunity_id,
            transcript_id=transcript_id,
            user_id=user_id,
        )
        row = {
            "generation_job_id": job_id,
            "transcript_id": transcript_id,
            "opportunity_id": opportunity_id,
            "conversation_id": conversation_id,
            "knowledge_model_json": copy.deepcopy(knowledge_model_json),
            "schema_version": schema_version,
            "prompt_version": prompt_version,
        }
        self.knowledge_model_checkpoints[(job_id, transcript_id)] = row
        return copy.deepcopy(row)

    def get_filing_record(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self.filed_artifacts.get(idempotency_key)
        return copy.deepcopy(row) if row is not None else None

    def save_filing_record(
        self,
        idempotency_key: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        row = copy.deepcopy(record)
        row["idempotency_key"] = idempotency_key
        row.setdefault("id", uuid.uuid4())
        self.filed_artifacts[idempotency_key] = row
        return copy.deepcopy(row)

    def list_filed_artifacts(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        target = str(opportunity_id)
        rows = [
            copy.deepcopy(row)
            for row in self.filed_artifacts.values()
            if str(row.get("opportunity_id")) == target and row.get("status") == "filed"
        ]
        return sorted(rows, key=lambda row: str(row.get("updated_at") or ""), reverse=True)

    def list_user_filed_artifacts(self, *, user_id: UUID) -> list[dict[str, Any]]:
        owned = {
            str(row["id"]): row
            for row in self.opportunities.values()
            if row["created_by"] == user_id
        }
        rows = []
        for artifact in self.filed_artifacts.values():
            if artifact.get("status") != "filed":
                continue
            opportunity = owned.get(str(artifact.get("opportunity_id")))
            if opportunity is not None:
                rows.append(
                    {
                        **copy.deepcopy(artifact),
                        "client_name": opportunity["client_name"],
                        "opportunity_name": opportunity["opportunity_name"],
                    }
                )
        return sorted(rows, key=lambda row: str(row.get("filed_at") or ""), reverse=True)

    def get_user_filed_artifact(
        self,
        *,
        artifact_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        row = next(
            (
                artifact
                for artifact in self.filed_artifacts.values()
                if str(artifact.get("id")) == str(artifact_id)
            ),
            None,
        )
        if row is None or row.get("status") != "filed":
            raise not_found("FILED_ARTIFACT_NOT_FOUND", "Filed artifact was not found")
        opportunity = self.get_opportunity(
            opportunity_id=UUID(str(row["opportunity_id"])),
            user_id=user_id,
        )
        return {
            **copy.deepcopy(row),
            "client_name": opportunity["client_name"],
            "opportunity_name": opportunity["opportunity_name"],
        }

    def create_generation_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = copy.deepcopy(payload)
        row.setdefault("id", uuid.uuid4())
        row.setdefault("created_at", _now())
        # Same rule as the database index generation_jobs_one_active_locked_generation.
        with _GENERATION_LOCK_GUARD:
            lock_key = row.get("generation_lock_key")
            if lock_key and any(
                other.get("generation_lock_key") == lock_key
                and other.get("opportunity_id") == row.get("opportunity_id")
                and str(getattr(other.get("status"), "value", other.get("status"))) in {"QUEUED", "RUNNING"}
                for other in self.generation_jobs.values()
            ):
                from app.services.job_service import GenerationLockConflict

                raise GenerationLockConflict(str(lock_key))
            self.generation_jobs[row["id"]] = row
        return copy.deepcopy(row)

    def get_generation_job(self, job_id: UUID) -> dict[str, Any] | None:
        row = self.generation_jobs.get(job_id)
        return copy.deepcopy(row) if row is not None else None

    def get_latest_generation_job_for_opportunity(
        self,
        opportunity_id: UUID,
    ) -> dict[str, Any] | None:
        rows = [
            row
            for row in self.generation_jobs.values()
            if row["opportunity_id"] == opportunity_id
        ]
        if not rows:
            return None
        return copy.deepcopy(
            max(rows, key=lambda row: (row["created_at"], str(row["id"])))
        )

    def list_generation_jobs_for_opportunity(
        self,
        opportunity_id: UUID,
    ) -> list[dict[str, Any]]:
        target = UUID(str(opportunity_id))
        rows = [
            copy.deepcopy(row)
            for row in self.generation_jobs.values()
            if row.get("opportunity_id") == target
        ]
        return sorted(rows, key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")))

    def update_generation_job(
        self,
        job_id: UUID,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        row = self.generation_jobs.get(job_id)
        if row is None:
            raise not_found("JOB_NOT_FOUND", f"No job found with id {job_id}")
        row.update(copy.deepcopy(updates))
        return copy.deepcopy(row)

    def get_active_job_for_opportunity(
        self,
        opportunity_id: str | UUID,
        stage_group: str | None = None,
        *,
        job_type: str | None = None,
    ) -> dict[str, Any] | None:
        from app.services.job_service import select_reconnect_job

        target = UUID(str(opportunity_id))
        rows = [
            copy.deepcopy(row)
            for row in self.generation_jobs.values()
            if row.get("opportunity_id") == target
            and (job_type is None or str(row.get("job_type") or "") == job_type)
        ]
        return select_reconnect_job(rows, stage_group=stage_group)

    def create_opportunity(
        self,
        *,
        user_id: UUID,
        client_name: str,
        opportunity_name: str,
        department: str,
        language: str,
        pitch_owner: dict[str, Any] | None = None,
        team_members: list[Any] | None = None,
        pitch_description: str | None = None,
        pii_redaction_enabled: bool = True,
        additional_client_information: dict[str, Any] | None = None,
        followup_statics: dict[str, Any] | None = None,
        stage1_intake: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from app.services.pitch_owner import employee_pitch_owner

        opportunity_id = uuid.uuid4()
        now = _now()
        row = {
            "id": opportunity_id,
            "client_name": client_name,
            "opportunity_name": opportunity_name,
            "department": department,
            "language": language,
            "pitch_owner": pitch_owner or employee_pitch_owner(user_id),
            "team_members": list(team_members or []),
            "pitch_description": pitch_description,
            "status": "active",
            "pii_redaction_enabled": bool(pii_redaction_enabled),
            "additional_client_information": copy.deepcopy(additional_client_information),
            "followup_statics": copy.deepcopy(followup_statics),
            "created_by": user_id,
            "created_at": now,
            "updated_at": now,
            **{key: None for key in STAGE1_DB_COLUMNS},
            **{key: None for key in JOURNEY_OUTPUT_DB_COLUMNS},
        }
        apply_intake_columns(row, stage1_intake)
        row["selected_use_case_ids"] = []
        self.opportunities[opportunity_id] = row
        return present_opportunity(row)

    def list_opportunities(self, *, user_id: UUID) -> list[dict[str, Any]]:
        rows = [row for row in self.opportunities.values() if row["created_by"] == user_id]
        return [
            present_opportunity(row)
            for row in sorted(rows, key=lambda row: row["created_at"], reverse=True)
        ]

    def load_recent_work_index(
        self,
        *,
        user_id: UUID,
        opportunity_ids: list[UUID],
    ) -> dict[str, Any]:
        owned = {row["id"] for row in self.list_opportunities(user_id=user_id)}
        wanted = {item for item in opportunity_ids if item in owned}
        transcripts: dict[str, list[dict[str, Any]]] = {str(item): [] for item in wanted}
        for row in self.transcripts.values():
            opp_id = row.get("opportunity_id")
            if opp_id in wanted:
                transcripts[str(opp_id)].append({"created_at": row.get("created_at")})

        frameworks: dict[str, dict[str, Any]] = {}
        for row in self.framework_versions.values():
            opp_id = row.get("opportunity_id")
            if opp_id not in wanted:
                continue
            current = frameworks.get(str(opp_id))
            if current is None or int(row.get("version_number") or 0) > int(current.get("version_number") or 0):
                frameworks[str(opp_id)] = {
                    "id": row["id"],
                    "status": row.get("status"),
                    "created_at": row.get("created_at"),
                    "version_number": row.get("version_number") or 0,
                }

        jobs: dict[str, list[dict[str, Any]]] = {str(item): [] for item in wanted}
        for row in self.generation_jobs.values():
            opp_id = row.get("opportunity_id")
            if opp_id in wanted:
                jobs[str(opp_id)].append(copy.deepcopy(row))

        framework_to_opp = {
            row["id"]: row["opportunity_id"]
            for row in self.framework_versions.values()
            if row.get("opportunity_id") in wanted
        }
        plans: dict[str, dict[str, Any]] = {}
        plan_to_opp: dict[UUID, UUID] = {}
        for row in self.presentation_plans.values():
            opp_id = framework_to_opp.get(row.get("framework_version_id"))
            if opp_id is None:
                continue
            plan_to_opp[row["id"]] = opp_id
            current = plans.get(str(opp_id))
            if current is None or str(row.get("created_at") or "") > str(current.get("created_at") or ""):
                plans[str(opp_id)] = {"id": row["id"], "created_at": row.get("created_at")}

        presentations: dict[str, dict[str, Any]] = {}
        for row in self.presentations.values():
            opp_id = plan_to_opp.get(row.get("presentation_plan_id"))
            if opp_id is None:
                continue
            current = presentations.get(str(opp_id))
            if current is None or str(row.get("created_at") or "") > str(current.get("created_at") or ""):
                latest_version = max(
                    (
                        version
                        for version in self.presentation_versions.values()
                        if version.get("presentation_id") == row["id"]
                    ),
                    key=lambda version: int(version.get("version_number") or 0),
                    default=None,
                )
                presentations[str(opp_id)] = {
                    "id": row["id"],
                    "name": row.get("name"),
                    "created_at": row.get("created_at"),
                    "version_status": None if latest_version is None else latest_version.get("status"),
                }

        return {
            "transcripts": transcripts,
            "frameworks": frameworks,
            "jobs": jobs,
            "plans": plans,
            "presentations": presentations,
        }

    def get_opportunity(self, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
        from app.services.pitch_owner import user_can_access_opportunity

        row = self.opportunities.get(opportunity_id)
        if row is None or not user_can_access_opportunity(row, user_id):
            raise not_found("OPPORTUNITY_NOT_FOUND", f"Opportunity {opportunity_id} was not found")
        return present_opportunity(row)

    def update_opportunity(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        from app.services.pitch_owner import user_can_access_opportunity

        row = self.opportunities.get(opportunity_id)
        if row is None or not user_can_access_opportunity(row, user_id):
            raise not_found("OPPORTUNITY_NOT_FOUND", f"Opportunity {opportunity_id} was not found")
        expanded = expand_opportunity_updates(dict(updates))
        reject_finalization_snapshot_change(row, expanded)
        for key, value in expanded.items():
            if value is not None or key in {
                "additional_client_information",
                "followup_statics",
                *STAGE1_DB_COLUMNS,
                *JOURNEY_OUTPUT_DB_COLUMNS,
            }:
                row[key] = value
        row["updated_at"] = _now()
        return present_opportunity(row)

    def upsert_client_logo(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        file_name: str,
        mime_type: str,
        size_bytes: int,
        storage_path: str,
        content: bytes,
        width_px: int | None = None,
        height_px: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        existing = next(
            (
                row
                for row in self.client_logos.values()
                if row["opportunity_id"] == opportunity_id
            ),
            None,
        )
        replaced = existing is not None
        if existing is not None:
            del self.client_logos[existing["id"]]
        row = {
            "id": existing["id"] if existing is not None else uuid.uuid4(),
            "opportunity_id": opportunity_id,
            "created_by": user_id,
            "file_name": file_name,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "width_px": width_px,
            "height_px": height_px,
            "storage_path": storage_path,
            "content": bytes(content),
            "uploaded_at": _now(),
        }
        self.client_logos[row["id"]] = row
        return copy.deepcopy(row), replaced

    def get_client_logo(self, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        row = next(
            (
                row
                for row in self.client_logos.values()
                if row["opportunity_id"] == opportunity_id
            ),
            None,
        )
        if row is None:
            raise not_found("CLIENT_LOGO_NOT_FOUND", "No client logo is stored for this opportunity")
        return copy.deepcopy(row)

    def get_client_logo_content(self, *, opportunity_id: UUID, user_id: UUID) -> bytes:
        row = self.get_client_logo(opportunity_id=opportunity_id, user_id=user_id)
        return bytes(row["content"])

    def mint_client_logo_provider_url(
        self,
        *,
        opportunity_id: UUID,
        ttl_seconds: int,
    ) -> str | None:
        return None

    def get_client_logo_for_signed_fetch(
        self, *, opportunity_id: UUID
    ) -> tuple[dict[str, Any], bytes]:
        """JJ-29: HMAC-gated lookup. Ownership is the signature, not a user JWT."""
        row = next(
            (
                item
                for item in self.client_logos.values()
                if item["opportunity_id"] == opportunity_id
            ),
            None,
        )
        if row is None:
            raise not_found("CLIENT_LOGO_NOT_FOUND", "No client logo is stored for this opportunity")
        return copy.deepcopy(row), bytes(row["content"])

    def delete_client_logo(self, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
        row = self.get_client_logo(opportunity_id=opportunity_id, user_id=user_id)
        del self.client_logos[row["id"]]
        return row

    def create_transcript(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        file_name: str,
        mime_type: str,
        storage_path: str,
        conversation_id: str,
        content: bytes,
        sections: list[dict[str, Any]],
        verify_owner: bool = True,
    ) -> dict[str, Any]:
        if verify_owner:
            self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        transcript_id = uuid.uuid4()
        row = {
            "id": transcript_id,
            "opportunity_id": opportunity_id,
            "file_name": file_name,
            "mime_type": mime_type,
            "storage_path": storage_path,
            "conversation_id": conversation_id,
            "content": bytes(content),
            "sections": copy.deepcopy(sections),
            "processing_status": "pending",
            "created_at": _now(),
        }
        self.transcripts[transcript_id] = row
        return row

    def list_transcripts(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        verify_owner: bool = True,
    ) -> list[dict[str, Any]]:
        if verify_owner:
            self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        rows = [
            row for row in self.transcripts.values() if row["opportunity_id"] == opportunity_id
        ]
        return sorted(rows, key=lambda row: row["created_at"])

    def list_transcript_sources(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        """Return persisted speaker turns for Stage A without reparsing uploads."""
        return [
            {
                "id": row["id"],
                "file_name": row["file_name"],
                "conversation_id": row["conversation_id"],
                "sections": copy.deepcopy(row["sections"]),
            }
            for row in self.list_transcripts(
                opportunity_id=opportunity_id,
                user_id=user_id,
            )
        ]

    def update_transcript_processing_status(
        self,
        *,
        opportunity_id: UUID,
        transcript_id: UUID,
        user_id: UUID,
        processing_status: str,
    ) -> None:
        row = self.get_transcript(
            opportunity_id=opportunity_id,
            transcript_id=transcript_id,
            user_id=user_id,
        )
        row["processing_status"] = processing_status

    def get_transcript(
        self,
        *,
        opportunity_id: UUID,
        transcript_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        row = self.transcripts.get(transcript_id)
        if row is None or row["opportunity_id"] != opportunity_id:
            raise not_found("TRANSCRIPT_NOT_FOUND", f"Transcript {transcript_id} was not found")
        return row

    def regenerate_transcript(
        self,
        *,
        opportunity_id: UUID,
        transcript_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        row = self.get_transcript(
            opportunity_id=opportunity_id,
            transcript_id=transcript_id,
            user_id=user_id,
        )
        row["processing_status"] = "pending"
        return row

    def delete_transcript(
        self,
        *,
        opportunity_id: UUID,
        transcript_id: UUID,
        user_id: UUID,
    ) -> None:
        self.get_transcript(
            opportunity_id=opportunity_id,
            transcript_id=transcript_id,
            user_id=user_id,
        )
        del self.transcripts[transcript_id]

    def create_client_document(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        file_name: str,
        mime_type: str,
        storage_path: str,
        document_key: str,
        content: bytes,
        sections: list[dict[str, Any]],
        processing_status: str = "processed",
        verify_owner: bool = True,
    ) -> dict[str, Any]:
        if verify_owner:
            self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        document_id = uuid.uuid4()
        row = {
            "id": document_id,
            "opportunity_id": opportunity_id,
            "file_name": file_name,
            "mime_type": mime_type,
            "storage_path": storage_path,
            "document_key": document_key,
            "content": bytes(content),
            "sections": copy.deepcopy(sections),
            "processing_status": processing_status,
            "created_at": _now(),
        }
        self.client_documents[document_id] = row
        return self._present_client_document(row)

    def list_client_documents(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        verify_owner: bool = True,
    ) -> list[dict[str, Any]]:
        if verify_owner:
            self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        rows = [
            row
            for row in self.client_documents.values()
            if row["opportunity_id"] == opportunity_id
        ]
        return [self._present_client_document(row) for row in sorted(rows, key=lambda item: item["created_at"])]

    def list_client_document_sources(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "file_name": row["file_name"],
                "document_key": row["document_key"],
                "processing_status": row["processing_status"],
                "sections": copy.deepcopy(row["sections"]),
            }
            for row in self.client_documents.values()
            if row["opportunity_id"] == opportunity_id
        ]

    def get_client_document(
        self,
        *,
        opportunity_id: UUID,
        document_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        row = self.client_documents.get(document_id)
        if row is None or row["opportunity_id"] != opportunity_id:
            raise not_found("CLIENT_DOCUMENT_NOT_FOUND", f"Client document {document_id} was not found")
        return self._present_client_document(row)

    def delete_client_document(
        self,
        *,
        opportunity_id: UUID,
        document_id: UUID,
        user_id: UUID,
    ) -> None:
        self.get_client_document(
            opportunity_id=opportunity_id,
            document_id=document_id,
            user_id=user_id,
        )
        del self.client_documents[document_id]

    @staticmethod
    def _present_client_document(row: dict[str, Any]) -> dict[str, Any]:
        sections = row.get("sections") or []
        return {
            "id": row["id"],
            "opportunity_id": row["opportunity_id"],
            "file_name": row["file_name"],
            "mime_type": row["mime_type"],
            "document_key": row["document_key"],
            "processing_status": row["processing_status"],
            "section_count": len(sections),
            "created_at": row["created_at"],
        }

    def create_discovery_paper_version(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        document_id: UUID,
        paper_json: dict[str, Any],
        version_id: UUID | None = None,
    ) -> dict[str, Any]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        existing = [
            row
            for row in self.discovery_paper_versions.values()
            if row["opportunity_id"] == opportunity_id
        ]
        version_number = max((row["version_number"] for row in existing), default=0) + 1
        now = _now()
        row = {
            "id": version_id or uuid.uuid4(),
            "opportunity_id": opportunity_id,
            "version_number": version_number,
            "document_id": document_id,
            "status": "draft",
            "paper_json": copy.deepcopy(paper_json),
            "created_by": user_id,
            "created_at": now,
            "updated_at": now,
            "approved_at": None,
        }
        self.discovery_paper_versions[row["id"]] = row
        return copy.deepcopy(row)

    def get_discovery_paper_version(
        self,
        *,
        version_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        row = self.discovery_paper_versions.get(version_id)
        if row is None:
            raise not_found(
                "DISCOVERY_PAPER_VERSION_NOT_FOUND",
                f"Discovery Paper version {version_id} was not found",
            )
        self.get_opportunity(opportunity_id=row["opportunity_id"], user_id=user_id)
        return copy.deepcopy(row)

    def list_discovery_paper_versions(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        rows = [
            row
            for row in self.discovery_paper_versions.values()
            if row["opportunity_id"] == opportunity_id
        ]
        return [copy.deepcopy(row) for row in sorted(rows, key=lambda item: item["version_number"])]

    def get_latest_approved_discovery_paper(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any] | None:
        rows = [
            row
            for row in self.list_discovery_paper_versions(
                opportunity_id=opportunity_id,
                user_id=user_id,
            )
            if row["status"] == "approved"
        ]
        if not rows:
            return None
        return max(rows, key=lambda row: (row["approved_at"] or row["created_at"], row["version_number"]))

    def get_draft_discovery_paper_version(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        document_id: UUID,
    ) -> dict[str, Any] | None:
        rows = [
            row
            for row in self.list_discovery_paper_versions(
                opportunity_id=opportunity_id,
                user_id=user_id,
            )
            if row["status"] == "draft" and row["document_id"] == document_id
        ]
        if not rows:
            return None
        return max(rows, key=lambda row: row["version_number"])

    def update_discovery_paper_draft(
        self,
        *,
        version_id: UUID,
        user_id: UUID,
        paper_json: dict[str, Any],
    ) -> dict[str, Any]:
        return self._write_discovery_paper_version(
            version_id=version_id,
            user_id=user_id,
            paper_json=paper_json,
            status="draft",
        )

    def approve_discovery_paper_version(
        self,
        *,
        version_id: UUID,
        user_id: UUID,
        paper_json: dict[str, Any],
    ) -> dict[str, Any]:
        return self._write_discovery_paper_version(
            version_id=version_id,
            user_id=user_id,
            paper_json=paper_json,
            status="approved",
        )

    def _write_discovery_paper_version(
        self,
        *,
        version_id: UUID,
        user_id: UUID,
        paper_json: dict[str, Any],
        status: str,
    ) -> dict[str, Any]:
        row = self.discovery_paper_versions.get(version_id)
        if row is None:
            raise not_found(
                "DISCOVERY_PAPER_VERSION_NOT_FOUND",
                f"Discovery Paper version {version_id} was not found",
            )
        self.get_opportunity(opportunity_id=row["opportunity_id"], user_id=user_id)
        if row["status"] == "approved":
            raise conflict(
                "DISCOVERY_PAPER_VERSION_IMMUTABLE",
                "Approved Discovery Paper versions are immutable",
            )
        if status not in {"draft", "approved"} or row["status"] != "draft":
            raise conflict(
                "DISCOVERY_PAPER_VERSION_IMMUTABLE",
                "Approved Discovery Paper versions cannot return to draft",
            )
        now = _now()
        row["paper_json"] = copy.deepcopy(paper_json)
        row["status"] = status
        row["updated_at"] = now
        if status == "approved":
            row["approved_at"] = now
        return copy.deepcopy(row)

    # ------------------------------------------------------------ BAP-7 Discovery 2 versions

    def create_discovery2_version(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        document_id: UUID,
        document_json: dict[str, Any],
        content_hash: str,
        basis_hash: str,
        discovery1_version_id: UUID,
    ) -> dict[str, Any]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        with _DISCOVERY2_GUARD:
            existing = [row for row in self.post_meeting_discovery_versions.values() if row["opportunity_id"] == opportunity_id]
            version_number = max((row["version_number"] for row in existing), default=0) + 1
            now = _now()
            row = {
                "id": uuid.uuid4(),
                "opportunity_id": opportunity_id,
                "version_number": version_number,
                "document_id": document_id,
                "status": "draft",
                "document_json": copy.deepcopy(document_json),
                "content_hash": content_hash,
                "basis_hash": basis_hash,
                "discovery1_version_id": discovery1_version_id,
                "created_by": user_id,
                "created_at": now,
                "updated_at": now,
                "approved_at": None,
                "approved_by": None,
            }
            self.post_meeting_discovery_versions[row["id"]] = row
            return copy.deepcopy(row)

    def get_discovery2_version(self, *, version_id: UUID, user_id: UUID) -> dict[str, Any]:
        row = self.post_meeting_discovery_versions.get(version_id)
        if row is None:
            raise not_found("DISCOVERY2_VERSION_NOT_FOUND", f"Discovery 2 version {version_id} was not found")
        self.get_opportunity(opportunity_id=row["opportunity_id"], user_id=user_id)
        return copy.deepcopy(row)

    def list_discovery2_versions(self, *, opportunity_id: UUID, user_id: UUID) -> list[dict[str, Any]]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        rows = [row for row in self.post_meeting_discovery_versions.values() if row["opportunity_id"] == opportunity_id]
        return [copy.deepcopy(row) for row in sorted(rows, key=lambda item: item["version_number"])]

    def update_discovery2_draft(
        self,
        *,
        version_id: UUID,
        user_id: UUID,
        document_json: dict[str, Any],
        content_hash: str,
        expected_content_hash: str,
    ) -> dict[str, Any]:
        return self._write_discovery2_version(
            version_id=version_id,
            user_id=user_id,
            expected_content_hash=expected_content_hash,
            updates={"document_json": copy.deepcopy(document_json), "content_hash": content_hash},
        )

    def approve_discovery2_version(
        self,
        *,
        version_id: UUID,
        user_id: UUID,
        expected_content_hash: str,
    ) -> dict[str, Any]:
        now = _now()
        return self._write_discovery2_version(
            version_id=version_id,
            user_id=user_id,
            expected_content_hash=expected_content_hash,
            updates={"status": "approved", "approved_at": now, "approved_by": user_id},
        )

    def _write_discovery2_version(
        self,
        *,
        version_id: UUID,
        user_id: UUID,
        expected_content_hash: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        with _DISCOVERY2_GUARD:
            row = self.post_meeting_discovery_versions.get(version_id)
            if row is None:
                raise not_found("DISCOVERY2_VERSION_NOT_FOUND", f"Discovery 2 version {version_id} was not found")
            self.get_opportunity(opportunity_id=row["opportunity_id"], user_id=user_id)
            if row["status"] != "draft":
                raise conflict("DISCOVERY2_VERSION_IMMUTABLE", "Approved Discovery 2 versions are immutable")
            if row["content_hash"] != expected_content_hash:
                raise conflict("DISCOVERY2_STALE", "Discovery 2 changed after it was loaded. Reload it and try again.")
            row.update(updates)
            row["updated_at"] = _now()
            return copy.deepcopy(row)

    def create_framework_version(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        framework_json: dict[str, Any],
        status: str = "draft",
        framework_version_id: UUID | None = None,
    ) -> dict[str, Any]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        existing = [
            row
            for row in self.framework_versions.values()
            if row["opportunity_id"] == opportunity_id
        ]
        version_number = len(existing) + 1
        framework_version_id = framework_version_id or uuid.uuid4()
        persisted_json = copy.deepcopy(framework_json)
        persisted_json["opportunity_id"] = str(opportunity_id)
        persisted_json["version"] = version_number
        persisted_json["previous_version_id"] = (
            str(max(existing, key=lambda item: item["version_number"])["id"])
            if existing
            else None
        )
        persisted_json["status"] = status
        from packages.contracts.schema_consumer import validate_framework_object

        validate_framework_object(persisted_json)
        row = {
            "id": framework_version_id,
            "opportunity_id": opportunity_id,
            "version_number": version_number,
            "status": status,
            "framework_json": persisted_json,
            "created_by": user_id,
            "created_at": _now(),
        }
        self.framework_versions[framework_version_id] = row
        return row

    def get_latest_framework(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        rows = [
            row
            for row in self.framework_versions.values()
            if row["opportunity_id"] == opportunity_id
        ]
        if not rows:
            raise not_found(
                "FRAMEWORK_NOT_FOUND",
                f"No framework version exists for opportunity {opportunity_id}",
            )
        return max(rows, key=lambda row: row["version_number"])

    def get_framework_version(
        self,
        *,
        framework_version_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        row = self.framework_versions.get(framework_version_id)
        if row is None:
            raise not_found(
                "FRAMEWORK_NOT_FOUND",
                f"Framework version {framework_version_id} was not found",
            )
        self.get_opportunity(opportunity_id=row["opportunity_id"], user_id=user_id)
        return row

    def confirm_framework(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        framework_version_id: UUID | None,
        confirmed_framework_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if confirmed_framework_json is not None:
            if framework_version_id is not None:
                row = self.get_framework_version(
                    framework_version_id=framework_version_id,
                    user_id=user_id,
                )
                if row["opportunity_id"] != opportunity_id:
                    raise not_found(
                        "FRAMEWORK_NOT_FOUND",
                        f"Framework version {framework_version_id} was not found",
                    )
            else:
                row = self.get_latest_framework(opportunity_id=opportunity_id, user_id=user_id)

            require_reviewable_framework(row["status"], action="confirm")
        elif framework_version_id is not None:
            row = self.get_framework_version(
                framework_version_id=framework_version_id,
                user_id=user_id,
            )
            if row["opportunity_id"] != opportunity_id:
                raise not_found(
                    "FRAMEWORK_NOT_FOUND",
                    f"Framework version {framework_version_id} was not found",
                )
        else:
            row = self.get_latest_framework(opportunity_id=opportunity_id, user_id=user_id)

        require_reviewable_framework(row["status"], action="confirm")

        now = _now()
        row["status"] = "confirmed"
        framework_json = confirmed_framework_json if confirmed_framework_json is not None else row["framework_json"]
        framework_json["status"] = "confirmed"
        framework_json["confirmed_by"] = str(user_id)
        framework_json["confirmed_at"] = now.isoformat().replace("+00:00", "Z")
        row["framework_json"] = framework_json
        row["confirmed_by"] = user_id
        row["confirmed_at"] = now
        return row

    def reopen_framework_for_correction(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        row = self.get_latest_framework(opportunity_id=opportunity_id, user_id=user_id)
        framework_json = copy.deepcopy(row["framework_json"])
        framework_json["status"] = "in_review"
        framework_json.pop("confirmed_by", None)
        framework_json.pop("confirmed_at", None)
        change_log = framework_json.setdefault("change_log", [])
        change_log.append("Reopened for a small correction after presentation generation")
        row["status"] = "in_review"
        row["framework_json"] = framework_json
        row["confirmed_by"] = None
        row["confirmed_at"] = None
        return row

    def update_latest_framework(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        framework_json: dict[str, Any],
    ) -> dict[str, Any]:
        row = self.get_latest_framework(opportunity_id=opportunity_id, user_id=user_id)
        require_reviewable_framework(row["status"], action="edit")

        updated = copy.deepcopy(framework_json)
        updated["opportunity_id"] = str(opportunity_id)
        updated["updated_at"] = _now().isoformat().replace("+00:00", "Z")
        change_log = updated.setdefault("change_log", [])
        change_log.append("Manual edit via framework review UI")
        row["framework_json"] = updated
        return row

    def regenerate_chapter(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        chapter_id: str,
    ) -> dict[str, Any]:
        row = self.get_latest_framework(opportunity_id=opportunity_id, user_id=user_id)
        require_reviewable_framework(row["status"], action="regenerate")

        chapters = row["framework_json"].get("chapters", [])
        if not any(str(chapter.get("chapter_id")) == chapter_id for chapter in chapters):
            raise bad_request("INVALID_CHAPTER_ID", f"Chapter {chapter_id} was not found")

        change_log = row["framework_json"].setdefault("change_log", [])
        change_log.append(f"Regenerated chapter {chapter_id}")
        return row

    def append_framework_version_transition(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        source_framework_version_id: UUID,
        framework_version_id: UUID,
        framework_json: dict[str, Any],
        status: str,
        source_revision: str,
        transition: str,
    ) -> dict[str, Any]:
        existing = self.framework_versions.get(framework_version_id)
        if existing is not None:
            if existing["framework_json"] == framework_json:
                return existing
            raise conflict("FRAMEWORK_VERSION_CONFLICT", "The reserved Framework version already exists")
        source = self.get_framework_version(
            framework_version_id=source_framework_version_id,
            user_id=user_id,
        )
        latest = self.get_latest_framework(opportunity_id=opportunity_id, user_id=user_id)
        if latest["id"] != source["id"]:
            raise conflict("FRAMEWORK_VERSION_CONFLICT", "The Framework changed before regeneration completed")
        from app.services.framework_versioning import framework_source_revision

        if framework_source_revision(source) != source_revision:
            raise conflict("FRAMEWORK_VERSION_CONFLICT", "The source Framework changed during regeneration")
        allowed = {
            "regenerate": ({"draft", "in_review"}, {"draft", "in_review"}),
            "edit": ({"draft", "in_review"}, {"draft", "in_review"}),
            "confirm": ({"draft", "in_review"}, {"confirmed"}),
            "reopen": ({"confirmed"}, {"in_review"}),
        }
        source_statuses, target_statuses = allowed.get(transition, (set(), set()))
        if source["status"] not in source_statuses or status not in target_statuses:
            raise conflict("FRAMEWORK_VERSION_CONFLICT", "Invalid Framework version transition")
        next_version = int(source["version_number"]) + 1
        if (
            int(framework_json.get("version") or 0) != next_version
            or str(framework_json.get("previous_version_id") or "") != str(source["id"])
            or str(framework_json.get("status") or "") != status
        ):
            raise conflict("FRAMEWORK_VERSION_CONFLICT", "Framework successor metadata is inconsistent")
        row = {
            "id": framework_version_id,
            "opportunity_id": opportunity_id,
            "version_number": next_version,
            "status": status,
            "framework_json": copy.deepcopy(framework_json),
            "created_by": user_id,
            "created_at": _now(),
        }
        self.framework_versions[framework_version_id] = row
        return row

    def generate_framework_stub(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        framework_json = load_framework_stub_template(opportunity_id)
        apply_persisted_intake_to_framework(
            framework_json,
            self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id),
        )
        return self.create_framework_version(
            opportunity_id=opportunity_id,
            user_id=user_id,
            framework_json=framework_json,
            status="draft",
        )

    def create_presentation_plan(
        self,
        *,
        framework_version_id: UUID,
        user_id: UUID,
        plan_json: dict[str, Any],
        presentation_plan_id: UUID | None = None,
    ) -> dict[str, Any]:
        framework = self.get_framework_version(
            framework_version_id=framework_version_id,
            user_id=user_id,
        )
        if framework["status"] != "confirmed":
            raise bad_request(
                "FRAMEWORK_NOT_CONFIRMED",
                "Framework must be confirmed before creating a presentation plan",
            )
        presentation_plan_id = presentation_plan_id or uuid.uuid4()
        row = {
            "id": presentation_plan_id,
            "framework_version_id": framework_version_id,
            "plan_json": plan_json,
            "created_at": _now(),
        }
        self.presentation_plans[presentation_plan_id] = row
        return row

    def get_presentation_plan(
        self,
        *,
        presentation_plan_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        row = self.presentation_plans.get(presentation_plan_id)
        if row is None:
            raise not_found(
                "PRESENTATION_PLAN_NOT_FOUND",
                f"Presentation plan {presentation_plan_id} was not found",
            )
        self.get_framework_version(
            framework_version_id=row["framework_version_id"],
            user_id=user_id,
        )
        return row

    def get_latest_presentation_plan(
        self,
        *,
        framework_version_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any] | None:
        self.get_framework_version(
            framework_version_id=framework_version_id,
            user_id=user_id,
        )
        rows = [
            row
            for row in self.presentation_plans.values()
            if row["framework_version_id"] == framework_version_id
        ]
        if not rows:
            return None
        return max(rows, key=lambda row: row["created_at"])

    def get_latest_presentation_plan_for_opportunity(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        framework = self.get_latest_framework(opportunity_id=opportunity_id, user_id=user_id)
        plan = self.get_latest_presentation_plan(
            framework_version_id=framework["id"],
            user_id=user_id,
        )
        if plan is None:
            raise not_found(
                "PRESENTATION_PLAN_NOT_FOUND",
                f"No presentation plan exists for opportunity {opportunity_id}",
            )
        return plan

    def generate_presentation_plan(
        self,
        *,
        framework_version_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        framework = self.get_framework_version(
            framework_version_id=framework_version_id,
            user_id=user_id,
        )
        return self.create_presentation_plan(
            framework_version_id=framework_version_id,
            user_id=user_id,
            plan_json=plan_json_from_confirmed_framework(framework["framework_json"]),
        )

    def create_presentation(
        self,
        *,
        presentation_plan_id: UUID,
        user_id: UUID,
        name: str,
    ) -> dict[str, Any]:
        plan = self.get_presentation_plan(
            presentation_plan_id=presentation_plan_id,
            user_id=user_id,
        )
        presentation_id = uuid.uuid4()
        row = {
            "id": presentation_id,
            "presentation_plan_id": presentation_plan_id,
            "name": name,
            "status": "draft",
            "created_at": _now(),
        }
        self.presentations[presentation_id] = row
        _ = plan
        return row

    def retarget_presentation_plan(
        self,
        *,
        presentation_id: UUID,
        presentation_plan_id: UUID,
        user_id: UUID,
        name: str | None = None,
    ) -> dict[str, Any]:
        row = self.get_presentation(presentation_id=presentation_id, user_id=user_id)
        self.get_presentation_plan(
            presentation_plan_id=presentation_plan_id,
            user_id=user_id,
        )
        row["presentation_plan_id"] = presentation_plan_id
        if name:
            row["name"] = name
        return row

    def list_presentations(self, *, user_id: UUID) -> list[dict[str, Any]]:
        accessible_plan_ids = {
            plan_id
            for plan_id, plan in self.presentation_plans.items()
            if self._user_can_access_plan(plan_id=plan_id, user_id=user_id)
        }
        rows = [
            row
            for row in self.presentations.values()
            if row["presentation_plan_id"] in accessible_plan_ids
        ]
        return sorted(rows, key=lambda row: row["created_at"], reverse=True)

    def get_presentation(
        self,
        *,
        presentation_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        row = self.presentations.get(presentation_id)
        if row is None:
            raise not_found(
                "PRESENTATION_NOT_FOUND",
                f"Presentation {presentation_id} was not found",
            )
        self.get_presentation_plan(
            presentation_plan_id=row["presentation_plan_id"],
            user_id=user_id,
        )
        return row

    def get_presentation_opportunity_id(
        self,
        *,
        presentation_id: UUID,
        user_id: UUID,
    ) -> UUID:
        presentation = self.get_presentation(presentation_id=presentation_id, user_id=user_id)
        plan = self.get_presentation_plan(
            presentation_plan_id=presentation["presentation_plan_id"],
            user_id=user_id,
        )
        framework = self.get_framework_version(
            framework_version_id=plan["framework_version_id"],
            user_id=user_id,
        )
        return framework["opportunity_id"]

    def create_presentation_version_with_slides(
        self,
        *,
        presentation_id: UUID,
        user_id: UUID,
        plan_json: dict[str, Any],
        journey_stage: str | None = None,
        prior_stage_presentation_version_id: UUID | None = None,
        discovery_pages: list[dict[str, Any]] | None = None,
        generation_source_manifest: dict[str, Any] | None = None,
        ppt2_generation_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        presentation = self.get_presentation(presentation_id=presentation_id, user_id=user_id)
        plan = self.get_presentation_plan(
            presentation_plan_id=presentation["presentation_plan_id"],
            user_id=user_id,
        )
        existing = [
            row
            for row in self.presentation_versions.values()
            if row["presentation_id"] == presentation_id
        ]
        version_number = len(existing) + 1
        presentation_version_id = uuid.uuid4()
        version_row = {
            "id": presentation_version_id,
            "presentation_id": presentation_id,
            "version_number": version_number,
            "slides_json": [],
            "pptx_storage_path": None,
            "pdf_storage_path": None,
            "status": "generating",
            "journey_stage": journey_stage,
            "prior_stage_presentation_version_id": prior_stage_presentation_version_id,
            "generation_source_manifest": (
                copy.deepcopy(generation_source_manifest)
                if generation_source_manifest is not None
                else None
            ),
            "created_at": _now(),
        }
        self.presentation_versions[presentation_version_id] = version_row

        if ppt2_generation_input is not None:
            from services.presentation.post_meeting_slide_content import (
                build_post_meeting_slide_specs,
            )

            generated_specs = build_post_meeting_slide_specs(
                plan_json,
                ppt2_generation_input,
            )
        elif discovery_pages is not None:
            from services.presentation.discovery_slide_content import (
                build_discovery_slide_specs,
            )

            generated_specs = build_discovery_slide_specs(plan_json, discovery_pages)
        else:
            framework = self.get_framework_version(
                framework_version_id=plan["framework_version_id"],
                user_id=user_id,
            )
            generated_specs = [
                build_slide_spec_for_planned_slide(
                    planned=planned,
                    framework_json=framework["framework_json"],
                )
                for planned in planned_slides_with_generators(plan_json)
            ]
        slide_specs: list[dict[str, Any]] = []
        for index, slide_spec in enumerate(generated_specs, start=1):
            persisted_slide_spec = copy.deepcopy(slide_spec)
            persisted_slide_spec["slideId"] = f"slide_{index:02d}"
            slide_id = uuid.uuid4()
            slide_row = {
                "id": slide_id,
                "presentation_version_id": presentation_version_id,
                "slide_index": index - 1,
                "layout_id": persisted_slide_spec["layoutId"],
                "slide_spec": persisted_slide_spec,
                "source_chapter_ids": copy.deepcopy(
                    persisted_slide_spec["sourceChapterIds"]
                ),
                "created_at": _now(),
            }
            self.slides[slide_id] = slide_row
            slide_specs.append(copy.deepcopy(persisted_slide_spec))

        version_row["slides_json"] = slide_specs
        # A Master Presentation is never stood in for by the fixture deck: it becomes ready only
        # once its real PPTX, PDF and previews exist.
        if settings.RENDERER_EXECUTION_MODE == "fixture" and plan_json.get("deck_kind") not in ("master_v1", "master_v2"):
            assets = materialize_fixture_deck_assets(
                version_id=presentation_version_id,
                slide_count=len(slide_specs),
            )
            self.update_presentation_version_assets(
                presentation_version_id=presentation_version_id,
                assets=assets,
                status="ready",
            )
        return version_row

    def update_presentation_version_assets(
        self,
        *,
        presentation_version_id: UUID,
        assets: dict[str, object],
        status: str,
    ) -> dict[str, Any]:
        row = self.presentation_versions.get(presentation_version_id)
        if row is None:
            raise not_found(
                "PRESENTATION_VERSION_NOT_FOUND",
                f"Presentation version {presentation_version_id} was not found",
            )
        row["pptx_storage_path"] = assets.get("pptx_storage_path")
        row["pdf_storage_path"] = assets.get("pdf_storage_path")
        row["preview_image_paths"] = list(assets.get("preview_image_paths") or [])
        row["status"] = status
        return copy.deepcopy(row)

    def get_latest_presentation_version(
        self,
        *,
        presentation_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        self.get_presentation(presentation_id=presentation_id, user_id=user_id)
        rows = [
            row
            for row in self.presentation_versions.values()
            if row["presentation_id"] == presentation_id
        ]
        if not rows:
            raise not_found(
                "PRESENTATION_VERSION_NOT_FOUND",
                f"No presentation version exists for presentation {presentation_id}",
            )
        return max(rows, key=lambda row: row["version_number"])

    def get_slide(
        self,
        *,
        presentation_id: UUID,
        slide_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        version = self.get_latest_presentation_version(
            presentation_id=presentation_id,
            user_id=user_id,
        )
        row = self.slides.get(slide_id)
        if row is None or row["presentation_version_id"] != version["id"]:
            raise not_found("SLIDE_NOT_FOUND", f"Slide {slide_id} was not found")
        return row

    def regenerate_slide(
        self,
        *,
        presentation_id: UUID,
        slide_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        return self._create_edited_presentation_version(
            presentation_id=presentation_id,
            slide_id=slide_id,
            user_id=user_id,
            layout_id=None,
        )

    def change_slide_layout(
        self,
        *,
        presentation_id: UUID,
        slide_id: UUID,
        user_id: UUID,
        layout_id: str,
    ) -> dict[str, Any]:
        return self._create_edited_presentation_version(
            presentation_id=presentation_id,
            slide_id=slide_id,
            user_id=user_id,
            layout_id=layout_id,
        )

    def _create_edited_presentation_version(
        self,
        *,
        presentation_id: UUID,
        slide_id: UUID,
        user_id: UUID,
        layout_id: str | None,
    ) -> dict[str, Any]:
        target = self.get_slide(
            presentation_id=presentation_id,
            slide_id=slide_id,
            user_id=user_id,
        )
        previous = self.get_latest_presentation_version(
            presentation_id=presentation_id,
            user_id=user_id,
        )
        presentation = self.get_presentation(presentation_id=presentation_id, user_id=user_id)
        plan = self.get_presentation_plan(
            presentation_plan_id=presentation["presentation_plan_id"],
            user_id=user_id,
        )
        framework = self.get_framework_version(
            framework_version_id=plan["framework_version_id"],
            user_id=user_id,
        )
        old_slides = [
            row
            for row in self.slides.values()
            if row["presentation_version_id"] == previous["id"]
        ]
        old_slides.sort(key=lambda row: row["slide_index"])
        version_id = uuid.uuid4()
        version = {
            "id": version_id,
            "presentation_id": presentation_id,
            "version_number": int(previous["version_number"]) + 1,
            "slides_json": [],
            "pptx_storage_path": None,
            "pdf_storage_path": None,
            "status": "generating",
            "journey_stage": previous.get("journey_stage"),
            "prior_stage_presentation_version_id": previous.get(
                "prior_stage_presentation_version_id"
            ),
            "generation_source_manifest": copy.deepcopy(
                previous.get("generation_source_manifest")
            ),
            "created_at": _now(),
        }
        self.presentation_versions[version_id] = version

        edited: dict[str, Any] | None = None
        specs: list[dict[str, Any]] = []
        for old_slide in old_slides:
            next_layout = layout_id if old_slide["id"] == target["id"] and layout_id else old_slide["layout_id"]
            if old_slide["id"] == target["id"]:
                references = [
                    "opportunity" if chapter_id == "0" else f"chapter_{chapter_id}"
                    for chapter_id in old_slide["source_chapter_ids"]
                ]
                spec = build_slide_spec_for_planned_slide(
                    planned={
                        "order": int(old_slide["slide_index"]) + 1,
                        "layoutId": next_layout,
                        "frameworkReferences": references,
                    },
                    framework_json=framework["framework_json"],
                )
            else:
                spec = copy.deepcopy(old_slide["slide_spec"])
            new_slide_id = uuid.uuid4()
            new_slide = {
                "id": new_slide_id,
                "presentation_version_id": version_id,
                "slide_index": old_slide["slide_index"],
                "layout_id": next_layout,
                "slide_spec": spec,
                "source_chapter_ids": list(spec["sourceChapterIds"]),
                "created_at": _now(),
            }
            self.slides[new_slide_id] = new_slide
            specs.append(spec)
            if old_slide["id"] == target["id"]:
                edited = new_slide
        version["slides_json"] = specs
        if settings.RENDERER_EXECUTION_MODE == "fixture":
            assets = materialize_fixture_deck_assets(
                version_id=version_id,
                slide_count=len(specs),
            )
            self.update_presentation_version_assets(
                presentation_version_id=version_id,
                assets=assets,
                status="ready",
            )
        if edited is None:
            raise not_found("SLIDE_NOT_FOUND", f"Slide {slide_id} was not found")
        return edited

    def list_slides(
        self,
        *,
        presentation_id: UUID,
        user_id: UUID,
        presentation_version_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        version = self._presentation_version(
            presentation_id=presentation_id,
            presentation_version_id=presentation_version_id,
            user_id=user_id,
        )
        rows = [
            row
            for row in self.slides.values()
            if row["presentation_version_id"] == version["id"]
        ]
        return sorted(rows, key=lambda row: row["slide_index"])

    def list_presentations_for_opportunity(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        accessible_plan_ids = {
            plan_id
            for plan_id, plan in self.presentation_plans.items()
            if self._user_can_access_plan(plan_id=plan_id, user_id=user_id)
            and self.framework_versions.get(plan["framework_version_id"], {}).get("opportunity_id")
            == opportunity_id
        }
        rows = [
            row
            for row in self.presentations.values()
            if row["presentation_plan_id"] in accessible_plan_ids
        ]
        return sorted(rows, key=lambda row: row["created_at"], reverse=True)

    def get_latest_presentation_for_opportunity(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        rows = self.list_presentations_for_opportunity(
            opportunity_id=opportunity_id,
            user_id=user_id,
        )
        if not rows:
            raise not_found(
                "PRESENTATION_NOT_FOUND",
                f"No presentation exists for opportunity {opportunity_id}",
            )
        return rows[0]

    def get_presentation_version(
        self,
        *,
        presentation_version_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        row = self.presentation_versions.get(presentation_version_id)
        if row is None:
            raise not_found(
                "PRESENTATION_VERSION_NOT_FOUND",
                f"Presentation version {presentation_version_id} was not found",
            )
        self.get_presentation(presentation_id=row["presentation_id"], user_id=user_id)
        return copy.deepcopy(row)

    def list_presentation_versions_for_opportunity(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        presentation_ids = {
            row["id"]
            for row in self.list_presentations_for_opportunity(
                opportunity_id=opportunity_id,
                user_id=user_id,
            )
        }
        rows = [
            copy.deepcopy(row)
            for row in self.presentation_versions.values()
            if row["presentation_id"] in presentation_ids
        ]
        return sorted(rows, key=_version_lineage_sort, reverse=True)

    def _presentation_version(
        self,
        *,
        presentation_id: UUID,
        presentation_version_id: UUID | None,
        user_id: UUID,
    ) -> dict[str, Any]:
        """The latest version, or the given one if it belongs to this presentation."""
        if presentation_version_id is None:
            return self.get_latest_presentation_version(
                presentation_id=presentation_id,
                user_id=user_id,
            )
        self.get_presentation(presentation_id=presentation_id, user_id=user_id)
        version = self.get_presentation_version(
            presentation_version_id=presentation_version_id,
            user_id=user_id,
        )
        if str(version["presentation_id"]) != str(presentation_id):
            raise not_found(
                "PRESENTATION_VERSION_NOT_FOUND",
                f"Presentation version {presentation_version_id} was not found",
            )
        return version

    def get_presentation_version_assets(
        self,
        *,
        presentation_id: UUID,
        user_id: UUID,
        presentation_version_id: UUID | None = None,
    ) -> dict[str, Any]:
        version = self._presentation_version(
            presentation_id=presentation_id,
            presentation_version_id=presentation_version_id,
            user_id=user_id,
        )
        if version.get("status") != "ready":
            raise bad_request(
                "PRESENTATION_NOT_READY",
                "Presentation version is not ready for preview or download",
            )
        return version

    def _user_can_access_plan(self, *, plan_id: UUID, user_id: UUID) -> bool:
        plan = self.presentation_plans.get(plan_id)
        if plan is None:
            return False
        framework = self.framework_versions.get(plan["framework_version_id"])
        if framework is None:
            return False
        opportunity = self.opportunities.get(framework["opportunity_id"])
        return opportunity is not None and opportunity["created_by"] == user_id

    def append_audit_log(
        self,
        *,
        actor_id: UUID,
        action: str,
        object_type: str,
        object_id: UUID,
        document_id: str | None = None,
        actor_email: str | None = None,
    ) -> dict[str, Any]:
        if not actor_email:
            profile = self.user_roles.get(actor_id)
            if profile:
                actor_email = profile.get("email")
        entry_id = uuid.uuid4()
        now = _now()
        row = {
            "id": entry_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "document_id": document_id or str(object_id),
            "timestamp": now,
        }
        self.audit_logs[entry_id] = row
        return row

    def list_audit_logs(
        self,
        *,
        actor_id: UUID | None = None,
        object_id: UUID | None = None,
        document_id: str | None = None,
        privileged: bool = False,
    ) -> list[dict[str, Any]]:
        rows = list(self.audit_logs.values())
        if actor_id is not None and not privileged:
            rows = [row for row in rows if row["actor_id"] == actor_id]
        if object_id is not None:
            rows = [row for row in rows if row["object_id"] == object_id]
        if document_id:
            rows = [row for row in rows if str(row.get("document_id") or "") == document_id]
        return sorted(rows, key=lambda row: row["timestamp"])

    def get_or_create_user_role(self, *, user_id: UUID, email: str) -> dict[str, Any]:
        existing = self.user_roles.get(user_id)
        if existing is not None:
            if email and existing.get("email") != email:
                existing["email"] = email
            return dict(existing)
        now = _now()
        # Memory/unit tests keep full pipeline access unless a test assigns a lower role.
        role = "admin"
        row = {
            "user_id": user_id,
            "email": email,
            "role": role,
            "created_at": now,
            "updated_at": now,
        }
        self.user_roles[user_id] = row
        return dict(row)

    def set_user_role(
        self,
        *,
        user_id: UUID,
        email: str,
        role: str,
    ) -> dict[str, Any]:
        now = _now()
        existing = self.user_roles.get(user_id)
        row = {
            "user_id": user_id,
            "email": email or (existing.get("email") if existing else ""),
            "role": role,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
        self.user_roles[user_id] = row
        return dict(row)

    def list_user_roles(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in sorted(self.user_roles.values(), key=lambda item: str(item.get("email") or ""))
        ]

    def append_egress_audit(
        self,
        *,
        opportunity_id: UUID | str,
        presentation_version_id: UUID | str,
        journey_stage: str,
        provider: str,
        pipeline_stage: str,
        decision: str,
        fields: list[dict[str, str]],
        attempt: int = 1,
    ) -> dict[str, Any]:
        entry_id = uuid.uuid4()
        row = {
            "id": entry_id,
            "opportunity_id": str(opportunity_id),
            "presentation_version_id": str(presentation_version_id),
            "journey_stage": journey_stage,
            "provider": provider,
            "pipeline_stage": pipeline_stage,
            "decision": decision,
            "fields": list(fields),
            "attempt": int(attempt),
            "created_at": _now(),
        }
        self.egress_audits[entry_id] = row
        return copy.deepcopy(row)

    def list_egress_audits(
        self,
        *,
        opportunity_id: UUID | str | None = None,
        presentation_version_id: UUID | str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self.egress_audits.values())
        if opportunity_id is not None:
            target = str(opportunity_id)
            rows = [row for row in rows if str(row["opportunity_id"]) == target]
        if presentation_version_id is not None:
            target = str(presentation_version_id)
            rows = [row for row in rows if str(row["presentation_version_id"]) == target]
        return [copy.deepcopy(row) for row in sorted(rows, key=lambda item: item["created_at"])]

    def ingest_approved_corpus(self, raw: dict[str, Any]) -> dict[str, Any]:
        from services.borek_rag.ingest import ingest_summary, plan_ingest

        plan = plan_ingest(raw)
        existing = next(
            (
                row
                for row in self.knowledge_corpus_versions.values()
                if row["corpus_key"] == plan.corpus_key and row["version"] == plan.version
            ),
            None,
        )
        replaced_existing = existing is not None
        version_id = existing["id"] if existing is not None else uuid.uuid4()
        if existing is not None:
            document_ids = [
                document_id
                for document_id, document in self.knowledge_documents.items()
                if document["corpus_version_id"] == version_id
            ]
            fact_ids = [
                fact_id
                for fact_id, fact in self.knowledge_facts.items()
                if fact["document_id"] in document_ids
            ]
            for fact_id in fact_ids:
                del self.knowledge_facts[fact_id]
            for document_id in document_ids:
                del self.knowledge_documents[document_id]
        for row in self.knowledge_corpus_versions.values():
            if (
                row["corpus_key"] == plan.corpus_key
                and row["status"] == "approved"
                and row["version"] != plan.version
            ):
                row["status"] = "retired"
        now = _now()
        self.knowledge_corpus_versions[version_id] = {
            "id": version_id,
            "corpus_key": plan.corpus_key,
            "version": plan.version,
            "status": "approved",
            "owner": plan.owner,
            "classification": plan.classification,
            "schema_version": plan.schema_version,
            "created_at": existing["created_at"] if existing is not None else now,
            "approved_at": now,
        }
        for document in plan.documents:
            document_id = uuid.uuid4()
            self.knowledge_documents[document_id] = {
                "id": document_id,
                "corpus_version_id": version_id,
                "document_key": document.document_key,
                "document_type": document.document_type,
                "source_uri": document.source_uri,
                "source_version": document.source_version,
                "classification": document.classification,
                "effective_from": document.effective_from,
                "effective_to": document.effective_to,
                "created_at": now,
            }
            for fact in document.facts:
                fact_id = uuid.uuid4()
                self.knowledge_facts[fact_id] = {
                    "id": fact_id,
                    "document_id": document_id,
                    "fact_key": fact.fact_key,
                    "kind": fact.kind,
                    "service_key": fact.service_key,
                    "query_key": fact.query_key,
                    "statement": fact.statement,
                    "payload": copy.deepcopy(fact.payload),
                    "search_terms": list(fact.search_terms),
                    "created_at": now,
                }
        return ingest_summary(plan, replaced_existing=replaced_existing)

    def list_approved_knowledge_facts(self) -> list[dict[str, Any]]:
        approved = {
            version_id: version
            for version_id, version in self.knowledge_corpus_versions.items()
            if version["status"] == "approved"
        }
        if not approved:
            return []
        readable = {
            document_id: document
            for document_id, document in self.knowledge_documents.items()
            if document["corpus_version_id"] in approved
            and document["classification"] in {"public", "internal"}
        }
        rows: list[dict[str, Any]] = []
        for fact in self.knowledge_facts.values():
            document = readable.get(fact["document_id"])
            if document is None:
                continue
            version = approved[document["corpus_version_id"]]
            rows.append(
                {
                    "fact_key": fact["fact_key"],
                    "kind": fact["kind"],
                    "service_key": fact["service_key"],
                    "query_key": fact["query_key"],
                    "statement": fact["statement"],
                    "payload": copy.deepcopy(fact["payload"]),
                    "search_terms": list(fact["search_terms"]),
                    "corpus_key": version["corpus_key"],
                    "corpus_version": version["version"],
                    "owner": version["owner"],
                    "schema_version": version.get("schema_version"),
                    "corpus_classification": version.get("classification"),
                    "document_key": document["document_key"],
                    "document_type": document["document_type"],
                    "document_version": document["source_version"],
                    "classification": document["classification"],
                    "effective_from": document["effective_from"],
                    "effective_to": document["effective_to"],
                }
            )
        return rows

    def get_approved_knowledge_corpus(self) -> dict[str, Any] | None:
        rows = self.list_approved_knowledge_facts()
        if not rows:
            return None
        first = rows[0]
        return {
            "source": "store",
            "corpus_key": first["corpus_key"],
            "version": first["corpus_version"],
            "status": "approved",
            "owner": first["owner"],
            "classification": first.get("corpus_classification") or first["classification"],
            "document_count": len({row["document_key"] for row in rows}),
            "fact_count": len(rows),
            "fact_kinds": sorted({row["kind"] for row in rows}),
        }

    def append_llm_call(self, record: Any) -> dict[str, Any]:
        row = _llm_call_row(record)
        self.llm_calls[row["id"]] = row
        return copy.deepcopy(row)

    def get_llm_calls_for_job(self, job_id: str) -> list[dict[str, Any]]:
        target = UUID(str(job_id))
        rows = [row for row in self.llm_calls.values() if row.get("job_id") == target]
        return [copy.deepcopy(row) for row in sorted(rows, key=lambda item: item["created_at"])]

    def save_email_draft(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        journey_stage: str,
        draft: dict[str, Any],
        expected_revision: int | None,
    ) -> dict[str, Any]:
        """Write one stage's draft only if it is still at ``expected_revision`` (None: no draft yet).

        Check and write are one step under the guard, and only this stage's entry is replaced in
        the current row - the same guarantee the database function gives the Supabase store.
        """
        from app.services.pitch_owner import user_can_access_opportunity

        with _EMAIL_DRAFT_GUARD:
            row = self.opportunities.get(opportunity_id)
            if row is None or not user_can_access_opportunity(row, user_id):
                raise not_found("OPPORTUNITY_NOT_FOUND", f"Opportunity {opportunity_id} was not found")
            drafts = row.get("email_drafts") if isinstance(row.get("email_drafts"), dict) else {}
            existing = drafts.get(journey_stage)
            current = None if existing is None else int(existing.get("revision") or 1)
            if current != expected_revision:
                raise conflict("EMAIL_DRAFT_CONFLICT", "The email draft was changed in the meantime. Reload it before saving.")
            now = _now()
            stored = {
                **copy.deepcopy(draft),
                "id": UUID(str(existing["id"])) if existing else uuid.uuid4(),
                "opportunity_id": opportunity_id,
                "journey_stage": journey_stage,
                "send_status": "not_sent",
                "revision": (current or 0) + 1,
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
            }
            row["email_drafts"] = {**drafts, journey_stage: stored}
            row["updated_at"] = now
            return copy.deepcopy(stored)

    def get_email_draft(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        journey_stage: str,
    ) -> dict[str, Any] | None:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        row = self.opportunities.get(opportunity_id)
        drafts = (row or {}).get("email_drafts") or {}
        stored = drafts.get(journey_stage)
        return copy.deepcopy(stored) if stored is not None else None

    def get_email_draft_by_id(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        draft_id: UUID,
    ) -> dict[str, Any]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        row = self.opportunities.get(opportunity_id)
        drafts = (row or {}).get("email_drafts") or {}
        for stored in drafts.values():
            if UUID(str(stored["id"])) == draft_id:
                return copy.deepcopy(stored)
        raise not_found("EMAIL_DRAFT_NOT_FOUND", "Email draft was not found")

    def upsert_transcript_summary(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        transcript_id: UUID,
        conversation_id: str,
        summary_json: dict[str, Any],
        prompt_version: str,
        generation_job_id: UUID | None = None,
    ) -> dict[str, Any]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        now = _now()
        row = {
            "transcript_id": transcript_id,
            "opportunity_id": opportunity_id,
            "conversation_id": conversation_id,
            "generation_job_id": generation_job_id,
            "schema_version": str(summary_json.get("schema_version") or "1.0"),
            "prompt_version": prompt_version,
            "processing_status": "completed",
            "summary_json": copy.deepcopy(summary_json),
            "created_at": now,
            "updated_at": now,
        }
        self.transcript_summaries[transcript_id] = row
        return copy.deepcopy(row)


_memory_store = MemoryDataStore()


def get_memory_store() -> MemoryDataStore:
    return _memory_store


def reset_memory_store() -> None:
    global _memory_store
    _memory_store = MemoryDataStore()
