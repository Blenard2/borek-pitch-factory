"""Discovery generation, draft edit, approval and version lookup.

New documents are AI Opportunity Analyses (schema 2.0, dynamic page count). Papers stored
with schema 1.0 (seven fixed pages) stay readable, editable and approvable as they are.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any
from uuid import UUID

from jsonschema import ValidationError

from app.config import settings
from app.services.api_errors import bad_request, conflict, not_found
from services.framework.discovery_analysis.edit import apply_edits
from services.framework.discovery_analysis.model import (
    DiscoveryAnalysisError,
    empty_discovery_analysis,
    is_v2,
    validate_discovery_analysis,
)
from services.framework.discovery_analysis.pipeline import (
    DiscoveryAnalysisStageError,
    finalize,
    generate_discovery_analysis,
)
from services.framework.discovery_paper import validate_discovery_paper
from services.language import normalize_language, stored_language

_SYSTEM_FIELDS = (
    "opportunity_id",
    "document_id",
    "generated_at",
    "latest_approved_version_id",
    "schema_version",
    "status",
)


def get_discovery_paper(store: Any, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    stored = opportunity.get("discovery_paper")
    if isinstance(stored, dict):
        return stored
    return empty_discovery_analysis(opportunity)


def generate_discovery_paper(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    provider: Any = None,
    optional_parts: dict[str, bool] | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Generate a new AI Opportunity Analysis. An approved version is never touched.

    BAP-13: ``language`` (``en``/``de``) is the content language; without it, the opportunity's
    language. An explicit choice becomes the opportunity's language, so Post Meeting follows it.
    Every generation is a new version: a German analysis never replaces an English one.
    """
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    language = normalize_language(language, default=stored_language(opportunity.get("language")))
    if opportunity.get("language") != language:
        opportunity = store.update_opportunity(opportunity_id=opportunity_id, user_id=user_id, updates={"language": language})
    approved = store.get_latest_approved_discovery_paper(
        opportunity_id=opportunity_id,
        user_id=user_id,
    )
    preserved_approval = str(approved["id"]) if approved is not None else None

    def persist(paper: dict[str, Any]) -> None:
        store.update_opportunity(
            opportunity_id=opportunity_id,
            user_id=user_id,
            updates={"discovery_paper": paper},
        )

    try:
        paper = generate_discovery_analysis(
            opportunity,
            persist=persist,
            complete=_live_complete() if settings.AI_EXECUTION_MODE == "live" else None,
            research_provider=provider,
            optional_parts=optional_parts,
            latest_approved_version_id=preserved_approval,
            language=language,
        )
    except DiscoveryAnalysisStageError as exc:
        raise bad_request("DISCOVERY_PAPER_GENERATION_FAILED", str(exc)) from exc
    if paper.get("status") == "ready":
        store.create_discovery_paper_version(
            opportunity_id=opportunity_id,
            user_id=user_id,
            document_id=UUID(str(paper["document_id"])),
            paper_json=paper,
        )
    return paper


def edit_discovery_paper(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    pages: list[dict[str, Any]] | None = None,
    edits: list[dict[str, Any]] | None = None,
    expected_document_id: str | None = None,
) -> tuple[dict[str, Any], UUID | None]:
    """Edit the working draft. An analysis is edited by logical section, a v1 paper by page.

    The approved version is immutable: the edit lands in the draft version of the document,
    and a new draft is created when the only version of the document is already approved.
    """
    paper = _stored_paper(store, opportunity_id=opportunity_id, user_id=user_id)
    if expected_document_id is not None and str(paper.get("document_id")) != expected_document_id:
        raise conflict(
            "DISCOVERY_PAPER_STALE",
            "The Discovery Paper changed before this edit was saved",
        )
    if is_v2(paper):
        _edit_analysis(paper, edits)
    else:
        _edit_pages(paper, pages)
    store.update_opportunity(
        opportunity_id=opportunity_id,
        user_id=user_id,
        updates={"discovery_paper": paper},
    )
    document_id = UUID(str(paper["document_id"]))
    draft = store.get_draft_discovery_paper_version(
        opportunity_id=opportunity_id,
        user_id=user_id,
        document_id=document_id,
    )
    if draft is None:
        created = store.create_discovery_paper_version(
            opportunity_id=opportunity_id,
            user_id=user_id,
            document_id=document_id,
            paper_json=paper,
        )
        return paper, created["id"]
    updated = store.update_discovery_paper_draft(
        version_id=draft["id"],
        user_id=user_id,
        paper_json=paper,
    )
    return paper, updated["id"]


def _edit_analysis(paper: dict[str, Any], edits: list[dict[str, Any]] | None) -> None:
    if not edits:
        raise bad_request(
            "DISCOVERY_PAPER_INVALID",
            "An AI Opportunity Analysis is edited by section, not by page",
        )
    if paper.get("status") != "ready" or paper.get("analysis") is None:
        raise bad_request(
            "DISCOVERY_PAPER_PAGE_NOT_EDITABLE",
            "The analysis is not ready to edit",
        )
    try:
        paper["analysis"] = apply_edits(paper["analysis"], edits)
        # Pages and the presentation brief are views of the content: rebuild, never patch.
        finalize(paper)
    except DiscoveryAnalysisError as exc:
        raise bad_request("DISCOVERY_PAPER_INVALID", str(exc)) from exc


def _edit_pages(paper: dict[str, Any], pages: list[dict[str, Any]] | None) -> None:
    if not pages:
        raise bad_request("DISCOVERY_PAPER_INVALID", "This Discovery Paper is edited by page")
    original_system = {field: copy.deepcopy(paper.get(field)) for field in _SYSTEM_FIELDS}
    original_shells = [
        {field: page.get(field) for field in ("key", "order", "title", "status")}
        for page in paper["pages"]
    ]
    seen: set[str] = set()
    by_key = {page["key"]: page for page in paper["pages"]}
    for edit in pages:
        key = str(edit["key"])
        if key in seen:
            raise bad_request("DISCOVERY_PAPER_INVALID", f"Page {key} was edited more than once")
        seen.add(key)
        page = by_key.get(key)
        if page is None:
            raise bad_request("DISCOVERY_PAPER_UNKNOWN_PAGE", f"Unknown Discovery Paper page {key}")
        if page.get("status") != "ready":
            raise bad_request(
                "DISCOVERY_PAPER_PAGE_NOT_EDITABLE",
                f"Page {key} is not ready to edit",
            )
        page["content"] = copy.deepcopy(edit["content"])
    for field, value in original_system.items():
        paper[field] = value
    for page, shell in zip(paper["pages"], original_shells, strict=True):
        page.update(shell)
    _require_valid(paper)


def approve_discovery_paper(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
) -> dict[str, Any]:
    paper = _stored_paper(store, opportunity_id=opportunity_id, user_id=user_id)
    if paper.get("status") != "ready" or any(
        page.get("status") != "ready" for page in ([] if is_v2(paper) else paper["pages"])
    ):
        raise bad_request(
            "DISCOVERY_PAPER_NOT_READY",
            "Only a fully ready Discovery Paper can be approved",
        )
    _require_valid(paper)
    draft = store.get_draft_discovery_paper_version(
        opportunity_id=opportunity_id,
        user_id=user_id,
        document_id=UUID(str(paper["document_id"])),
    )
    if draft is None:
        raise bad_request(
            "DISCOVERY_PAPER_DRAFT_NOT_FOUND",
            "No draft Discovery Paper version matches the current document",
        )
    approved_paper = copy.deepcopy(paper)
    approved_paper["latest_approved_version_id"] = str(draft["id"])
    _require_valid(approved_paper)
    approved = store.approve_discovery_paper_version(
        version_id=draft["id"],
        user_id=user_id,
        paper_json=approved_paper,
    )
    store.update_opportunity(
        opportunity_id=opportunity_id,
        user_id=user_id,
        updates={"discovery_paper": approved_paper},
    )
    return _public_version(approved)


def get_discovery_paper_version(
    store: Any,
    *,
    opportunity_id: UUID,
    version_id: UUID,
    user_id: UUID,
) -> dict[str, Any]:
    row = store.get_discovery_paper_version(version_id=version_id, user_id=user_id)
    if row["opportunity_id"] != opportunity_id:
        raise not_found(
            "DISCOVERY_PAPER_VERSION_NOT_FOUND",
            f"Discovery Paper version {version_id} was not found",
        )
    return _public_version(row)


def list_discovery_paper_versions(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
) -> list[dict[str, Any]]:
    return [
        _public_version(row, include_paper=False)
        for row in store.list_discovery_paper_versions(
            opportunity_id=opportunity_id,
            user_id=user_id,
        )
    ]


def get_latest_approved_discovery_paper(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
) -> dict[str, Any]:
    """Authoritative lookup for the latest approved Discovery Paper version."""
    row = store.get_latest_approved_discovery_paper(
        opportunity_id=opportunity_id,
        user_id=user_id,
    )
    if row is None:
        raise not_found(
            "DISCOVERY_PAPER_NOT_APPROVED",
            f"No approved Discovery Paper exists for opportunity {opportunity_id}",
        )
    return _public_version(row)


def _stored_paper(store: Any, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    stored = opportunity.get("discovery_paper")
    if not isinstance(stored, dict) or stored.get("status") == "not_generated" or stored.get("document_id") is None:
        raise bad_request(
            "DISCOVERY_PAPER_NOT_GENERATED",
            "Generate a Discovery Paper before editing or approving it",
        )
    return copy.deepcopy(stored)


def _require_valid(paper: dict[str, Any]) -> None:
    try:
        if is_v2(paper):
            validate_discovery_analysis(paper)
        else:
            validate_discovery_paper(paper)
    except (ValidationError, ValueError) as exc:
        raise bad_request("DISCOVERY_PAPER_INVALID", str(exc).splitlines()[0]) from exc


def _public_version(row: dict[str, Any], *, include_paper: bool = True) -> dict[str, Any]:
    payload = {
        "id": str(row["id"]),
        "opportunity_id": str(row["opportunity_id"]),
        "version_number": row["version_number"],
        "document_id": str(row["document_id"]),
        "status": row["status"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "approved_at": _iso(row["approved_at"]) if row.get("approved_at") else None,
    }
    if include_paper:
        payload["paper_json"] = copy.deepcopy(row["paper_json"])
    return payload


def _iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _live_complete():
    from llm.claude.client import structured_complete

    def complete(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return structured_complete(
            system,
            user,
            schema,
            tool_name="submit_opportunity_analysis_stage",
            tool_description="Submit one stage of the AI Opportunity Analysis.",
            max_tokens=4096,
        )

    return complete
