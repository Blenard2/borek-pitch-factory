"""BAP-11: which saved Discovery documents belong to a client's post-meeting document list.

Read-only. Nothing is generated, regenerated, edited or approved here; the documents themselves
are read and downloaded through the existing version endpoints.

Pre-Meeting Discovery Document - the approved Discovery version Presentation V1 was built from,
taken from the V1 version's generation source manifest (Master Presentation V1 and the earlier
PPT #1 both record ``approved_discovery_version_id``). The V1 version is, in this order:

1. the V1 version the latest ready Master Presentation V2 was built on
   (``base_presentation_version_id`` of the V2 manifest),
2. the V1 / PPT #1 version pinned when the package was finalized,
3. the latest ready V1 / PPT #1 version.

Presentation V1 can be rebuilt after the first meeting (from a Discovery approved later), and V2
and the final package then build on that rebuilt V1. Once the meeting is recorded, the V1 that was
ready at the meeting therefore decides: the V1 version named by the meeting's audit record, or,
when that record is missing, the V1 versions created before ``first_meeting_completed_at`` - but
only when they all refer to the same Discovery version. Otherwise the document is unavailable.

The latest approved Discovery is never used in its place: it can be an approval made after the
meeting. Only when no V1 version records its Discovery is a fallback used, and only if it is
unambiguous - the opportunity has exactly one approved Discovery version. Otherwise the document
is reported as unavailable, with the reason.

Post-Meeting Discovery Document - the latest approved Discovery 2 version, a separate document.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException

MASTER_V1_KIND = "master_presentation_v1"
MASTER_V2_KIND = "master_presentation_v2"
LEGACY_PPT1_KIND = "ppt1"
FIRST_MEETING_AUDIT_ACTION = "workflow.first_meeting_completed"
ANALYSIS_SCHEMA_VERSION = "2.0"

_UNAVAILABLE = {
    "NO_APPROVED_DISCOVERY": "No approved Discovery Document exists for this client.",
    "DISCOVERY_ORIGIN_AMBIGUOUS": "The Discovery version Presentation V1 was built from is not recorded, and more than one approved version exists.",
    "FIRST_MEETING_ORIGIN_AMBIGUOUS": "Presentation V1 was rebuilt after the first meeting, and the Discovery version used in the meeting cannot be determined.",
    "DISCOVERY_VERSION_MISSING": "The Discovery version Presentation V1 was built from could not be found.",
    "DISCOVERY_VERSION_MISMATCH": "The Discovery version Presentation V1 refers to belongs to another client.",
    "DISCOVERY_VERSION_NOT_APPROVED": "The Discovery version Presentation V1 refers to is not approved.",
}


def discovery_documents(store: Any, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
    """Both Discovery documents of the post-meeting document list. Raises 404 for a foreign client."""
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    return {
        "opportunity_id": str(opportunity_id),
        "pre_meeting": _pre_meeting(store, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id),
        "post_meeting": _post_meeting(store, opportunity_id=opportunity_id, user_id=user_id),
    }


# ------------------------------------------------------------------ Pre-Meeting (Discovery 1)


def _pre_meeting(store: Any, *, opportunity: dict[str, Any], opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
    versions = store.list_presentation_versions_for_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    anchor, how = _presentation_v1(opportunity, versions)
    anchor, how = _first_meeting_v1(store, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id, anchor=anchor, how=how, versions=versions)
    if how == "ambiguous":
        return _unavailable("FIRST_MEETING_ORIGIN_AMBIGUOUS")
    reference = _manifest(anchor).get("approved_discovery_version_id") if anchor else None
    if reference:
        try:
            row = store.get_discovery_paper_version(version_id=UUID(str(reference)), user_id=user_id)
        except (HTTPException, ValueError):
            return _unavailable("DISCOVERY_VERSION_MISSING")
        if str(row.get("opportunity_id")) != str(opportunity_id):
            return _unavailable("DISCOVERY_VERSION_MISMATCH")
        if row.get("status") != "approved":
            return _unavailable("DISCOVERY_VERSION_NOT_APPROVED")
        return _available(row, opportunity_id=opportunity_id, resolution=how, presentation_version=anchor)
    approved = [
        row
        for row in store.list_discovery_paper_versions(opportunity_id=opportunity_id, user_id=user_id)
        if row.get("status") == "approved" and str(row.get("opportunity_id")) == str(opportunity_id)
    ]
    if not approved:
        return _unavailable("NO_APPROVED_DISCOVERY")
    if len(approved) > 1:
        return _unavailable("DISCOVERY_ORIGIN_AMBIGUOUS")
    return _available(approved[0], opportunity_id=opportunity_id, resolution="only_approved_version", presentation_version=anchor)


def _presentation_v1(opportunity: dict[str, Any], versions: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    by_id = {str(row.get("id")): row for row in versions}
    ready_v2 = [row for row in versions if _kind(row) == MASTER_V2_KIND and row.get("status") == "ready"]
    if ready_v2:
        base = _manifest(_latest(ready_v2)).get("base_presentation_version_id")
        if base and str(base) in by_id:
            return by_id[str(base)], "presentation_v2_base"
    snapshot = _snapshot(opportunity)
    pinned = (snapshot or {}).get("ppt1_version_id")
    if pinned and str(pinned) in by_id:
        return by_id[str(pinned)], "finalized_presentation_v1"
    ready_v1 = _ready_v1(opportunity, versions)
    if ready_v1:
        return _latest(ready_v1), "presentation_v1"
    return None, "presentation_v1"


def _ready_v1(opportunity: dict[str, Any], versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    presentation_id = _stage1_presentation_id(opportunity)
    return [
        row
        for row in versions
        if row.get("journey_stage") == "first_contact"
        and row.get("status") == "ready"
        and (presentation_id is None or str(row.get("presentation_id")) == presentation_id)
        and _kind(row) in (MASTER_V1_KIND, LEGACY_PPT1_KIND, None)
    ]


def _first_meeting_v1(
    store: Any,
    *,
    opportunity: dict[str, Any],
    opportunity_id: UUID,
    user_id: UUID,
    anchor: dict[str, Any] | None,
    how: str,
    versions: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """The V1 that was ready at the first meeting, when it differs from ``anchor``.

    Returns ``(None, "ambiguous")`` when V1 was rebuilt after the meeting and the meeting's V1
    cannot be told apart. Before the meeting is recorded nothing changes.
    """
    met = _when(opportunity.get("first_meeting_completed_at"))
    if met is None:
        return anchor, how
    ready_v1 = _ready_v1(opportunity, versions)
    recorded = _recorded_meeting_v1(store, opportunity_id=opportunity_id, user_id=user_id)
    meeting_v1 = next((row for row in ready_v1 if str(row.get("id")) == recorded), None) if recorded else None
    if meeting_v1 is None:
        after = [row for row in ready_v1 if (_when(row.get("created_at")) or datetime.max.replace(tzinfo=UTC)) > met]
        if not after:
            return anchor, how  # nothing was rebuilt after the meeting
        before = [row for row in ready_v1 if row not in after]
        references = {str(_manifest(row).get("approved_discovery_version_id") or "") for row in before}
        if not before or len(references) != 1 or "" in references:
            return None, "ambiguous"
        meeting_v1 = _latest(before)
    if anchor is not None and str(anchor.get("id")) == str(meeting_v1.get("id")):
        return anchor, how
    return meeting_v1, "first_meeting_presentation_v1"


def _recorded_meeting_v1(store: Any, *, opportunity_id: UUID, user_id: UUID) -> str | None:
    """The V1 version id the first-meeting audit record names, if it can be read."""
    try:
        rows = store.list_audit_logs(actor_id=user_id, object_id=opportunity_id)
    except Exception:  # an unreadable audit log falls back to the timestamps
        return None
    rows = [row for row in rows if row.get("action") == FIRST_MEETING_AUDIT_ACTION and str(row.get("object_id")) == str(opportunity_id)]
    if not rows:
        return None
    first = min(rows, key=lambda row: _when(row.get("timestamp")) or datetime.max.replace(tzinfo=UTC))
    return str(first.get("document_id") or "") or None


def _available(row: dict[str, Any], *, opportunity_id: UUID, resolution: str, presentation_version: dict[str, Any] | None) -> dict[str, Any]:
    paper = row.get("paper_json") if isinstance(row.get("paper_json"), dict) else {}
    current = paper.get("schema_version") == ANALYSIS_SCHEMA_VERSION and isinstance(paper.get("analysis"), dict)
    version_id = str(row["id"])
    return {
        "state": "available",
        "label": "Pre-Meeting Discovery Document",
        "resolution": resolution,
        "version_id": version_id,
        "version_number": row.get("version_number"),
        "document_id": str(row.get("document_id") or ""),
        "approved_at": _iso(row.get("approved_at")),
        # "analysis": the current format (PDF from its stored pages, Word export);
        # "legacy": the earlier seven-page format (PDF through its own print path, no Word export).
        "format": "analysis" if current else "legacy",
        "docx_available": current,
        "version_path": f"/opportunities/{opportunity_id}/discovery-paper/versions/{version_id}",
        "docx_download_path": f"/opportunities/{opportunity_id}/discovery-paper/versions/{version_id}/download/docx" if current else None,
        "presentation_version_id": None if presentation_version is None else str(presentation_version.get("id")),
    }


def _unavailable(reason: str) -> dict[str, Any]:
    return {"state": "unavailable", "label": "Pre-Meeting Discovery Document", "reason": reason, "message": _UNAVAILABLE[reason]}


# ------------------------------------------------------------------ Post-Meeting (Discovery 2)


def _post_meeting(store: Any, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
    rows = [
        row
        for row in store.list_discovery2_versions(opportunity_id=opportunity_id, user_id=user_id)
        if row.get("status") == "approved" and str(row.get("opportunity_id")) == str(opportunity_id)
    ]
    if not rows:
        return {"state": "unavailable", "label": "Post-Meeting Discovery Document", "reason": "NO_APPROVED_DISCOVERY2", "message": "No approved Post-Meeting Discovery Document exists yet."}
    row = max(rows, key=lambda item: int(item.get("version_number") or 0))
    version_id = str(row["id"])
    return {
        "state": "available",
        "label": "Post-Meeting Discovery Document",
        "version_id": version_id,
        "version_number": row.get("version_number"),
        "approved_at": _iso(row.get("approved_at")),
        "docx_download_path": f"/opportunities/{opportunity_id}/post-meeting-discovery/versions/{version_id}/download/docx",
    }


# ------------------------------------------------------------------ helpers


def _manifest(row: dict[str, Any] | None) -> dict[str, Any]:
    manifest = (row or {}).get("generation_source_manifest")
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except json.JSONDecodeError:
            return {}
    return manifest if isinstance(manifest, dict) else {}


def _kind(row: dict[str, Any]) -> str | None:
    kind = _manifest(row).get("kind")
    return None if kind in (None, "") else str(kind)


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=lambda row: (int(row.get("version_number") or 0), str(row.get("created_at") or "")))


def _snapshot(opportunity: dict[str, Any]) -> dict[str, Any] | None:
    raw = opportunity.get("finalization_snapshot")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw if isinstance(raw, dict) else None


def _stage1_presentation_id(opportunity: dict[str, Any]) -> str | None:
    envelope = opportunity.get("stage1_outputs")
    presentation = ((envelope or {}).get("outputs") or {}).get("presentation") if isinstance(envelope, dict) else None
    raw = presentation.get("presentation_id") if isinstance(presentation, dict) else None
    return None if raw in (None, "") else str(raw)


def _when(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return value.isoformat().replace("+00:00", "Z") if hasattr(value, "isoformat") else str(value)
