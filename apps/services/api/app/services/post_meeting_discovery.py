"""BAP-7 Discovery 2: generation, editing, approval, versions and Word export.

Discovery 2 is generated once the meeting findings are confirmed (the Post Meeting review has no
blocker before Discovery 2). Its lifecycle:

    not_generated -> generating -> draft -> approved
                               \\-> failed

* Every generation and every edit of an approved version creates a new draft version; a draft is
  edited in place. Approved versions are immutable (store and database).
* Edits and approvals name the version and the content they were made on; anything else is a
  conflict, so concurrent requests never overwrite each other.
* Only the latest version can be approved, and only while it was generated from the current
  meeting basis. Master Presentation V2 is generated from that approved version only.

The approved Discovery 1 is read and never written.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.config import settings
from app.services.api_errors import bad_request, conflict, not_found
from app.services.meeting_extraction import transcript_sections
from app.services.post_meeting_review import build_post_meeting_review
from services.meeting.discovery2 import (
    GENERATOR_VERSION,
    PROMPT_VERSION,
    STAGE_DISCOVERY2,
    Discovery2Error,
    apply_section_edits,
    content_hash,
    empty_discovery2,
    generate_discovery2,
    has_customer_meeting_content,
    has_meeting_content,
    safe_file_name,
    validate_discovery2,
)
from services.meeting.discovery_docx import render_discovery1_docx, render_discovery2_docx
from services.meeting.extraction import transcript_text_from_sections
from app.services.content_language import require_source_language
from services.language import in_other_language, stored_language

DISCOVERY2_BLOCKERS = frozenset({"DISCOVERY2_MISSING", "DISCOVERY2_NOT_APPROVED", "DISCOVERY2_STALE"})
_INPUT_STEPS = {
    "DISCOVERY_NOT_APPROVED": "approve the Discovery document",
    "MASTER_PRESENTATION_V1_NOT_READY": "generate Master Presentation V1",
    "FIRST_MEETING_NOT_COMPLETED": "mark the first meeting as completed",
    "TRANSCRIPT_MISSING": "upload the meeting transcript",
    "MEETING_EXTRACTION_MISSING": "analyse the meeting",
    "MEETING_EXTRACTION_STALE": "analyse the meeting again",
    "MEETING_REVIEW_NOT_CONFIRMED": "confirm the meeting findings",
    "MEETING_REVIEW_STALE": "confirm the meeting findings again",
    "MEETING_FINDINGS_EMPTY": "provide a transcript or notes with at least one finding",
    "MEETING_FINDINGS_NONE_CONFIRMED": "confirm at least one meeting finding",
}
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text.replace("+00:00", "Z")


def public_version(row: dict[str, Any], *, include_document: bool = True) -> dict[str, Any]:
    document = row.get("document_json") if isinstance(row.get("document_json"), dict) else {}
    payload = {
        "id": str(row["id"]),
        "opportunity_id": str(row["opportunity_id"]),
        "version_number": int(row["version_number"]),
        "document_id": str(row["document_id"]),
        "status": str(row["status"]),
        "content_hash": str(row["content_hash"]),
        "basis_hash": str(row["basis_hash"]),
        "discovery1_version_id": str(row["discovery1_version_id"]),
        "generation_mode": (document.get("generation") or {}).get("mode"),
        "language": stored_language(document.get("language")),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "approved_at": _iso(row.get("approved_at")),
        "approved_by": str(row["approved_by"]) if row.get("approved_by") else None,
    }
    if include_document:
        payload["document"] = copy.deepcopy(document)
    return payload


def get_post_meeting_discovery(store: Any, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
    """The current Discovery 2 (the latest version, or the working state before the first one)."""
    review = build_post_meeting_review(store, opportunity_id=opportunity_id, user_id=user_id)
    versions = store.list_discovery2_versions(opportunity_id=opportunity_id, user_id=user_id)
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    latest = versions[-1] if versions else None
    if latest is not None:
        document = copy.deepcopy(latest["document_json"])
    else:
        working = opportunity.get("post_meeting_discovery")
        document = copy.deepcopy(working) if isinstance(working, dict) else empty_discovery2(opportunity)
    input_blockers = [code for code in review["readiness"]["blockers"] if code not in DISCOVERY2_BLOCKERS]
    return {
        "opportunity_id": str(opportunity_id),
        "state": review["discovery2"],
        "document": document,
        "current_version": None if latest is None else public_version(latest, include_document=False),
        "versions": [public_version(row, include_document=False) for row in versions],
        "input_blockers": input_blockers,
        "finalized": review["finalized"],
        "ready_for_v2": review["readiness"]["ready_for_v2"],
        "language_check": language_check(document),
    }


def language_check(document: dict[str, Any]) -> dict[str, Any]:
    """PR #13 review: items that read as another language than the document.

    Items keep the words they were confirmed or approved in; nothing is translated without a
    check. The owner can reword such an item in Discovery 2 (it then counts as the owner's wording).
    """
    language = stored_language(document.get("language"))
    items = [item for section in document.get("sections") or [] for item in section.get("items") or []]
    return {"language": language, "other_language_item_ids": [str(item["id"]) for item in items if in_other_language(item.get("text"), language)]}


def generate_post_meeting_discovery(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    complete: Any = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Generate a new Discovery 2 draft version from the current, confirmed meeting basis.

    BAP-13: ``language`` defaults to the approved Discovery 1's language; another language is
    refused (``LANGUAGE_MISMATCH``) because the carried-forward items are in Discovery 1's words.
    """
    review = build_post_meeting_review(store, opportunity_id=opportunity_id, user_id=user_id)
    if review["finalized"]:
        raise conflict("WORKFLOW_FINALIZED", "This package is finalized. Discovery 2 cannot be generated again.")
    missing = [code for code in review["readiness"]["blockers"] if code not in DISCOVERY2_BLOCKERS]
    if missing:
        steps = ", ".join(_INPUT_STEPS.get(code, code) for code in missing)
        raise conflict("DISCOVERY2_INPUTS_NOT_READY", f"Discovery 2 cannot be generated yet. In Post Meeting, {steps}.")
    state = review["discovery2"]
    if state["status"] == "generating":
        raise conflict("DISCOVERY2_GENERATION_IN_PROGRESS", "Discovery 2 is being generated. Reload in a moment.")
    basis = state["basis_hash"]
    # BAP-13: a language the approved Discovery 1 was not written in is refused before anything
    # is written - a refused request leaves the working document, versions and readiness as they were.
    discovery1 = store.get_discovery_paper_version(version_id=UUID(review["approved_discovery"]["version_id"]), user_id=user_id)
    paper = discovery1.get("paper_json") if isinstance(discovery1.get("paper_json"), dict) else {}
    language = require_source_language(language, paper.get("language"), artifact="Discovery 2", source="Discovery document")
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    working = empty_discovery2(opportunity)
    working.update({"status": "generating", "generated_at": _now()})
    _set_working(store, opportunity_id=opportunity_id, user_id=user_id, document=working)
    try:
        document = _generate(store, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id, review=review, basis=basis, complete=complete, language=language)
        # The sources must still be the ones the document was generated from.
        again = build_post_meeting_review(store, opportunity_id=opportunity_id, user_id=user_id)
        if again["discovery2"]["basis_hash"] != basis:
            raise Discovery2Error(
                "A meeting source changed while Discovery 2 was being generated. Generate it again.", "DISCOVERY2_SOURCES_CHANGED"
            )
    except Discovery2Error as exc:
        _fail(store, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id, code=exc.code, message=exc.user_message)
        error = conflict if exc.code in {"DISCOVERY2_SOURCES_CHANGED", "DISCOVERY_NOT_APPROVED", "LANGUAGE_MISMATCH"} else bad_request
        raise error(exc.code, exc.user_message) from exc
    except Exception:
        # Nothing is left in "generating": the failure is visible and a new generation can start.
        _fail(store, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id, code="DISCOVERY2_GENERATION_FAILED", message="Discovery 2 could not be generated. Try again.")
        raise
    row = store.create_discovery2_version(
        opportunity_id=opportunity_id,
        user_id=user_id,
        document_id=UUID(document["document_id"]),
        document_json=document,
        content_hash=content_hash(document),
        basis_hash=basis,
        discovery1_version_id=UUID(document["sources"]["discovery1"]["version_id"]),
    )
    _set_working(store, opportunity_id=opportunity_id, user_id=user_id, document=document)
    return {**get_post_meeting_discovery(store, opportunity_id=opportunity_id, user_id=user_id), "generated_version_id": str(row["id"])}


def _generate(
    store: Any,
    *,
    opportunity: dict[str, Any],
    opportunity_id: UUID,
    user_id: UUID,
    review: dict[str, Any],
    basis: str,
    complete: Any,
    language: str | None = None,
) -> dict[str, Any]:
    discovery1 = store.get_discovery_paper_version(version_id=UUID(review["approved_discovery"]["version_id"]), user_id=user_id)
    if discovery1["opportunity_id"] != opportunity_id:
        raise Discovery2Error("The approved Discovery belongs to another opportunity.", "DISCOVERY_NOT_APPROVED")
    extraction = review["extraction"]
    sections = transcript_sections(store, opportunity_id=opportunity_id, user_id=user_id, transcript_id=extraction["transcript_id"]) or []
    live = settings.AI_EXECUTION_MODE == "live"
    if live and complete is None:
        complete = _live_complete(str(opportunity_id))
    return generate_discovery2(
        opportunity=opportunity,
        document_id=str(uuid.uuid4()),
        generated_at=_now(),
        discovery1=discovery1,
        transcript={
            "transcript_id": extraction["transcript_id"],
            "revision": extraction["transcript_revision"],
            "file_name": extraction["transcript_file_name"],
            "text": transcript_text_from_sections(sections),
        },
        personal_notes=review["personal_notes"],
        extraction=extraction,
        confirmation=review["confirmation"],
        basis=basis,
        live=live,
        complete=complete if live else None,
        language=language,
    )


def _set_working(store: Any, *, opportunity_id: UUID, user_id: UUID, document: dict[str, Any]) -> None:
    store.update_opportunity(opportunity_id=opportunity_id, user_id=user_id, updates={"post_meeting_discovery": validate_discovery2(document)})


def _fail(store: Any, *, opportunity: dict[str, Any], opportunity_id: UUID, user_id: UUID, code: str, message: str) -> None:
    failed = empty_discovery2(opportunity)
    failed.update({"status": "failed", "generated_at": _now(), "error": {"code": code, "message": message}})
    _set_working(store, opportunity_id=opportunity_id, user_id=user_id, document=failed)


def _latest(store: Any, *, opportunity_id: UUID, user_id: UUID, expected_version_id: UUID, expected_content_hash: str) -> dict[str, Any]:
    versions = store.list_discovery2_versions(opportunity_id=opportunity_id, user_id=user_id)
    if not versions:
        raise bad_request("DISCOVERY2_NOT_GENERATED", "Generate Discovery 2 first.")
    latest = versions[-1]
    if str(latest["id"]) != str(expected_version_id) or latest["content_hash"] != expected_content_hash:
        raise conflict("DISCOVERY2_STALE", "Discovery 2 changed after it was loaded. Reload it and try again.")
    return latest


def edit_post_meeting_discovery(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    expected_version_id: UUID,
    expected_content_hash: str,
    sections: list[dict[str, Any]],
) -> dict[str, Any]:
    """Save the owner's edit: in place for a draft, as a new draft version after an approval."""
    review = build_post_meeting_review(store, opportunity_id=opportunity_id, user_id=user_id)
    if review["finalized"]:
        raise conflict("WORKFLOW_FINALIZED", "This package is finalized. Discovery 2 is read-only.")
    latest = _latest(store, opportunity_id=opportunity_id, user_id=user_id, expected_version_id=expected_version_id, expected_content_hash=expected_content_hash)
    try:
        edited = apply_section_edits(latest["document_json"], sections)
    except Discovery2Error as exc:
        raise bad_request(exc.code, exc.user_message) from exc
    new_hash = content_hash(edited)
    if new_hash == latest["content_hash"]:
        return get_post_meeting_discovery(store, opportunity_id=opportunity_id, user_id=user_id)
    if latest["status"] == "draft":
        store.update_discovery2_draft(
            version_id=latest["id"], user_id=user_id, document_json=edited, content_hash=new_hash, expected_content_hash=expected_content_hash
        )
    else:
        # The approved version stays as it is; the edit is the next draft of the same document.
        store.create_discovery2_version(
            opportunity_id=opportunity_id,
            user_id=user_id,
            document_id=latest["document_id"],
            document_json=edited,
            content_hash=new_hash,
            basis_hash=latest["basis_hash"],
            discovery1_version_id=latest["discovery1_version_id"],
        )
    _set_working(store, opportunity_id=opportunity_id, user_id=user_id, document=edited)
    return get_post_meeting_discovery(store, opportunity_id=opportunity_id, user_id=user_id)


def approve_post_meeting_discovery(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    expected_version_id: UUID,
    expected_content_hash: str,
) -> dict[str, Any]:
    review = build_post_meeting_review(store, opportunity_id=opportunity_id, user_id=user_id)
    if review["finalized"]:
        raise conflict("WORKFLOW_FINALIZED", "This package is finalized. Discovery 2 is read-only.")
    latest = _latest(store, opportunity_id=opportunity_id, user_id=user_id, expected_version_id=expected_version_id, expected_content_hash=expected_content_hash)
    if latest["status"] == "approved":
        return get_post_meeting_discovery(store, opportunity_id=opportunity_id, user_id=user_id)
    basis = review["discovery2"]["basis_hash"]
    if basis is None:
        missing = [code for code in review["readiness"]["blockers"] if code not in DISCOVERY2_BLOCKERS]
        steps = ", ".join(_INPUT_STEPS.get(code, code) for code in missing)
        raise conflict("DISCOVERY2_INPUTS_NOT_READY", f"Discovery 2 cannot be approved while the meeting information is incomplete. In Post Meeting, {steps}.")
    if latest["basis_hash"] != basis:
        raise conflict(
            "DISCOVERY2_STALE",
            "The meeting information changed after this Discovery 2 was generated. Generate Discovery 2 again before approving it.",
        )
    if not has_meeting_content(latest["document_json"]):
        raise bad_request("DISCOVERY2_NO_MEETING_CONTENT", "Discovery 2 needs at least one item from the meeting before it can be approved.")
    if not has_customer_meeting_content(latest["document_json"]):
        raise bad_request(
            "DISCOVERY2_NO_CLIENT_CONTENT",
            "Every item from the meeting comes only from your personal notes, which stay internal. "
            "Share at least one of them with the client, or add a point in your own words, before approving.",
        )
    store.approve_discovery2_version(version_id=latest["id"], user_id=user_id, expected_content_hash=expected_content_hash)
    return get_post_meeting_discovery(store, opportunity_id=opportunity_id, user_id=user_id)


def list_post_meeting_discovery_versions(store: Any, *, opportunity_id: UUID, user_id: UUID) -> list[dict[str, Any]]:
    return [public_version(row, include_document=False) for row in store.list_discovery2_versions(opportunity_id=opportunity_id, user_id=user_id)]


def get_post_meeting_discovery_version(store: Any, *, opportunity_id: UUID, version_id: UUID, user_id: UUID) -> dict[str, Any]:
    return public_version(_own_version(store, opportunity_id=opportunity_id, version_id=version_id, user_id=user_id))


def _own_version(store: Any, *, opportunity_id: UUID, version_id: UUID, user_id: UUID) -> dict[str, Any]:
    row = store.get_discovery2_version(version_id=version_id, user_id=user_id)
    if row["opportunity_id"] != opportunity_id:
        raise not_found("DISCOVERY2_VERSION_NOT_FOUND", f"Discovery 2 version {version_id} was not found")
    return row


def discovery2_docx(store: Any, *, opportunity_id: UUID, version_id: UUID, user_id: UUID) -> dict[str, Any]:
    row = _own_version(store, opportunity_id=opportunity_id, version_id=version_id, user_id=user_id)
    document = row["document_json"]
    content = render_discovery2_docx(document, {"version_number": row["version_number"], "status": row["status"], "approved_at": _iso(row.get("approved_at"))})
    name = safe_file_name(str(document.get("title") or "Discovery 2"), "Discovery 2")
    return {"file_name": f"{name} - v{row['version_number']}.docx", "content": content, "media_type": DOCX_MEDIA_TYPE}


def discovery1_docx(store: Any, *, opportunity_id: UUID, version_id: UUID, user_id: UUID) -> dict[str, Any]:
    row = store.get_discovery_paper_version(version_id=version_id, user_id=user_id)
    if row["opportunity_id"] != opportunity_id:
        raise not_found("DISCOVERY_PAPER_VERSION_NOT_FOUND", f"Discovery Paper version {version_id} was not found")
    paper = row["paper_json"]
    if not isinstance(paper, dict) or paper.get("schema_version") != "2.0" or not isinstance(paper.get("analysis"), dict):
        raise conflict("DISCOVERY_DOCX_UNSUPPORTED", "This Discovery version is stored in the earlier format; download it as PDF from the Discovery page.")
    content = render_discovery1_docx(paper, {"version_number": row["version_number"], "status": row["status"], "approved_at": _iso(row.get("approved_at"))})
    title = str(paper["analysis"]["framing"]["document"].get("title") or "Discovery")
    return {"file_name": f"{safe_file_name(title, 'Discovery')} - Discovery 1 v{row['version_number']}.docx", "content": content, "media_type": DOCX_MEDIA_TYPE}


def _live_complete(opportunity_id: str):
    from llm.claude.client import CLAUDE_STRUCTURED_MAX_TOKENS, ClaudeClientError, sonnet_model, structured_complete
    from services.observability.llm_logger import run_logged_llm_call

    def complete(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        def invoke() -> dict[str, Any]:
            return structured_complete(
                system,
                user,
                schema,
                tool_name="submit_discovery2_meeting_update",
                tool_description="Submit what the meeting added to the Discovery: eight arrays of items with verbatim evidence. Empty arrays are valid.",
                max_tokens=CLAUDE_STRUCTURED_MAX_TOKENS,
            )

        try:
            return run_logged_llm_call(
                stage=STAGE_DISCOVERY2,
                prompt_version=PROMPT_VERSION,
                model=sonnet_model(),
                attempt=1,
                opportunity_id=opportunity_id,
                invoke=invoke,
            )
        except ClaudeClientError as exc:
            raise Discovery2Error(exc.user_message, "DISCOVERY2_GENERATION_FAILED") from exc

    return complete


__all__ = [
    "DOCX_MEDIA_TYPE",
    "GENERATOR_VERSION",
    "approve_post_meeting_discovery",
    "discovery1_docx",
    "discovery2_docx",
    "edit_post_meeting_discovery",
    "generate_post_meeting_discovery",
    "get_post_meeting_discovery",
    "get_post_meeting_discovery_version",
    "list_post_meeting_discovery_versions",
]
