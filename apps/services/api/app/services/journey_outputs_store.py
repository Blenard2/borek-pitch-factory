"""BT-36: map persisted journey artefacts off the public opportunity GET."""

from __future__ import annotations

from typing import Any

JOURNEY_OUTPUT_DB_COLUMNS = (
    "meeting_feedback_text",
    "meeting_feedback_updated_at",
    "stage1_outputs",
    "stage2_outputs",
    "email_drafts",
    "client_preparation_email",
    "discovery_paper",
    "personal_notes",
    "personal_notes_updated_at",
    "meeting_extraction",
    "meeting_review",
    "post_meeting_discovery",
    "selected_use_case_ids",
    "first_meeting_completed_at",
    "owner_reviewed_at",
    "owner_reviewed_presentation_version_id",
    "finalized_at",
    "finalization_snapshot",
)


def reject_finalization_snapshot_change(current: dict[str, Any], updates: dict[str, Any]) -> None:
    """A written finalization snapshot stays byte-for-byte until a future ticket says otherwise."""
    if "finalization_snapshot" not in updates:
        return
    existing = current.get("finalization_snapshot")
    if existing is None:
        return
    if updates["finalization_snapshot"] != existing:
        from app.services.api_errors import conflict

        raise conflict(
            "FINALIZATION_SNAPSHOT_IMMUTABLE",
            "The finalization snapshot cannot be changed after it is written.",
        )


def strip_journey_output_columns(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in JOURNEY_OUTPUT_DB_COLUMNS}
