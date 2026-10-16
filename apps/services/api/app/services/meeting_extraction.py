"""BT-44: persist owner notes and a transcript-scoped meeting extraction."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.config import settings
from app.services.api_errors import bad_request
from services.meeting.extraction import (
    MeetingExtractionError,
    extract_meeting_findings,
    validate_meeting_extraction,
)

CATEGORIES = (
    "requirements",
    "challenges",
    "priorities",
    "opportunities",
    "discussed_solutions",
    "decisions",
    "follow_ups",
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        text = value.isoformat()
    else:
        text = str(value)
    return text.replace("+00:00", "Z")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def transcript_revision(sections: list[dict[str, Any]]) -> str:
    """Checksum of the speaker turns an extraction reads; changes when the content changes."""
    turns = [
        [str(section.get("speaker_role") or ""), str(section.get("content") or "")]
        for section in sorted(sections, key=lambda item: int(item.get("section_index") or 0))
    ]
    return hashlib.sha256(json.dumps(turns, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def execution_mode() -> str:
    """"live" calls the configured model; "fixture" is the deterministic marker-line extractor."""
    return "live" if settings.AI_EXECUTION_MODE == "live" else "fixture"


def transcript_sections(store: Any, *, opportunity_id: UUID, user_id: UUID, transcript_id: Any) -> list[dict[str, Any]] | None:
    """Stored speaker turns of one transcript of this opportunity; None when it does not exist."""
    for row in store.list_transcript_sources(opportunity_id=opportunity_id, user_id=user_id):
        if str(row["id"]) == str(transcript_id):
            return list(row.get("sections") or [])
    return None


def personal_notes_view(opportunity: dict[str, Any]) -> dict[str, Any]:
    text = opportunity.get("personal_notes")
    if isinstance(text, str) and not text.strip():
        text = None
    return {
        "opportunity_id": str(opportunity["id"]),
        "text": text,
        "updated_at": _iso(opportunity.get("personal_notes_updated_at")),
    }


def get_meeting_extraction(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
) -> dict[str, Any] | None:
    """Return the stored BT-44 extraction, or None when it has not been generated."""
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    stored = opportunity.get("meeting_extraction")
    if not isinstance(stored, dict):
        return None
    return copy.deepcopy(stored)


def empty_meeting_extraction(opportunity_id: UUID) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "opportunity_id": str(opportunity_id),
        "status": "not_generated",
        "extraction": None,
    }


def generate_meeting_extraction(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    transcript_id: UUID,
) -> dict[str, Any]:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    store.get_transcript(
        opportunity_id=opportunity_id,
        transcript_id=transcript_id,
        user_id=user_id,
    )
    sections = transcript_sections(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
        transcript_id=transcript_id,
    ) or []
    if not any(str(section.get("content") or "").strip() for section in sections):
        raise bad_request(
            "TRANSCRIPT_EMPTY",
            "The selected transcript has no readable text. Upload the transcript again.",
        )
    notes_view = personal_notes_view(opportunity)
    notes = notes_view["text"]
    try:
        categories, item_sources = extract_meeting_findings(
            sections=sections,
            personal_notes=notes,
            live=execution_mode() == "live",
            opportunity_id=str(opportunity_id),
        )
    except MeetingExtractionError as exc:
        raise bad_request("MEETING_EXTRACTION_FAILED", exc.user_message) from exc
    payload = {
        "schema_version": "1.0",
        "opportunity_id": str(opportunity_id),
        "transcript_id": str(transcript_id),
        "generated_at": _now(),
        "personal_notes_updated_at": notes_view["updated_at"] if notes else None,
        **{category: list(categories[category]) for category in CATEGORIES},
        "transcript_revision": transcript_revision(sections),
        "execution_mode": execution_mode(),
        "item_sources": item_sources,
    }
    stored = copy.deepcopy(validate_meeting_extraction(payload))
    store.update_opportunity(
        opportunity_id=opportunity_id,
        user_id=user_id,
        updates={"meeting_extraction": stored},
    )
    return copy.deepcopy(stored)
