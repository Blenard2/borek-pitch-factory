"""Supabase PostgREST data access with caller JWT for RLS (AT-40 / AT-41)."""

from __future__ import annotations

import copy
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException

from app.config import settings
from app.services.api_errors import bad_request, conflict, not_found, service_unavailable
from app.services.data.memory_store import ALLOWED_TRANSCRIPT_EXTENSIONS
from app.services.stage_b_orchestration import (
    build_slide_spec_for_planned_slide,
    plan_json_from_confirmed_framework,
    planned_slides_with_generators,
)
from app.services.deck_assets import (
    list_preview_image_paths,
    materialize_fixture_deck_assets,
    resolve_pdf_path,
    resolve_pptx_path,
)
from app.services.framework_status import require_reviewable_framework
from app.services.framework_stub_template import load_framework_stub_template
from services.framework.stage1_intake import apply_persisted_intake_to_framework
from services.transcript.ingestion import transcript_mime_allowed

logger = logging.getLogger(__name__)
_HTTP_CLIENT: httpx.Client | None = None
_TRANSIENT_ERRORS = (httpx.TimeoutException, httpx.NetworkError)
_MAX_ATTEMPTS = 3

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[6]
    / "packages"
    / "contracts"
    / "fixtures"
    / "framework_object.minimal.json"
)


def _supabase_auth_headers(access_token: str) -> dict[str, str]:
    """Build user- or privileged-request headers without treating secret keys as JWTs."""
    service_credential = settings.SUPABASE_SERVICE_ROLE_KEY
    is_privileged = access_token == service_credential
    headers = {
        "apikey": service_credential if is_privileged else settings.SUPABASE_ANON_KEY,
    }
    if not (is_privileged and service_credential.startswith("sb_secret_")):
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _shared_client() -> httpx.Client:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.Client(
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _HTTP_CLIENT


def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    json: dict[str, Any] | list[dict[str, Any]] | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            kwargs: dict[str, Any] = {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
            }
            if content is not None:
                kwargs["content"] = content
            elif json is not None:
                kwargs["json"] = json
            return _shared_client().request(**kwargs)
        except _TRANSIENT_ERRORS as exc:
            last_error = exc
            logger.warning(
                "Supabase request %s %s failed (attempt %s/%s): %s",
                method,
                url,
                attempt + 1,
                _MAX_ATTEMPTS,
                exc,
            )
            if attempt + 1 < _MAX_ATTEMPTS:
                time.sleep(0.4 * (attempt + 1))
    raise service_unavailable(
        "SUPABASE_UNAVAILABLE",
        "Supabase timed out. Retry the action.",
    ) from last_error


def _json_safe_value(value: Any) -> Any:
    """Recursively coerce UUID/datetime values for PostgREST JSON bodies (AT-37)."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_value(item) for item in value]
    return value


def _parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _normalize_opportunity(row: dict[str, Any]) -> dict[str, Any]:
    from app.services.stage1_intake_store import legacy_row_without_intake_columns, present_opportunity

    normalized = legacy_row_without_intake_columns(row)
    return present_opportunity(
        {
            **normalized,
            "id": UUID(str(normalized["id"])),
            "created_by": UUID(str(normalized["created_by"])),
            "created_at": _parse_timestamp(normalized["created_at"]),
            "updated_at": _parse_timestamp(normalized["updated_at"]),
            "pii_redaction_enabled": bool(normalized.get("pii_redaction_enabled", True)),
            "additional_client_information": normalized.get("additional_client_information"),
            "followup_statics": normalized.get("followup_statics"),
        }
    )


def _normalize_client_logo(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "id": UUID(str(row["id"])),
        "opportunity_id": UUID(str(row["opportunity_id"])),
        "created_by": UUID(str(row["created_by"])),
        "size_bytes": int(row["size_bytes"]),
        "width_px": int(row["width_px"]) if row.get("width_px") is not None else None,
        "height_px": int(row["height_px"]) if row.get("height_px") is not None else None,
        "uploaded_at": _parse_timestamp(row["uploaded_at"]),
    }


def _normalize_transcript(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "id": UUID(str(row["id"])),
        "opportunity_id": UUID(str(row["opportunity_id"])),
        "created_at": _parse_timestamp(row["created_at"]),
    }


def _normalize_client_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "id": UUID(str(row["id"])),
        "opportunity_id": UUID(str(row["opportunity_id"])),
        "created_at": _parse_timestamp(row["created_at"]),
    }


def _present_client_document(row: dict[str, Any], *, section_count: int) -> dict[str, Any]:
    return {
        "id": row["id"],
        "opportunity_id": row["opportunity_id"],
        "file_name": row["file_name"],
        "mime_type": row["mime_type"],
        "document_key": row["document_key"],
        "processing_status": row["processing_status"],
        "section_count": section_count,
        "created_at": row["created_at"],
    }


def _normalize_discovery_paper_version(row: dict[str, Any]) -> dict[str, Any]:
    approved_at = row.get("approved_at")
    return {
        **row,
        "id": UUID(str(row["id"])),
        "opportunity_id": UUID(str(row["opportunity_id"])),
        "document_id": UUID(str(row["document_id"])),
        "created_by": UUID(str(row["created_by"])),
        "created_at": _parse_timestamp(row["created_at"]),
        "updated_at": _parse_timestamp(row["updated_at"]),
        "approved_at": _parse_timestamp(approved_at) if approved_at else None,
    }


def _normalize_framework(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "id": UUID(str(row["id"])),
        "opportunity_id": UUID(str(row["opportunity_id"])),
        "created_by": UUID(str(row["created_by"])),
        "created_at": _parse_timestamp(row["created_at"]),
    }


def _normalize_presentation_plan(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "id": UUID(str(row["id"])),
        "framework_version_id": UUID(str(row["framework_version_id"])),
        "created_at": _parse_timestamp(row["created_at"]),
    }


def _normalize_presentation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "id": UUID(str(row["id"])),
        "presentation_plan_id": UUID(str(row["presentation_plan_id"])),
        "created_at": _parse_timestamp(row["created_at"]),
    }


def _normalize_presentation_version(row: dict[str, Any]) -> dict[str, Any]:
    prior = row.get("prior_stage_presentation_version_id")
    return {
        **row,
        "id": UUID(str(row["id"])),
        "presentation_id": UUID(str(row["presentation_id"])),
        "prior_stage_presentation_version_id": UUID(str(prior)) if prior else None,
        "created_at": _parse_timestamp(row["created_at"]),
    }


def _normalize_slide(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "id": UUID(str(row["id"])),
        "presentation_version_id": UUID(str(row["presentation_version_id"])),
        "created_at": _parse_timestamp(row["created_at"]),
    }


def _normalize_generation_job(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        **row,
        "id": UUID(str(row["id"])),
        "opportunity_id": UUID(str(row["opportunity_id"])),
        "presentation_id": (
            UUID(str(row["presentation_id"])) if row.get("presentation_id") else None
        ),
    }
    for field in ("started_at", "completed_at", "created_at"):
        if normalized.get(field):
            normalized[field] = _parse_timestamp(normalized[field])
    return normalized


class SupabaseDataStore:
    """PostgREST client scoped to the authenticated user's access token."""

    def __init__(self, access_token: str) -> None:
        self._base_url = settings.SUPABASE_URL.rstrip("/")
        self._headers = {
            **_supabase_auth_headers(access_token),
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def _storage_auth_headers(self) -> dict[str, str]:
        return {
            name: self._headers[name]
            for name in ("apikey", "Authorization")
            if name in self._headers
        }

    def create_generation_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = _json_safe_value(copy.deepcopy(payload))
        response = self._request("POST", "generation_jobs", json_body=body)
        if response.status_code == 409 and body.get("generation_lock_key"):
            # Unique violation on generation_jobs_one_active_locked_generation: the lock is held.
            from app.services.job_service import GenerationLockConflict

            raise GenerationLockConflict(str(body["generation_lock_key"]))
        if response.status_code not in (200, 201):
            raise bad_request("JOB_CREATE_FAILED", response.text)
        return _normalize_generation_job(response.json()[0])

    def get_generation_job(self, job_id: UUID) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            "generation_jobs",
            params={"id": f"eq.{job_id}", "select": "*", "limit": "1"},
        )
        if response.status_code != 200:
            raise bad_request("JOB_READ_FAILED", response.text)
        rows = response.json()
        return _normalize_generation_job(rows[0]) if rows else None

    def get_active_job_for_opportunity(
        self,
        opportunity_id: str | UUID,
        stage_group: str | None = None,
        *,
        job_type: str | None = None,
    ) -> dict[str, Any] | None:
        from app.services.job_service import select_reconnect_job

        params: dict[str, str] = {
            "select": "*",
            "opportunity_id": f"eq.{opportunity_id}",
            "order": "created_at.desc,id.desc",
        }
        if job_type:
            params["job_type"] = f"eq.{job_type}"
        elif stage_group == "framework":
            params["job_type"] = "ilike.*framework*"
        elif stage_group == "presentation":
            params["or"] = "(job_type.ilike.*presentation*,job_type.ilike.*slide*)"
        response = self._request("GET", "generation_jobs", params=params)
        if response.status_code != 200:
            raise bad_request("JOB_LIST_FAILED", response.text)
        rows = [_normalize_generation_job(row) for row in response.json()]
        return select_reconnect_job(rows, stage_group=stage_group)

    def get_latest_generation_job_for_opportunity(
        self,
        opportunity_id: UUID,
    ) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            "generation_jobs",
            params={
                "opportunity_id": f"eq.{opportunity_id}",
                "select": "*",
                "order": "created_at.desc,id.desc",
                "limit": "1",
            },
        )
        if response.status_code != 200:
            raise bad_request("JOB_READ_FAILED", response.text)
        rows = response.json()
        return _normalize_generation_job(rows[0]) if rows else None

    def list_generation_jobs_for_opportunity(
        self,
        opportunity_id: UUID,
    ) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            "generation_jobs",
            params={
                "opportunity_id": f"eq.{opportunity_id}",
                "select": "*",
                "order": "created_at.asc,id.asc",
            },
        )
        if response.status_code != 200:
            raise bad_request("JOB_LIST_FAILED", response.text)
        return [_normalize_generation_job(row) for row in response.json()]

    def update_generation_job(
        self,
        job_id: UUID,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        body = _json_safe_value(copy.deepcopy(updates))
        response = self._request(
            "PATCH",
            "generation_jobs",
            params={"id": f"eq.{job_id}"},
            json_body=body,
        )
        if response.status_code != 200 or not response.json():
            raise not_found("JOB_NOT_FOUND", f"No job found with id {job_id}")
        return _normalize_generation_job(response.json()[0])

    def _request(
        self,
        method: str,
        table: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return _request_with_retry(
            method,
            f"{self._base_url}/rest/v1/{table}",
            headers={**self._headers, **(headers or {})},
            params=params,
            json=json_body,
        )

    def _service_role_request(
        self,
        method: str,
        resource: str,
        *,
        json_body: dict[str, Any] | list[dict[str, Any]] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        service_store = SupabaseDataStore(settings.SUPABASE_SERVICE_ROLE_KEY)
        return service_store._request(
            method,
            resource,
            json_body=json_body,
            params=params,
            headers=headers,
        )

    def _upload_transcript_content(
        self,
        *,
        storage_path: str,
        mime_type: str,
        content: bytes,
    ) -> None:
        headers = {
            **self._storage_auth_headers(),
            "Content-Type": mime_type,
            "x-upsert": "false",
        }
        response = _request_with_retry(
            "POST",
            f"{self._base_url}/storage/v1/object/transcripts/{storage_path}",
            headers=headers,
            content=content,
        )
        if response.status_code not in (200, 201):
            raise bad_request("TRANSCRIPT_STORAGE_FAILED", response.text)

    def _delete_transcript_content(self, *, storage_path: str) -> None:
        if not storage_path:
            return
        headers = self._storage_auth_headers()
        response = _request_with_retry(
            "DELETE",
            f"{self._base_url}/storage/v1/object/transcripts/{storage_path}",
            headers=headers,
        )
        if response.status_code not in (200, 204, 404):
            logger.warning(
                "Transcript storage cleanup failed for %s: HTTP %s",
                storage_path,
                response.status_code,
            )

    def _upload_client_document_content(
        self,
        *,
        storage_path: str,
        mime_type: str,
        content: bytes,
    ) -> None:
        headers = {
            **self._storage_auth_headers(),
            "Content-Type": mime_type,
            "x-upsert": "false",
        }
        response = _request_with_retry(
            "POST",
            f"{self._base_url}/storage/v1/object/client_documents/{storage_path}",
            headers=headers,
            content=content,
        )
        if response.status_code not in (200, 201):
            raise bad_request("CLIENT_DOCUMENT_STORAGE_FAILED", response.text)

    def _delete_client_document_content(self, *, storage_path: str) -> None:
        if not storage_path:
            return
        headers = self._storage_auth_headers()
        response = _request_with_retry(
            "DELETE",
            f"{self._base_url}/storage/v1/object/client_documents/{storage_path}",
            headers=headers,
        )
        if response.status_code not in (200, 204, 404):
            logger.warning(
                "Client document storage cleanup failed for %s: HTTP %s",
                storage_path,
                response.status_code,
            )

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
        from app.services.stage1_intake_store import apply_intake_columns

        payload = {
            "client_name": client_name,
            "opportunity_name": opportunity_name,
            "department": department,
            "language": language,
            "pitch_owner": pitch_owner or employee_pitch_owner(user_id),
            "team_members": team_members if team_members is not None else [],
            "pitch_description": pitch_description,
            "status": "active",
            "pii_redaction_enabled": bool(pii_redaction_enabled),
            "additional_client_information": additional_client_information,
            "followup_statics": followup_statics,
            "created_by": str(user_id),
        }
        apply_intake_columns(payload, stage1_intake)
        response = self._request("POST", "opportunities", json_body=payload)
        if response.status_code not in (200, 201):
            raise bad_request("OPPORTUNITY_CREATE_FAILED", response.text)
        row = response.json()[0]
        return _normalize_opportunity(row)

    def list_opportunities(self, *, user_id: UUID) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            "opportunities",
            params={
                "select": "*",
                "created_by": f"eq.{user_id}",
                "order": "created_at.desc",
            },
        )
        if response.status_code != 200:
            raise bad_request("OPPORTUNITY_LIST_FAILED", response.text)
        return [_normalize_opportunity(row) for row in response.json()]

    def load_recent_work_index(
        self,
        *,
        user_id: UUID,
        opportunity_ids: list[UUID],
    ) -> dict[str, Any]:
        _ = user_id
        if not opportunity_ids:
            return {
                "transcripts": {},
                "frameworks": {},
                "jobs": {},
                "plans": {},
                "presentations": {},
            }
        in_filter = f"in.({','.join(str(item) for item in opportunity_ids)})"

        def rows(table: str, params: dict[str, str], error_code: str) -> list[dict[str, Any]]:
            response = self._request("GET", table, params=params)
            if response.status_code != 200:
                raise bad_request(error_code, response.text)
            payload = response.json()
            return payload if isinstance(payload, list) else []

        transcript_rows = rows(
            "transcripts",
            {"select": "opportunity_id,created_at", "opportunity_id": in_filter},
            "TRANSCRIPT_LIST_FAILED",
        )
        framework_rows = rows(
            "framework_versions",
            {
                "select": "id,opportunity_id,status,created_at,version_number",
                "opportunity_id": in_filter,
                "order": "version_number.desc",
            },
            "FRAMEWORK_LIST_FAILED",
        )
        job_rows = rows(
            "generation_jobs",
            {
                "select": "id,opportunity_id,job_type,status,current_stage,auto_continue,started_at,completed_at,created_at",
                "opportunity_id": in_filter,
                "order": "created_at.desc,id.desc",
            },
            "JOB_LIST_FAILED",
        )
        framework_ids = [str(row.get("id")) for row in framework_rows if row.get("id")]
        plan_rows: list[dict[str, Any]] = []
        if framework_ids:
            plan_rows = rows(
                "presentation_plans",
                {
                    "select": "id,created_at,framework_version_id",
                    "framework_version_id": f"in.({','.join(framework_ids)})",
                    "order": "created_at.desc",
                },
                "PRESENTATION_PLAN_LIST_FAILED",
            )
        plan_ids = [str(row.get("id")) for row in plan_rows if row.get("id")]
        presentation_rows: list[dict[str, Any]] = []
        if plan_ids:
            presentation_rows = rows(
                "presentations",
                {
                    "select": "id,name,created_at,presentation_plan_id",
                    "presentation_plan_id": f"in.({','.join(plan_ids)})",
                    "order": "created_at.desc",
                },
                "PRESENTATION_LIST_FAILED",
            )
        presentation_ids = [str(row.get("id")) for row in presentation_rows if row.get("id")]
        version_rows: list[dict[str, Any]] = []
        if presentation_ids:
            version_rows = rows(
                "presentation_versions",
                {
                    "select": "id,presentation_id,status,version_number",
                    "presentation_id": f"in.({','.join(presentation_ids)})",
                    "order": "version_number.desc",
                },
                "PRESENTATION_VERSION_LIST_FAILED",
            )

        transcripts: dict[str, list[dict[str, Any]]] = {str(item): [] for item in opportunity_ids}
        for row in transcript_rows:
            transcripts.setdefault(str(row.get("opportunity_id")), []).append(
                {"created_at": row.get("created_at")}
            )

        frameworks: dict[str, dict[str, Any]] = {}
        for row in framework_rows:
            key = str(row.get("opportunity_id"))
            if key not in frameworks:
                frameworks[key] = {
                    "id": row.get("id"),
                    "status": row.get("status"),
                    "created_at": row.get("created_at"),
                    "version_number": row.get("version_number") or 0,
                }

        jobs: dict[str, list[dict[str, Any]]] = {str(item): [] for item in opportunity_ids}
        for row in job_rows:
            jobs.setdefault(str(row.get("opportunity_id")), []).append(_normalize_generation_job(row))

        framework_to_opp = {str(row.get("id")): str(row.get("opportunity_id")) for row in framework_rows}
        plans: dict[str, dict[str, Any]] = {}
        plan_to_opp: dict[str, str] = {}
        for row in plan_rows:
            opp_id = framework_to_opp.get(str(row.get("framework_version_id")))
            if not opp_id:
                continue
            plan_to_opp[str(row.get("id"))] = opp_id
            if opp_id not in plans:
                plans[opp_id] = {"id": row.get("id"), "created_at": row.get("created_at")}

        latest_version: dict[str, dict[str, Any]] = {}
        for row in version_rows:
            key = str(row.get("presentation_id"))
            if key not in latest_version:
                latest_version[key] = row

        presentations: dict[str, dict[str, Any]] = {}
        for row in presentation_rows:
            opp_id = plan_to_opp.get(str(row.get("presentation_plan_id")))
            if not opp_id or opp_id in presentations:
                continue
            version = latest_version.get(str(row.get("id")))
            presentations[opp_id] = {
                "id": row.get("id"),
                "name": row.get("name"),
                "created_at": row.get("created_at"),
                "version_status": None if version is None else version.get("status"),
            }

        return {
            "transcripts": transcripts,
            "frameworks": frameworks,
            "jobs": jobs,
            "plans": plans,
            "presentations": presentations,
        }

    def _fetch_opportunity_row(self, opportunity_id: UUID) -> dict[str, Any] | None:
        """Load by primary key; RLS on the caller JWT enforces row access."""
        response = self._request(
            "GET",
            "opportunities",
            params={
                "select": "*",
                "id": f"eq.{opportunity_id}",
                "limit": "1",
            },
        )
        if response.status_code != 200 or not response.json():
            return None
        return response.json()[0]

    def get_opportunity(self, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
        from app.services.pitch_owner import user_can_access_opportunity

        row = self._fetch_opportunity_row(opportunity_id)
        if row is None or not user_can_access_opportunity(row, user_id):
            raise not_found("OPPORTUNITY_NOT_FOUND", f"Opportunity {opportunity_id} was not found")
        return _normalize_opportunity(row)

    def update_opportunity(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        from app.services.journey_outputs_store import (
            JOURNEY_OUTPUT_DB_COLUMNS,
            reject_finalization_snapshot_change,
        )
        from app.services.stage1_intake_store import STAGE1_DB_COLUMNS, expand_opportunity_updates

        current = self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        expanded = expand_opportunity_updates(dict(updates))
        reject_finalization_snapshot_change(current, expanded)
        payload = {
            key: value
            for key, value in expanded.items()
            if value is not None
            or key in {
                "additional_client_information",
                "followup_statics",
                *STAGE1_DB_COLUMNS,
                *JOURNEY_OUTPUT_DB_COLUMNS,
            }
        }
        payload["updated_at"] = datetime.now(UTC).isoformat()
        response = self._request(
            "PATCH",
            "opportunities",
            params={"id": f"eq.{opportunity_id}"},
            json_body=payload,
        )
        if response.status_code not in (200, 201, 204):
            raise not_found("OPPORTUNITY_NOT_FOUND", f"Opportunity {opportunity_id} was not found")
        rows = response.json() if response.content else []
        if rows:
            row = rows[0]
            from app.services.pitch_owner import user_can_access_opportunity

            if not user_can_access_opportunity(row, user_id):
                raise not_found("OPPORTUNITY_NOT_FOUND", f"Opportunity {opportunity_id} was not found")
            return _normalize_opportunity(row)
        return self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)

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

        The database function ``save_email_draft`` (migration 043) changes only this stage inside
        the locked row and checks the revision in the same statement. The whole ``email_drafts``
        object is never sent back, so a save of another stage at the same moment is not lost, and
        two API processes saving the same draft cannot both succeed.
        """
        opportunity = self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        existing = (opportunity.get("email_drafts") or {}).get(journey_stage)
        current = None if existing is None else int(existing.get("revision") or 1)
        stale = conflict("EMAIL_DRAFT_CONFLICT", "The email draft was changed in the meantime. Reload it before saving.")
        if current != expected_revision:
            raise stale
        now = datetime.now(UTC).isoformat()
        stored = {
            **draft,
            "id": str(existing["id"]) if existing else str(uuid4()),
            "opportunity_id": str(opportunity_id),
            "journey_stage": journey_stage,
            "send_status": "not_sent",
            "revision": (current or 0) + 1,
            "created_at": existing.get("created_at") if existing else now,
            "updated_at": now,
        }
        response = self._request(
            "POST",
            "rpc/save_email_draft",
            json_body={
                "p_opportunity_id": str(opportunity_id),
                "p_journey_stage": journey_stage,
                "p_expected_revision": expected_revision,
                "p_draft": stored,
            },
        )
        if response.status_code not in (200, 201):
            raise bad_request("EMAIL_DRAFT_SAVE_FAILED", "The email draft could not be saved.")
        saved = response.json()
        if not isinstance(saved, dict):
            # No row matched: the draft moved on (or was created) after it was read.
            raise stale
        return saved

    def get_email_draft(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        journey_stage: str,
    ) -> dict[str, Any] | None:
        opportunity = self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        drafts = opportunity.get("email_drafts") or {}
        row = drafts.get(journey_stage)
        return dict(row) if row is not None else None

    def get_email_draft_by_id(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        draft_id: UUID,
    ) -> dict[str, Any]:
        opportunity = self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        drafts = opportunity.get("email_drafts") or {}
        for row in drafts.values():
            if str(row.get("id")) == str(draft_id):
                return dict(row)
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
        self.get_transcript(
            opportunity_id=opportunity_id,
            transcript_id=transcript_id,
            user_id=user_id,
        )
        now = datetime.now(UTC).isoformat()
        payload = {
            "transcript_id": str(transcript_id),
            "opportunity_id": str(opportunity_id),
            "conversation_id": conversation_id,
            "generation_job_id": str(generation_job_id) if generation_job_id else None,
            "schema_version": str(summary_json.get("schema_version") or "1.0"),
            "prompt_version": prompt_version,
            "processing_status": "completed",
            "summary_json": summary_json,
            "updated_at": now,
        }
        # Table grants are service_role only (031_transcript_summaries.sql).
        scope = {
            "transcript_id": f"eq.{transcript_id}",
            "opportunity_id": f"eq.{opportunity_id}",
        }
        existing = self._service_role_request(
            "GET",
            "transcript_summaries",
            params={"select": "transcript_id", "limit": "1", **scope},
        )
        if existing.status_code == 200 and existing.json():
            response = self._service_role_request(
                "PATCH",
                "transcript_summaries",
                params=scope,
                json_body=payload,
            )
        else:
            payload["created_at"] = now
            response = self._service_role_request(
                "POST",
                "transcript_summaries",
                json_body=payload,
            )
        if response.status_code not in (200, 201, 204):
            raise bad_request("TRANSCRIPT_SUMMARY_STORE_FAILED", response.text)
        return payload

    def _upload_client_logo_content(
        self,
        *,
        storage_path: str,
        mime_type: str,
        content: bytes,
    ) -> None:
        headers = {
            "apikey": self._headers["apikey"],
            "Authorization": self._headers["Authorization"],
            "Content-Type": mime_type,
            "x-upsert": "false",
        }
        response = _request_with_retry(
            "POST",
            f"{self._base_url}/storage/v1/object/client-logos/{storage_path}",
            headers=headers,
            content=content,
        )
        if response.status_code not in (200, 201):
            raise bad_request("CLIENT_LOGO_STORAGE_FAILED", response.text)

    def _delete_client_logo_content(self, *, storage_path: str) -> None:
        if not storage_path:
            return
        headers = {
            "apikey": self._headers["apikey"],
            "Authorization": self._headers["Authorization"],
        }
        response = _request_with_retry(
            "DELETE",
            f"{self._base_url}/storage/v1/object/client-logos/{storage_path}",
            headers=headers,
        )
        if response.status_code not in (200, 204, 404):
            logger.warning(
                "Client logo storage cleanup failed for %s: HTTP %s",
                storage_path,
                response.status_code,
            )

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
        response = self._request(
            "GET",
            "opportunity_client_logos",
            params={"opportunity_id": f"eq.{opportunity_id}", "select": "*", "limit": "1"},
        )
        if response.status_code != 200:
            raise bad_request("CLIENT_LOGO_READ_FAILED", response.text)
        previous = response.json()[0] if response.json() else None
        self._upload_client_logo_content(
            storage_path=storage_path,
            mime_type=mime_type,
            content=content,
        )
        payload = {
            "opportunity_id": str(opportunity_id),
            "created_by": str(user_id),
            "file_name": file_name,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "width_px": width_px,
            "height_px": height_px,
            "storage_path": storage_path,
            "uploaded_at": datetime.now(UTC).isoformat(),
        }
        if previous:
            metadata_response = self._request(
                "PATCH",
                "opportunity_client_logos",
                params={"id": f"eq.{previous['id']}"},
                json_body=payload,
            )
        else:
            metadata_response = self._request(
                "POST",
                "opportunity_client_logos",
                json_body=payload,
            )
        if metadata_response.status_code not in (200, 201) or not metadata_response.json():
            self._delete_client_logo_content(storage_path=storage_path)
            raise bad_request("CLIENT_LOGO_SAVE_FAILED", metadata_response.text)
        if previous and previous.get("storage_path") != storage_path:
            self._delete_client_logo_content(storage_path=str(previous["storage_path"]))
        return _normalize_client_logo(metadata_response.json()[0]), previous is not None

    def get_client_logo(self, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        response = self._request(
            "GET",
            "opportunity_client_logos",
            params={"opportunity_id": f"eq.{opportunity_id}", "select": "*", "limit": "1"},
        )
        if response.status_code != 200 or not response.json():
            raise not_found("CLIENT_LOGO_NOT_FOUND", "No client logo is stored for this opportunity")
        return _normalize_client_logo(response.json()[0])

    def get_client_logo_content(self, *, opportunity_id: UUID, user_id: UUID) -> bytes:
        row = self.get_client_logo(opportunity_id=opportunity_id, user_id=user_id)
        headers = {
            "apikey": self._headers["apikey"],
            "Authorization": self._headers["Authorization"],
        }
        response = _request_with_retry(
            "GET",
            f"{self._base_url}/storage/v1/object/client-logos/{row['storage_path']}",
            headers=headers,
        )
        if response.status_code != 200:
            raise not_found("CLIENT_LOGO_NOT_FOUND", "Client logo bytes are not available")
        return response.content

    def mint_client_logo_provider_url(
        self,
        *,
        opportunity_id: UUID,
        ttl_seconds: int,
    ) -> str | None:
        """Retired provider-logo mint. Stored logos remain readable; no signed fetch URL is issued."""
        del opportunity_id, ttl_seconds
        return None

    def get_client_logo_for_signed_fetch(
        self, *, opportunity_id: UUID
    ) -> tuple[dict[str, Any], bytes]:
        """JJ-29: HMAC-gated lookup via the service-role store. No caller JWT."""
        response = self._request(
            "GET",
            "opportunity_client_logos",
            params={"opportunity_id": f"eq.{opportunity_id}", "select": "*", "limit": "1"},
        )
        if response.status_code != 200 or not response.json():
            raise not_found("CLIENT_LOGO_NOT_FOUND", "No client logo is stored for this opportunity")
        row = _normalize_client_logo(response.json()[0])
        headers = {
            "apikey": self._headers["apikey"],
            "Authorization": self._headers["Authorization"],
        }
        content = _request_with_retry(
            "GET",
            f"{self._base_url}/storage/v1/object/client-logos/{row['storage_path']}",
            headers=headers,
        )
        if content.status_code != 200:
            raise not_found("CLIENT_LOGO_NOT_FOUND", "Client logo bytes are not available")
        return row, content.content

    def delete_client_logo(self, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
        row = self.get_client_logo(opportunity_id=opportunity_id, user_id=user_id)
        response = self._request(
            "DELETE",
            "opportunity_client_logos",
            params={"id": f"eq.{row['id']}"},
        )
        if response.status_code not in (200, 204):
            raise bad_request("CLIENT_LOGO_DELETE_FAILED", response.text)
        self._delete_client_logo_content(storage_path=str(row["storage_path"]))
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
        self._upload_transcript_content(
            storage_path=storage_path,
            mime_type=mime_type,
            content=content,
        )
        payload = {
            "opportunity_id": str(opportunity_id),
            "file_name": file_name,
            "mime_type": mime_type,
            "storage_path": storage_path,
            "conversation_id": conversation_id,
            "processing_status": "pending",
        }
        response = self._request("POST", "transcripts", json_body=payload)
        if response.status_code not in (200, 201):
            raise bad_request("TRANSCRIPT_UPLOAD_FAILED", response.text)
        transcript = _normalize_transcript(response.json()[0])
        section_payloads = [
            {
                "transcript_id": str(transcript["id"]),
                "section_index": int(section["section_index"]),
                "speaker_role": section.get("speaker_role"),
                "content": str(section["content"]),
                "metadata": copy.deepcopy(section.get("metadata") or {}),
            }
            for section in sections
        ]
        section_response = self._request(
            "POST",
            "transcript_sections",
            json_body=section_payloads,
        )
        if section_response.status_code not in (200, 201):
            raise bad_request("TRANSCRIPT_SECTIONS_CREATE_FAILED", section_response.text)
        return transcript

    def list_transcripts(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        verify_owner: bool = True,
    ) -> list[dict[str, Any]]:
        if verify_owner:
            self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        response = self._request(
            "GET",
            "transcripts",
            params={
                "select": "*",
                "opportunity_id": f"eq.{opportunity_id}",
                "order": "created_at.asc",
            },
        )
        if response.status_code != 200:
            raise bad_request("TRANSCRIPT_LIST_FAILED", response.text)
        return [_normalize_transcript(row) for row in response.json()]

    def list_transcript_sources(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        """Return persisted speaker turns for Stage A under caller RLS."""
        transcripts = self.list_transcripts(
            opportunity_id=opportunity_id,
            user_id=user_id,
        )
        sources: list[dict[str, Any]] = []
        for transcript in transcripts:
            response = self._request(
                "GET",
                "transcript_sections",
                params={
                    "select": "section_index,speaker_role,content,metadata",
                    "transcript_id": f"eq.{transcript['id']}",
                    "order": "section_index.asc",
                },
            )
            if response.status_code != 200:
                raise bad_request("TRANSCRIPT_SECTIONS_LIST_FAILED", response.text)
            sources.append(
                {
                    "id": transcript["id"],
                    "file_name": transcript["file_name"],
                    "conversation_id": transcript["conversation_id"],
                    "sections": response.json(),
                }
            )
        return sources

    def update_transcript_processing_status(
        self,
        *,
        opportunity_id: UUID,
        transcript_id: UUID,
        user_id: UUID,
        processing_status: str,
    ) -> None:
        self.get_transcript(
            opportunity_id=opportunity_id,
            transcript_id=transcript_id,
            user_id=user_id,
        )
        response = self._request(
            "PATCH",
            "transcripts",
            params={"id": f"eq.{transcript_id}"},
            json_body={"processing_status": processing_status},
        )
        if response.status_code not in (200, 204):
            raise bad_request("TRANSCRIPT_STATUS_UPDATE_FAILED", response.text)

    def get_transcript(
        self,
        *,
        opportunity_id: UUID,
        transcript_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        _ = user_id
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        response = self._request(
            "GET",
            "transcripts",
            params={
                "select": "*",
                "id": f"eq.{transcript_id}",
                "opportunity_id": f"eq.{opportunity_id}",
                "limit": "1",
            },
        )
        if response.status_code != 200 or not response.json():
            raise not_found("TRANSCRIPT_NOT_FOUND", f"Transcript {transcript_id} was not found")
        return _normalize_transcript(response.json()[0])

    def regenerate_transcript(
        self,
        *,
        opportunity_id: UUID,
        transcript_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        _ = user_id
        self.get_transcript(
            opportunity_id=opportunity_id,
            transcript_id=transcript_id,
            user_id=user_id,
        )
        response = self._request(
            "PATCH",
            "transcripts",
            params={"id": f"eq.{transcript_id}"},
            json_body={"processing_status": "pending"},
        )
        if response.status_code not in (200, 204) or not response.json():
            raise not_found("TRANSCRIPT_NOT_FOUND", f"Transcript {transcript_id} was not found")
        return _normalize_transcript(response.json()[0])

    def delete_transcript(
        self,
        *,
        opportunity_id: UUID,
        transcript_id: UUID,
        user_id: UUID,
    ) -> None:
        row = self.get_transcript(
            opportunity_id=opportunity_id,
            transcript_id=transcript_id,
            user_id=user_id,
        )
        storage_path = str(row.get("storage_path") or "").strip()
        response = self._request(
            "DELETE",
            "transcripts",
            params={
                "id": f"eq.{transcript_id}",
                "opportunity_id": f"eq.{opportunity_id}",
            },
        )
        if response.status_code == 204:
            self._delete_transcript_content(storage_path=storage_path)
            return
        if response.status_code != 200 or not response.json():
            raise not_found("TRANSCRIPT_NOT_FOUND", f"Transcript {transcript_id} was not found")
        self._delete_transcript_content(storage_path=storage_path)

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
        self._upload_client_document_content(
            storage_path=storage_path,
            mime_type=mime_type,
            content=content,
        )
        payload = {
            "opportunity_id": str(opportunity_id),
            "file_name": file_name,
            "mime_type": mime_type,
            "storage_path": storage_path,
            "document_key": document_key,
            "processing_status": processing_status,
        }
        response = self._request("POST", "client_documents", json_body=payload)
        if response.status_code not in (200, 201):
            raise bad_request("CLIENT_DOCUMENT_UPLOAD_FAILED", response.text)
        document = _normalize_client_document(response.json()[0])
        section_payloads = [
            {
                "client_document_id": str(document["id"]),
                "section_index": int(section["section_index"]),
                "content": str(section["content"]),
                "metadata": copy.deepcopy(section.get("metadata") or {}),
            }
            for section in sections
        ]
        section_response = self._request(
            "POST",
            "client_document_sections",
            json_body=section_payloads,
        )
        if section_response.status_code not in (200, 201):
            raise bad_request("CLIENT_DOCUMENT_SECTIONS_CREATE_FAILED", section_response.text)
        return _present_client_document(document, section_count=len(sections))

    def list_client_documents(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        verify_owner: bool = True,
    ) -> list[dict[str, Any]]:
        if verify_owner:
            self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        response = self._request(
            "GET",
            "client_documents",
            params={
                "select": "*",
                "opportunity_id": f"eq.{opportunity_id}",
                "order": "created_at.asc",
            },
        )
        if response.status_code != 200:
            raise bad_request("CLIENT_DOCUMENT_LIST_FAILED", response.text)
        rows = [_normalize_client_document(row) for row in response.json()]
        presented: list[dict[str, Any]] = []
        for row in rows:
            section_response = self._request(
                "GET",
                "client_document_sections",
                params={
                    "select": "section_index",
                    "client_document_id": f"eq.{row['id']}",
                },
            )
            if section_response.status_code != 200:
                raise bad_request("CLIENT_DOCUMENT_SECTIONS_LIST_FAILED", section_response.text)
            presented.append(
                _present_client_document(
                    row,
                    section_count=len(section_response.json()),
                )
            )
        return presented

    def list_client_document_sources(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        documents = self.list_client_documents(
            opportunity_id=opportunity_id,
            user_id=user_id,
        )
        sources: list[dict[str, Any]] = []
        for document in documents:
            response = self._request(
                "GET",
                "client_document_sections",
                params={
                    "select": "section_index,content,metadata",
                    "client_document_id": f"eq.{document['id']}",
                    "order": "section_index.asc",
                },
            )
            if response.status_code != 200:
                raise bad_request("CLIENT_DOCUMENT_SECTIONS_LIST_FAILED", response.text)
            sources.append(
                {
                    "id": document["id"],
                    "file_name": document["file_name"],
                    "document_key": document["document_key"],
                    "processing_status": document["processing_status"],
                    "sections": response.json(),
                }
            )
        return sources

    def get_client_document(
        self,
        *,
        opportunity_id: UUID,
        document_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        response = self._request(
            "GET",
            "client_documents",
            params={
                "select": "*",
                "id": f"eq.{document_id}",
                "opportunity_id": f"eq.{opportunity_id}",
            },
        )
        if response.status_code != 200 or not response.json():
            raise not_found("CLIENT_DOCUMENT_NOT_FOUND", f"Client document {document_id} was not found")
        row = _normalize_client_document(response.json()[0])
        section_response = self._request(
            "GET",
            "client_document_sections",
            params={
                "select": "section_index",
                "client_document_id": f"eq.{document_id}",
            },
        )
        if section_response.status_code != 200:
            raise bad_request("CLIENT_DOCUMENT_SECTIONS_LIST_FAILED", section_response.text)
        return _present_client_document(row, section_count=len(section_response.json()))

    def delete_client_document(
        self,
        *,
        opportunity_id: UUID,
        document_id: UUID,
        user_id: UUID,
    ) -> None:
        response = self._request(
            "GET",
            "client_documents",
            params={
                "select": "storage_path",
                "id": f"eq.{document_id}",
                "opportunity_id": f"eq.{opportunity_id}",
            },
        )
        if response.status_code != 200 or not response.json():
            raise not_found("CLIENT_DOCUMENT_NOT_FOUND", f"Client document {document_id} was not found")
        storage_path = str(response.json()[0].get("storage_path") or "").strip()
        delete_response = self._request(
            "DELETE",
            "client_documents",
            params={
                "id": f"eq.{document_id}",
                "opportunity_id": f"eq.{opportunity_id}",
            },
        )
        if delete_response.status_code == 204:
            self._delete_client_document_content(storage_path=storage_path)
            return
        if delete_response.status_code != 200 or not delete_response.json():
            raise not_found("CLIENT_DOCUMENT_NOT_FOUND", f"Client document {document_id} was not found")
        self._delete_client_document_content(storage_path=storage_path)

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
        latest = self._request(
            "GET",
            "framework_versions",
            params={
                "select": "id,version_number",
                "opportunity_id": f"eq.{opportunity_id}",
                "order": "version_number.desc",
                "limit": "1",
            },
        )
        version_number = 1
        previous_version_id = None
        if latest.status_code == 200 and latest.json():
            version_number = int(latest.json()[0]["version_number"]) + 1
            previous_version_id = str(latest.json()[0]["id"])

        persisted_json = copy.deepcopy(framework_json)
        persisted_json["opportunity_id"] = str(opportunity_id)
        persisted_json["version"] = version_number
        persisted_json["previous_version_id"] = previous_version_id
        persisted_json["status"] = status
        from packages.contracts.schema_consumer import validate_framework_object

        validate_framework_object(persisted_json)

        payload = {
            "opportunity_id": str(opportunity_id),
            "version_number": version_number,
            "status": status,
            "framework_json": persisted_json,
            "created_by": str(user_id),
        }
        if framework_version_id is not None:
            payload["id"] = str(framework_version_id)
        response = self._request("POST", "framework_versions", json_body=payload)
        if response.status_code not in (200, 201):
            if framework_version_id is not None:
                try:
                    existing = self.get_framework_version(
                        framework_version_id=framework_version_id,
                        user_id=user_id,
                    )
                except HTTPException:
                    existing = None
                if (
                    existing is not None
                    and existing["opportunity_id"] == opportunity_id
                    and existing["status"] == status
                    and existing["framework_json"] == persisted_json
                ):
                    return existing
            raise bad_request("FRAMEWORK_CREATE_FAILED", response.text)
        return _normalize_framework(response.json()[0])

    def get_latest_framework(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        _ = user_id
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        response = self._request(
            "GET",
            "framework_versions",
            params={
                "select": "*",
                "opportunity_id": f"eq.{opportunity_id}",
                "order": "version_number.desc",
                "limit": "1",
            },
        )
        if response.status_code != 200 or not response.json():
            raise not_found(
                "FRAMEWORK_NOT_FOUND",
                f"No framework version exists for opportunity {opportunity_id}",
            )
        return _normalize_framework(response.json()[0])

    def get_framework_version(
        self,
        *,
        framework_version_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "framework_versions",
            params={"select": "*", "id": f"eq.{framework_version_id}", "limit": "1"},
        )
        if response.status_code != 200 or not response.json():
            raise not_found(
                "FRAMEWORK_NOT_FOUND",
                f"Framework version {framework_version_id} was not found",
            )
        row = _normalize_framework(response.json()[0])
        self.get_opportunity(opportunity_id=row["opportunity_id"], user_id=user_id)
        return row

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
        latest = self._request(
            "GET",
            "discovery_paper_versions",
            params={
                "select": "version_number",
                "opportunity_id": f"eq.{opportunity_id}",
                "order": "version_number.desc",
                "limit": "1",
            },
        )
        if latest.status_code != 200:
            raise bad_request("DISCOVERY_PAPER_VERSION_CREATE_FAILED", latest.text)
        version_number = int(latest.json()[0]["version_number"]) + 1 if latest.json() else 1
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        payload = {
            "opportunity_id": str(opportunity_id),
            "version_number": version_number,
            "document_id": str(document_id),
            "status": "draft",
            "paper_json": copy.deepcopy(paper_json),
            "created_by": str(user_id),
            "created_at": now,
            "updated_at": now,
            "approved_at": None,
        }
        if version_id is not None:
            payload["id"] = str(version_id)
        response = self._request("POST", "discovery_paper_versions", json_body=payload)
        if response.status_code not in (200, 201) or not response.json():
            raise bad_request("DISCOVERY_PAPER_VERSION_CREATE_FAILED", response.text)
        return _normalize_discovery_paper_version(response.json()[0])

    def get_discovery_paper_version(
        self,
        *,
        version_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "discovery_paper_versions",
            params={"select": "*", "id": f"eq.{version_id}", "limit": "1"},
        )
        if response.status_code != 200 or not response.json():
            raise not_found(
                "DISCOVERY_PAPER_VERSION_NOT_FOUND",
                f"Discovery Paper version {version_id} was not found",
            )
        row = _normalize_discovery_paper_version(response.json()[0])
        self.get_opportunity(opportunity_id=row["opportunity_id"], user_id=user_id)
        return row

    def list_discovery_paper_versions(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        response = self._request(
            "GET",
            "discovery_paper_versions",
            params={
                "select": "*",
                "opportunity_id": f"eq.{opportunity_id}",
                "order": "version_number.asc",
            },
        )
        if response.status_code != 200:
            raise bad_request("DISCOVERY_PAPER_VERSION_LIST_FAILED", response.text)
        return [_normalize_discovery_paper_version(row) for row in response.json()]

    def get_latest_approved_discovery_paper(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any] | None:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        response = self._request(
            "GET",
            "discovery_paper_versions",
            params={
                "select": "*",
                "opportunity_id": f"eq.{opportunity_id}",
                "status": "eq.approved",
                "order": "approved_at.desc,version_number.desc",
                "limit": "1",
            },
        )
        if response.status_code != 200:
            raise bad_request("DISCOVERY_PAPER_VERSION_LIST_FAILED", response.text)
        if not response.json():
            return None
        return _normalize_discovery_paper_version(response.json()[0])

    def get_draft_discovery_paper_version(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        document_id: UUID,
    ) -> dict[str, Any] | None:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        response = self._request(
            "GET",
            "discovery_paper_versions",
            params={
                "select": "*",
                "opportunity_id": f"eq.{opportunity_id}",
                "document_id": f"eq.{document_id}",
                "status": "eq.draft",
                "order": "version_number.desc",
                "limit": "1",
            },
        )
        if response.status_code != 200:
            raise bad_request("DISCOVERY_PAPER_VERSION_LIST_FAILED", response.text)
        if not response.json():
            return None
        return _normalize_discovery_paper_version(response.json()[0])

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
        current = self.get_discovery_paper_version(version_id=version_id, user_id=user_id)
        if current["status"] == "approved":
            raise conflict(
                "DISCOVERY_PAPER_VERSION_IMMUTABLE",
                "Approved Discovery Paper versions are immutable",
            )
        if status not in {"draft", "approved"} or current["status"] != "draft":
            raise conflict(
                "DISCOVERY_PAPER_VERSION_IMMUTABLE",
                "Approved Discovery Paper versions cannot return to draft",
            )
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        payload: dict[str, Any] = {
            "paper_json": copy.deepcopy(paper_json),
            "status": status,
            "updated_at": now,
        }
        if status == "approved":
            payload["approved_at"] = now
        response = self._request(
            "PATCH",
            "discovery_paper_versions",
            params={"id": f"eq.{version_id}", "status": "eq.draft"},
            json_body=payload,
        )
        if response.status_code not in (200, 204) or not response.json():
            raise conflict(
                "DISCOVERY_PAPER_VERSION_IMMUTABLE",
                "Approved Discovery Paper versions are immutable",
            )
        return _normalize_discovery_paper_version(response.json()[0])

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
        updated["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        change_log = updated.setdefault("change_log", [])
        change_log.append("Manual edit via framework review UI")

        response = self._request(
            "PATCH",
            "framework_versions",
            params={"id": f"eq.{row['id']}"},
            json_body={"framework_json": updated},
        )
        if response.status_code not in (200, 204) or not response.json():
            raise bad_request("FRAMEWORK_UPDATE_FAILED", response.text)
        return _normalize_framework(response.json()[0])

    def confirm_framework(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        framework_version_id: UUID | None,
        confirmed_framework_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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

        framework_json = copy.deepcopy(
            confirmed_framework_json if confirmed_framework_json is not None else row["framework_json"]
        )
        framework_json["status"] = "confirmed"
        framework_json["confirmed_by"] = str(user_id)
        framework_json["confirmed_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        response = self._request(
            "PATCH",
            "framework_versions",
            params={"id": f"eq.{row['id']}"},
            json_body={"status": "confirmed", "framework_json": framework_json},
        )
        if response.status_code not in (200, 204) or not response.json():
            raise bad_request("FRAMEWORK_CONFIRM_FAILED", response.text)
        return _normalize_framework(response.json()[0])

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
        response = self._request(
            "PATCH",
            "framework_versions",
            params={"id": f"eq.{row['id']}"},
            json_body={"status": "in_review", "framework_json": framework_json},
        )
        if response.status_code not in (200, 204) or not response.json():
            raise bad_request("FRAMEWORK_REOPEN_FAILED", response.text)
        return _normalize_framework(response.json()[0])

    def regenerate_chapter(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
        chapter_id: str,
    ) -> dict[str, Any]:
        row = self.get_latest_framework(opportunity_id=opportunity_id, user_id=user_id)
        require_reviewable_framework(row["status"], action="regenerate")

        framework_json = copy.deepcopy(row["framework_json"])
        chapters = framework_json.get("chapters", [])
        if not any(str(chapter.get("chapter_id")) == chapter_id for chapter in chapters):
            raise bad_request("INVALID_CHAPTER_ID", f"Chapter {chapter_id} was not found")

        change_log = framework_json.setdefault("change_log", [])
        change_log.append(f"Regenerated chapter {chapter_id}")
        response = self._request(
            "PATCH",
            "framework_versions",
            params={"id": f"eq.{row['id']}"},
            json_body={"framework_json": framework_json},
        )
        if response.status_code not in (200, 204) or not response.json():
            raise bad_request("FRAMEWORK_REGENERATE_FAILED", response.text)
        return _normalize_framework(response.json()[0])

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
        source = self.get_framework_version(
            framework_version_id=source_framework_version_id,
            user_id=user_id,
        )
        latest = self.get_latest_framework(opportunity_id=opportunity_id, user_id=user_id)
        if latest["id"] != source["id"]:
            raise conflict("FRAMEWORK_VERSION_CONFLICT", "The Framework changed before transition completed")
        from app.services.framework_versioning import framework_source_revision

        if framework_source_revision(source) != source_revision:
            raise conflict("FRAMEWORK_VERSION_CONFLICT", "The source Framework changed during transition")
        payload = {
            "p_source_framework_version_id": str(source_framework_version_id),
            "p_framework_version_id": str(framework_version_id),
            "p_opportunity_id": str(opportunity_id),
            "p_expected_status": str(source["status"]),
            "p_expected_source_json": source["framework_json"],
            "p_transition": transition,
            "p_successor_status": status,
            "p_successor_json": framework_json,
        }
        response = self._service_role_request(
            "POST",
            "rpc/append_framework_version_transition",
            json_body=payload,
        )
        if response.status_code not in (200, 201):
            raise conflict("FRAMEWORK_VERSION_CONFLICT", "The Framework successor could not be created")
        return _normalize_framework(response.json()[0])

    def list_job_knowledge_models(
        self,
        *,
        job_id: UUID,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        job = self.get_generation_job(job_id)
        if job is None or job["opportunity_id"] != opportunity_id:
            raise bad_request("KNOWLEDGE_CHECKPOINT_INVALID", "Generation job does not belong to this opportunity")
        response = self._request(
            "GET",
            "knowledge_model_checkpoints",
            params={
                "select": "*",
                "generation_job_id": f"eq.{job_id}",
                "opportunity_id": f"eq.{opportunity_id}",
            },
        )
        if response.status_code != 200:
            raise bad_request("KNOWLEDGE_CHECKPOINT_READ_FAILED", response.text)
        return list(response.json() or [])

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
        job = self.get_generation_job(job_id)
        if job is None or job["opportunity_id"] != opportunity_id:
            raise bad_request("KNOWLEDGE_CHECKPOINT_INVALID", "Generation job does not belong to this opportunity")
        self.get_transcript(
            opportunity_id=opportunity_id,
            transcript_id=transcript_id,
            user_id=user_id,
        )
        payload = {
            "generation_job_id": str(job_id),
            "transcript_id": str(transcript_id),
            "opportunity_id": str(opportunity_id),
            "conversation_id": conversation_id,
            "knowledge_model_json": knowledge_model_json,
            "schema_version": schema_version,
            "prompt_version": prompt_version,
        }
        response = self._request(
            "POST",
            "knowledge_model_checkpoints",
            params={"on_conflict": "generation_job_id,transcript_id"},
            json_body=payload,
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        if response.status_code not in (200, 201) or not response.json():
            raise bad_request("KNOWLEDGE_CHECKPOINT_WRITE_FAILED", response.text)
        return dict(response.json()[0])

    def generate_framework_stub(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        payload = load_framework_stub_template(opportunity_id)
        apply_persisted_intake_to_framework(
            payload,
            self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id),
        )
        return self.create_framework_version(
            opportunity_id=opportunity_id,
            user_id=user_id,
            framework_json=payload,
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
        payload = {
            "framework_version_id": str(framework_version_id),
            "plan_json": plan_json,
        }
        if presentation_plan_id is not None:
            payload["id"] = str(presentation_plan_id)
        response = self._request("POST", "presentation_plans", json_body=payload)
        if response.status_code not in (200, 201):
            raise bad_request("PRESENTATION_PLAN_CREATE_FAILED", response.text)
        return _normalize_presentation_plan(response.json()[0])

    def get_presentation_plan(
        self,
        *,
        presentation_plan_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "presentation_plans",
            params={"select": "*", "id": f"eq.{presentation_plan_id}", "limit": "1"},
        )
        if response.status_code != 200 or not response.json():
            raise not_found(
                "PRESENTATION_PLAN_NOT_FOUND",
                f"Presentation plan {presentation_plan_id} was not found",
            )
        row = _normalize_presentation_plan(response.json()[0])
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
        _ = user_id
        self.get_framework_version(
            framework_version_id=framework_version_id,
            user_id=user_id,
        )
        response = self._request(
            "GET",
            "presentation_plans",
            params={
                "select": "*",
                "framework_version_id": f"eq.{framework_version_id}",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        if response.status_code != 200 or not response.json():
            return None
        return _normalize_presentation_plan(response.json()[0])

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
        _ = user_id
        self.get_presentation_plan(
            presentation_plan_id=presentation_plan_id,
            user_id=user_id,
        )
        payload = {
            "presentation_plan_id": str(presentation_plan_id),
            "name": name,
            "status": "draft",
        }
        response = self._request("POST", "presentations", json_body=payload)
        if response.status_code not in (200, 201):
            raise bad_request("PRESENTATION_CREATE_FAILED", response.text)
        return _normalize_presentation(response.json()[0])

    def retarget_presentation_plan(
        self,
        *,
        presentation_id: UUID,
        presentation_plan_id: UUID,
        user_id: UUID,
        name: str | None = None,
    ) -> dict[str, Any]:
        self.get_presentation(presentation_id=presentation_id, user_id=user_id)
        self.get_presentation_plan(
            presentation_plan_id=presentation_plan_id,
            user_id=user_id,
        )
        payload: dict[str, Any] = {"presentation_plan_id": str(presentation_plan_id)}
        if name:
            payload["name"] = name
        response = self._request(
            "PATCH",
            "presentations",
            params={"id": f"eq.{presentation_id}"},
            json_body=payload,
        )
        if response.status_code not in (200, 204) or not response.json():
            raise bad_request("PRESENTATION_UPDATE_FAILED", response.text)
        return _normalize_presentation(response.json()[0])

    def list_presentations(self, *, user_id: UUID) -> list[dict[str, Any]]:
        _ = user_id
        response = self._request(
            "GET",
            "presentations",
            params={"select": "*", "order": "created_at.desc"},
        )
        if response.status_code != 200:
            raise bad_request("PRESENTATION_LIST_FAILED", response.text)
        return [_normalize_presentation(row) for row in response.json()]

    def get_presentation(
        self,
        *,
        presentation_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "presentations",
            params={"select": "*", "id": f"eq.{presentation_id}", "limit": "1"},
        )
        if response.status_code != 200 or not response.json():
            raise not_found(
                "PRESENTATION_NOT_FOUND",
                f"Presentation {presentation_id} was not found",
            )
        row = _normalize_presentation(response.json()[0])
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
        self.get_presentation(presentation_id=presentation_id, user_id=user_id)
        latest = self._request(
            "GET",
            "presentation_versions",
            params={
                "select": "version_number",
                "presentation_id": f"eq.{presentation_id}",
                "order": "version_number.desc",
                "limit": "1",
            },
        )
        version_number = 1
        if latest.status_code == 200 and latest.json():
            version_number = int(latest.json()[0]["version_number"]) + 1

        version_payload = {
            "presentation_id": str(presentation_id),
            "version_number": version_number,
            "slides_json": [],
            "status": "generating",
            "journey_stage": journey_stage,
            "prior_stage_presentation_version_id": (
                str(prior_stage_presentation_version_id)
                if prior_stage_presentation_version_id
                else None
            ),
        }
        if generation_source_manifest is not None:
            version_payload["generation_source_manifest"] = generation_source_manifest
        version_response = self._request(
            "POST",
            "presentation_versions",
            json_body=version_payload,
        )
        if version_response.status_code not in (200, 201):
            raise bad_request("PRESENTATION_VERSION_CREATE_FAILED", version_response.text)
        version_row = _normalize_presentation_version(version_response.json()[0])

        presentation = self.get_presentation(presentation_id=presentation_id, user_id=user_id)
        plan = self.get_presentation_plan(
            presentation_plan_id=presentation["presentation_plan_id"],
            user_id=user_id,
        )
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
            slide_payload = {
                "presentation_version_id": str(version_row["id"]),
                "slide_index": index - 1,
                "layout_id": persisted_slide_spec["layoutId"],
                "slide_spec": persisted_slide_spec,
                "source_chapter_ids": copy.deepcopy(
                    persisted_slide_spec["sourceChapterIds"]
                ),
            }
            slide_response = self._request("POST", "slides", json_body=slide_payload)
            if slide_response.status_code not in (200, 201):
                raise bad_request("SLIDE_CREATE_FAILED", slide_response.text)
            slide_specs.append(copy.deepcopy(persisted_slide_spec))

        patch_response = self._request(
            "PATCH",
            "presentation_versions",
            params={"id": f"eq.{version_row['id']}"},
            json_body={"slides_json": slide_specs, "status": "generating"},
        )
        if patch_response.status_code not in (200, 204) or not patch_response.json():
            raise bad_request("PRESENTATION_VERSION_UPDATE_FAILED", patch_response.text)
        version_row = _normalize_presentation_version(patch_response.json()[0])
        # A Master Presentation is never stood in for by the fixture deck: it becomes ready only
        # once its real PPTX, PDF and previews exist.
        if settings.RENDERER_EXECUTION_MODE == "fixture" and plan_json.get("deck_kind") not in ("master_v1", "master_v2"):
            assets = materialize_fixture_deck_assets(
                version_id=version_row["id"],
                slide_count=len(slide_specs),
            )
            version_row = self.update_presentation_version_assets(
                presentation_version_id=version_row["id"],
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
        asset_patch = self._request(
            "PATCH",
            "presentation_versions",
            params={"id": f"eq.{presentation_version_id}"},
            json_body={
                "pptx_storage_path": assets["pptx_storage_path"],
                "pdf_storage_path": assets["pdf_storage_path"],
                "status": status,
            },
        )
        if asset_patch.status_code not in (200, 204) or not asset_patch.json():
            raise bad_request("PRESENTATION_VERSION_UPDATE_FAILED", asset_patch.text)
        version_row = _normalize_presentation_version(asset_patch.json()[0])
        version_row["preview_image_paths"] = assets["preview_image_paths"]
        return version_row

    def get_latest_presentation_version(
        self,
        *,
        presentation_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        _ = user_id
        self.get_presentation(presentation_id=presentation_id, user_id=user_id)
        response = self._request(
            "GET",
            "presentation_versions",
            params={
                "select": "*",
                "presentation_id": f"eq.{presentation_id}",
                "order": "version_number.desc",
                "limit": "1",
            },
        )
        if response.status_code != 200 or not response.json():
            raise not_found(
                "PRESENTATION_VERSION_NOT_FOUND",
                f"No presentation version exists for presentation {presentation_id}",
            )
        return _normalize_presentation_version(response.json()[0])

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
        response = self._request(
            "GET",
            "slides",
            params={
                "select": "*",
                "id": f"eq.{slide_id}",
                "presentation_version_id": f"eq.{version['id']}",
                "limit": "1",
            },
        )
        if response.status_code != 200 or not response.json():
            raise not_found("SLIDE_NOT_FOUND", f"Slide {slide_id} was not found")
        return _normalize_slide(response.json()[0])

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
        old_slides = self.list_slides(presentation_id=presentation_id, user_id=user_id)
        presentation = self.get_presentation(presentation_id=presentation_id, user_id=user_id)
        plan = self.get_presentation_plan(
            presentation_plan_id=presentation["presentation_plan_id"],
            user_id=user_id,
        )
        framework = self.get_framework_version(
            framework_version_id=plan["framework_version_id"],
            user_id=user_id,
        )
        edited_payload = {
            "presentation_id": str(presentation_id),
            "version_number": int(previous["version_number"]) + 1,
            "slides_json": [],
            "status": "generating",
            "journey_stage": previous.get("journey_stage"),
            "prior_stage_presentation_version_id": (
                str(previous["prior_stage_presentation_version_id"])
                if previous.get("prior_stage_presentation_version_id")
                else None
            ),
        }
        if previous.get("generation_source_manifest") is not None:
            edited_payload["generation_source_manifest"] = previous["generation_source_manifest"]
        version_response = self._request(
            "POST",
            "presentation_versions",
            json_body=edited_payload,
        )
        if version_response.status_code not in (200, 201):
            raise bad_request("PRESENTATION_VERSION_CREATE_FAILED", version_response.text)
        version = _normalize_presentation_version(version_response.json()[0])

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
            slide_response = self._request(
                "POST",
                "slides",
                json_body={
                    "presentation_version_id": str(version["id"]),
                    "slide_index": old_slide["slide_index"],
                    "layout_id": next_layout,
                    "slide_spec": spec,
                    "source_chapter_ids": spec["sourceChapterIds"],
                },
            )
            if slide_response.status_code not in (200, 201):
                raise bad_request("SLIDE_CREATE_FAILED", slide_response.text)
            new_slide = _normalize_slide(slide_response.json()[0])
            specs.append(spec)
            if old_slide["id"] == target["id"]:
                edited = new_slide

        patch = self._request(
            "PATCH",
            "presentation_versions",
            params={"id": f"eq.{version['id']}"},
            json_body={"slides_json": specs},
        )
        if patch.status_code not in (200, 204) or not patch.json():
            raise bad_request("PRESENTATION_VERSION_UPDATE_FAILED", patch.text)
        if settings.RENDERER_EXECUTION_MODE == "fixture":
            assets = materialize_fixture_deck_assets(
                version_id=version["id"],
                slide_count=len(specs),
            )
            self.update_presentation_version_assets(
                presentation_version_id=version["id"],
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
        response = self._request(
            "GET",
            "slides",
            params={
                "select": "*",
                "presentation_version_id": f"eq.{version['id']}",
                "order": "slide_index.asc",
            },
        )
        if response.status_code != 200:
            raise bad_request("SLIDE_LIST_FAILED", response.text)
        return [_normalize_slide(row) for row in response.json()]

    def list_presentations_for_opportunity(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        # Workers use a service-role credential, so RLS cannot scope
        # list_presentations() for them. Resolve the requested opportunity's
        # lineage explicitly instead of walking presentations owned by every
        # user and failing on the first foreign framework.
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        framework_response = self._request(
            "GET",
            "framework_versions",
            params={
                "select": "id",
                "opportunity_id": f"eq.{opportunity_id}",
                "created_by": f"eq.{user_id}",
            },
        )
        if framework_response.status_code != 200:
            raise bad_request("FRAMEWORK_LIST_FAILED", framework_response.text)
        framework_ids = [str(row["id"]) for row in framework_response.json()]
        if not framework_ids:
            return []

        plan_response = self._request(
            "GET",
            "presentation_plans",
            params={
                "select": "id",
                "framework_version_id": f"in.({','.join(framework_ids)})",
            },
        )
        if plan_response.status_code != 200:
            raise bad_request("PRESENTATION_PLAN_LIST_FAILED", plan_response.text)
        plan_ids = [str(row["id"]) for row in plan_response.json()]
        if not plan_ids:
            return []

        response = self._request(
            "GET",
            "presentations",
            params={
                "select": "*",
                "presentation_plan_id": f"in.({','.join(plan_ids)})",
                "order": "created_at.desc",
            },
        )
        if response.status_code != 200:
            raise bad_request("PRESENTATION_LIST_FAILED", response.text)
        return [_normalize_presentation(row) for row in response.json()]

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
        response = self._request(
            "GET",
            "presentation_versions",
            params={
                "select": "*",
                "id": f"eq.{presentation_version_id}",
                "limit": "1",
            },
        )
        if response.status_code != 200 or not response.json():
            raise not_found(
                "PRESENTATION_VERSION_NOT_FOUND",
                f"Presentation version {presentation_version_id} was not found",
            )
        row = _normalize_presentation_version(response.json()[0])
        self.get_presentation(presentation_id=row["presentation_id"], user_id=user_id)
        return row

    def list_presentation_versions_for_opportunity(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        versions: list[dict[str, Any]] = []
        for presentation in self.list_presentations_for_opportunity(
            opportunity_id=opportunity_id,
            user_id=user_id,
        ):
            response = self._request(
                "GET",
                "presentation_versions",
                params={
                    "select": "*",
                    "presentation_id": f"eq.{presentation['id']}",
                    "order": "created_at.desc",
                },
            )
            if response.status_code != 200:
                raise bad_request("PRESENTATION_VERSION_LIST_FAILED", response.text)
            versions.extend(
                _normalize_presentation_version(row) for row in response.json()
            )
        return versions

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
        version_id = version["id"]
        slide_count = max(len(version.get("slides_json") or []), 1)
        if not version.get("pptx_storage_path"):
            version["pptx_storage_path"] = str(resolve_pptx_path(version_id=version_id).resolve())
        if not version.get("pdf_storage_path"):
            version["pdf_storage_path"] = str(resolve_pdf_path(version_id=version_id).resolve())
        version["preview_image_paths"] = list_preview_image_paths(
            version_id=version_id,
            stored_paths=list(version.get("preview_image_paths") or []),
            slide_count=slide_count,
        )
        return version

    def get_filing_record(self, idempotency_key: str) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            "filed_artifacts",
            params={
                "select": "*",
                "idempotency_key": f"eq.{idempotency_key}",
                "limit": "1",
            },
        )
        if response.status_code != 200:
            raise bad_request("FILING_RECORD_READ_FAILED", response.text)
        rows = response.json()
        return dict(rows[0]) if rows else None

    def save_filing_record(
        self,
        idempotency_key: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        body = _json_safe_value({**record, "idempotency_key": idempotency_key})
        response = _request_with_retry(
            "POST",
            f"{self._base_url}/rest/v1/filed_artifacts",
            headers={
                **self._headers,
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
            params={"on_conflict": "idempotency_key"},
            json=body,
        )
        if response.status_code not in (200, 201) or not response.json():
            raise bad_request("FILING_RECORD_WRITE_FAILED", response.text)
        return dict(response.json()[0])

    def list_filed_artifacts(
        self,
        *,
        opportunity_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        self.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        response = self._request(
            "GET",
            "filed_artifacts",
            params={
                "select": "*",
                "opportunity_id": f"eq.{opportunity_id}",
                "order": "updated_at.desc",
            },
        )
        if response.status_code != 200:
            raise bad_request("FILING_RECORD_READ_FAILED", response.text)
        return [dict(row) for row in response.json()]

    def list_user_filed_artifacts(self, *, user_id: UUID) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            "filed_artifacts",
            params={
                "select": "*",
                "status": "eq.filed",
                "order": "filed_at.desc.nullslast,updated_at.desc",
            },
        )
        if response.status_code != 200:
            raise bad_request("FILING_RECORD_READ_FAILED", response.text)
        opportunities: dict[str, dict[str, Any]] = {}
        rows: list[dict[str, Any]] = []
        for artifact in response.json():
            opportunity_id = UUID(str(artifact["opportunity_id"]))
            key = str(opportunity_id)
            if key not in opportunities:
                opportunities[key] = self.get_opportunity(
                    opportunity_id=opportunity_id,
                    user_id=user_id,
                )
            opportunity = opportunities[key]
            rows.append(
                {
                    **artifact,
                    "client_name": opportunity["client_name"],
                    "opportunity_name": opportunity["opportunity_name"],
                }
            )
        return rows

    def get_user_filed_artifact(
        self,
        *,
        artifact_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "filed_artifacts",
            params={
                "select": "*",
                "id": f"eq.{artifact_id}",
                "status": "eq.filed",
                "limit": "1",
            },
        )
        if response.status_code != 200 or not response.json():
            raise not_found("FILED_ARTIFACT_NOT_FOUND", "Filed artifact was not found")
        row = dict(response.json()[0])
        opportunity = self.get_opportunity(
            opportunity_id=UUID(str(row["opportunity_id"])),
            user_id=user_id,
        )
        return {
            **row,
            "client_name": opportunity["client_name"],
            "opportunity_name": opportunity["opportunity_name"],
        }

    def append_llm_call(self, record: Any) -> dict[str, Any]:
        from app.services.data.memory_store import _llm_call_row

        row = _llm_call_row(record)
        payload: dict[str, Any] = {
            "request_id": row["request_id"],
            "stage": row["stage"],
            "provider": row["provider"],
            "model": row["model"],
            "prompt_version": row["prompt_version"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "total_tokens": row["total_tokens"],
            "latency_ms": row["latency_ms"],
            "retry_count": row["retry_count"],
            "status": row["status"],
            "error_category": row["error_category"],
            "estimated_cost_eur": row["estimated_cost_eur"],
        }
        if row["job_id"] is not None:
            payload["job_id"] = str(row["job_id"])
        if row["opportunity_id"] is not None:
            payload["opportunity_id"] = str(row["opportunity_id"])
        response = self._request("POST", "llm_calls", json_body=payload)
        if response.status_code not in (200, 201):
            raise bad_request("LLM_CALL_LOG_FAILED", response.text)
        return response.json()[0]

    def get_llm_calls_for_job(self, job_id: str) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            "llm_calls",
            params={
                "select": "*",
                "job_id": f"eq.{job_id}",
                "order": "created_at.asc",
            },
        )
        if response.status_code != 200:
            raise bad_request("LLM_CALL_LIST_FAILED", response.text)
        return list(response.json())

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
        payload = {
            "actor_id": str(actor_id),
            "action": action,
            "object_type": object_type,
            "object_id": str(object_id),
            "document_id": document_id or str(object_id),
            "actor_email": actor_email,
        }
        response = self._request("POST", "audit_log", json_body=payload)
        if response.status_code not in (200, 201):
            raise bad_request("AUDIT_LOG_WRITE_FAILED", response.text)
        row = response.json()[0]
        return {
            "id": UUID(str(row["id"])),
            "actor_id": UUID(str(row["actor_id"])),
            "actor_email": row.get("actor_email"),
            "action": row["action"],
            "object_type": row["object_type"],
            "object_id": UUID(str(row["object_id"])),
            "document_id": row.get("document_id") or str(row["object_id"]),
            "timestamp": _parse_timestamp(row["timestamp"]),
        }

    def list_audit_logs(
        self,
        *,
        actor_id: UUID | None = None,
        object_id: UUID | None = None,
        document_id: str | None = None,
        privileged: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "select": "*",
            "order": "timestamp.desc",
        }
        if actor_id is not None and not privileged:
            params["actor_id"] = f"eq.{actor_id}"
        if object_id is not None:
            params["object_id"] = f"eq.{object_id}"
        if document_id:
            params["document_id"] = f"eq.{document_id}"
        if privileged:
            response = self._service_role_request("GET", "audit_log", params=params)
        else:
            response = self._request("GET", "audit_log", params=params)
        if response.status_code != 200:
            raise bad_request("AUDIT_LOG_LIST_FAILED", response.text)
        rows = []
        for row in response.json():
            rows.append(
                {
                    "id": UUID(str(row["id"])),
                    "actor_id": UUID(str(row["actor_id"])),
                    "actor_email": row.get("actor_email"),
                    "action": row["action"],
                    "object_type": row["object_type"],
                    "object_id": UUID(str(row["object_id"])),
                    "document_id": row.get("document_id") or str(row["object_id"]),
                    "timestamp": _parse_timestamp(row["timestamp"]),
                }
            )
        return rows

    def get_or_create_user_role(self, *, user_id: UUID, email: str) -> dict[str, Any]:
        lookup = self._service_role_request(
            "GET",
            "user_roles",
            params={"select": "*", "user_id": f"eq.{user_id}"},
        )
        if lookup.status_code == 200 and lookup.json():
            row = lookup.json()[0]
            if email and row.get("email") != email:
                updated = self._service_role_request(
                    "PATCH",
                    "user_roles",
                    params={"user_id": f"eq.{user_id}"},
                    json_body={"email": email},
                )
                if updated.status_code in (200, 204) and updated.json():
                    row = updated.json()[0]
                else:
                    row["email"] = email
            return self._normalize_user_role(row)

        admins = self._service_role_request(
            "GET",
            "user_roles",
            params={"select": "user_id", "role": "eq.admin", "limit": "1"},
        )
        role = "admin"
        if admins.status_code == 200 and admins.json():
            role = settings.DEFAULT_EMPLOYEE_ROLE
        created = self._service_role_request(
            "POST",
            "user_roles",
            json_body={
                "user_id": str(user_id),
                "email": email,
                "role": role,
            },
        )
        if created.status_code not in (200, 201):
            raise bad_request("USER_ROLE_WRITE_FAILED", created.text)
        return self._normalize_user_role(created.json()[0])

    def set_user_role(
        self,
        *,
        user_id: UUID,
        email: str,
        role: str,
    ) -> dict[str, Any]:
        existing = self._service_role_request(
            "GET",
            "user_roles",
            params={"select": "*", "user_id": f"eq.{user_id}"},
        )
        payload = {
            "user_id": str(user_id),
            "email": email,
            "role": role,
        }
        if existing.status_code == 200 and existing.json():
            payload["email"] = email or existing.json()[0].get("email") or ""
            updated = self._service_role_request(
                "PATCH",
                "user_roles",
                params={"user_id": f"eq.{user_id}"},
                json_body={"email": payload["email"], "role": role},
            )
            if updated.status_code not in (200, 204):
                raise bad_request("USER_ROLE_WRITE_FAILED", updated.text)
            body = updated.json()
            if body:
                return self._normalize_user_role(body[0])
            return self._normalize_user_role({**existing.json()[0], **payload})
        created = self._service_role_request("POST", "user_roles", json_body=payload)
        if created.status_code not in (200, 201):
            raise bad_request("USER_ROLE_WRITE_FAILED", created.text)
        return self._normalize_user_role(created.json()[0])

    def list_user_roles(self) -> list[dict[str, Any]]:
        response = self._service_role_request(
            "GET",
            "user_roles",
            params={"select": "*", "order": "email.asc"},
        )
        if response.status_code != 200:
            raise bad_request("USER_ROLE_LIST_FAILED", response.text)
        return [self._normalize_user_role(row) for row in response.json()]

    def _normalize_user_role(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "user_id": UUID(str(row["user_id"])),
            "email": str(row.get("email") or ""),
            "role": str(row["role"]),
            "created_at": _parse_timestamp(row["created_at"]) if row.get("created_at") else None,
            "updated_at": _parse_timestamp(row["updated_at"]) if row.get("updated_at") else None,
        }

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
        payload = {
            "opportunity_id": str(opportunity_id),
            "presentation_version_id": str(presentation_version_id),
            "journey_stage": journey_stage,
            "provider": provider,
            "pipeline_stage": pipeline_stage,
            "decision": decision,
            "fields": list(fields),
            "attempt": int(attempt),
        }
        response = self._request("POST", "egress_audit", json_body=payload)
        if response.status_code not in (200, 201):
            raise bad_request("EGRESS_AUDIT_WRITE_FAILED", response.text)
        row = response.json()[0]
        return {
            "id": UUID(str(row["id"])),
            "opportunity_id": str(row["opportunity_id"]),
            "presentation_version_id": str(row["presentation_version_id"]),
            "journey_stage": row["journey_stage"],
            "provider": row["provider"],
            "pipeline_stage": row["pipeline_stage"],
            "decision": row["decision"],
            "fields": list(row.get("fields") or []),
            "attempt": int(row.get("attempt") or 1),
            "created_at": _parse_timestamp(row["created_at"]),
        }

    def list_egress_audits(
        self,
        *,
        opportunity_id: UUID | str | None = None,
        presentation_version_id: UUID | str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "select": "*",
            "order": "created_at.asc",
        }
        if opportunity_id is not None:
            params["opportunity_id"] = f"eq.{opportunity_id}"
        if presentation_version_id is not None:
            params["presentation_version_id"] = f"eq.{presentation_version_id}"
        response = self._request("GET", "egress_audit", params=params)
        if response.status_code != 200:
            raise bad_request("EGRESS_AUDIT_LIST_FAILED", response.text)
        return list(response.json())

    def ingest_approved_corpus(self, raw: dict[str, Any]) -> dict[str, Any]:
        from services.borek_rag.ingest import ingest_summary, plan_ingest

        plan = plan_ingest(raw)
        existing_response = self._request(
            "GET",
            "knowledge_corpus_versions",
            params={
                "corpus_key": f"eq.{plan.corpus_key}",
                "version": f"eq.{plan.version}",
                "select": "*",
                "limit": "1",
            },
        )
        if existing_response.status_code != 200:
            raise bad_request("KNOWLEDGE_INGEST_FAILED", existing_response.text)
        existing = existing_response.json()[0] if existing_response.json() else None
        replaced_existing = existing is not None
        version_id = str(existing["id"]) if existing is not None else str(uuid.uuid4())
        if existing is not None:
            self._delete_knowledge_version_children(version_id)
        retire_response = self._request(
            "PATCH",
            "knowledge_corpus_versions",
            params={
                "corpus_key": f"eq.{plan.corpus_key}",
                "status": "eq.approved",
                "version": f"neq.{plan.version}",
            },
            json_body={"status": "retired"},
        )
        if retire_response.status_code not in (200, 204):
            raise bad_request("KNOWLEDGE_INGEST_FAILED", retire_response.text)
        version_payload = {
            "id": version_id,
            "corpus_key": plan.corpus_key,
            "version": plan.version,
            "status": "approved",
            "owner": plan.owner,
            "approved_at": datetime.now(UTC).isoformat(),
        }
        if existing is not None:
            version_response = self._request(
                "PATCH",
                "knowledge_corpus_versions",
                params={"id": f"eq.{version_id}"},
                json_body=version_payload,
            )
        else:
            version_response = self._request(
                "POST",
                "knowledge_corpus_versions",
                json_body=version_payload,
            )
        if version_response.status_code not in (200, 201) or not version_response.json():
            raise bad_request("KNOWLEDGE_INGEST_FAILED", version_response.text)
        for document in plan.documents:
            document_id = str(uuid.uuid4())
            document_response = self._request(
                "POST",
                "knowledge_documents",
                json_body={
                    "id": document_id,
                    "corpus_version_id": version_id,
                    "document_key": document.document_key,
                    "document_type": document.document_type,
                    "source_uri": document.source_uri,
                    "source_version": document.source_version,
                    "classification": document.classification,
                    "effective_from": document.effective_from,
                    "effective_to": document.effective_to,
                },
            )
            if document_response.status_code not in (200, 201):
                raise bad_request("KNOWLEDGE_INGEST_FAILED", document_response.text)
            if not document.facts:
                continue
            facts_response = self._request(
                "POST",
                "knowledge_facts",
                json_body=[
                    {
                        "document_id": document_id,
                        "fact_key": fact.fact_key,
                        "kind": fact.kind,
                        "service_key": fact.service_key,
                        "query_key": fact.query_key,
                        "statement": fact.statement,
                        "payload": fact.payload,
                        "search_terms": list(fact.search_terms),
                    }
                    for fact in document.facts
                ],
            )
            if facts_response.status_code not in (200, 201):
                raise bad_request("KNOWLEDGE_INGEST_FAILED", facts_response.text)
        return ingest_summary(plan, replaced_existing=replaced_existing)

    def _delete_knowledge_version_children(self, version_id: str) -> None:
        documents_response = self._request(
            "GET",
            "knowledge_documents",
            params={"corpus_version_id": f"eq.{version_id}", "select": "id"},
        )
        if documents_response.status_code != 200:
            raise bad_request("KNOWLEDGE_INGEST_FAILED", documents_response.text)
        document_ids = [str(row["id"]) for row in documents_response.json()]
        if document_ids:
            facts_response = self._request(
                "DELETE",
                "knowledge_facts",
                params={"document_id": f"in.({','.join(document_ids)})"},
            )
            if facts_response.status_code not in (200, 204):
                raise bad_request("KNOWLEDGE_INGEST_FAILED", facts_response.text)
        documents_delete = self._request(
            "DELETE",
            "knowledge_documents",
            params={"corpus_version_id": f"eq.{version_id}"},
        )
        if documents_delete.status_code not in (200, 204):
            raise bad_request("KNOWLEDGE_INGEST_FAILED", documents_delete.text)

    def list_approved_knowledge_facts(self) -> list[dict[str, Any]]:
        versions_response = self._request(
            "GET",
            "knowledge_corpus_versions",
            params={"status": "eq.approved", "select": "*"},
        )
        if versions_response.status_code != 200:
            raise bad_request("KNOWLEDGE_READ_FAILED", versions_response.text)
        versions = {str(row["id"]): row for row in versions_response.json()}
        if not versions:
            return []
        documents_response = self._request(
            "GET",
            "knowledge_documents",
            params={
                "corpus_version_id": f"in.({','.join(versions)})",
                "classification": "in.(public,internal)",
                "select": "*",
            },
        )
        if documents_response.status_code != 200:
            raise bad_request("KNOWLEDGE_READ_FAILED", documents_response.text)
        documents = {str(row["id"]): row for row in documents_response.json()}
        if not documents:
            return []
        facts_response = self._request(
            "GET",
            "knowledge_facts",
            params={"document_id": f"in.({','.join(documents)})", "select": "*"},
        )
        if facts_response.status_code != 200:
            raise bad_request("KNOWLEDGE_READ_FAILED", facts_response.text)
        rows: list[dict[str, Any]] = []
        for fact in facts_response.json():
            document = documents.get(str(fact["document_id"]))
            if document is None:
                continue
            version = versions.get(str(document["corpus_version_id"]))
            if version is None:
                continue
            rows.append(
                {
                    "fact_key": fact["fact_key"],
                    "kind": fact["kind"],
                    "service_key": fact.get("service_key"),
                    "query_key": fact["query_key"],
                    "statement": fact["statement"],
                    "payload": fact.get("payload") or {},
                    "search_terms": list(fact.get("search_terms") or []),
                    "corpus_key": version["corpus_key"],
                    "corpus_version": version["version"],
                    "owner": version["owner"],
                    "schema_version": None,
                    "corpus_classification": None,
                    "document_key": document["document_key"],
                    "document_type": document["document_type"],
                    "document_version": document["source_version"],
                    "classification": document["classification"],
                    "effective_from": document.get("effective_from"),
                    "effective_to": document.get("effective_to"),
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
            "classification": first.get("classification") or "internal",
            "document_count": len({row["document_key"] for row in rows}),
            "fact_count": len(rows),
            "fact_kinds": sorted({row["kind"] for row in rows}),
        }


def validate_transcript_upload(file_name: str, mime_type: str | None) -> None:
    extension = Path(file_name).suffix.lower()
    if extension not in ALLOWED_TRANSCRIPT_EXTENSIONS:
        raise bad_request(
            "INVALID_TRANSCRIPT_FORMAT",
            f"Unsupported transcript extension {extension or '(none)'}",
        )
    # Browsers label the same file differently (charset parameters, text/srt, application/zip for
    # .docx, ...). The type only has to fit the extension; the content itself is validated on ingestion.
    if not transcript_mime_allowed(extension, mime_type):
        raise bad_request(
            "INVALID_TRANSCRIPT_FORMAT",
            f"The file type {mime_type} does not match a {extension} transcript. Upload a .txt, .vtt, .srt, or .docx file.",
        )
