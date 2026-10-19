"""Master Presentation V2: generation request, status and the checks the worker runs.

V2 is a new version of the opportunity's one Master Presentation (the same ``presentation_id``
as V1): the unchanged canonical deck followed by a post-meeting appendix. A generation is only
accepted when the Post Meeting review reports that everything is ready, and it is built from a
snapshot frozen at that moment (``services.presentation.master_deck.v2``). The worker renders
the frozen plan; it never reads the live sources again.

The legacy standalone PPT #2 generator is not involved anywhere in this module.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from app.services import job_service
from app.services.api_errors import bad_request, conflict
from app.services.data import DataStore
from app.services.post_meeting_review import build_post_meeting_review
from app.services.use_case_selection import get_selected_use_cases
from services.presentation.master_deck.appendix import AppendixPlanError
from services.presentation.master_deck.plan import MANIFEST_KIND_V2
from services.presentation.master_deck.registry import MasterDeckError
from services.presentation.master_deck.v2 import (
    SnapshotError,
    build_plan,
    build_snapshot,
    generation_manifest,
    same_source,
    verify_frozen_job,
)

PRODUCT_VERSION = "V2"
_BLOCKER_TEXT = {
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
    "DISCOVERY2_MISSING": "generate Discovery 2",
    "DISCOVERY2_NOT_APPROVED": "approve Discovery 2",
    "DISCOVERY2_STALE": "generate Discovery 2 again from the current meeting information and approve it",
}
# The generation lock every V2 job takes. The store admits one active job per opportunity and
# key (a partial unique index in the database), so competing requests are decided there and
# not in this process: it holds for any number of API replicas.
LOCK_KEY = "master_presentation_v2"


def _status_text(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _job_enqueue(row: dict[str, Any]) -> dict[str, Any]:
    result = row.get("result_json") if isinstance(row.get("result_json"), dict) else {}
    return dict(result.get("_enqueue") or {})


def v2_versions(store: DataStore, *, opportunity_id: UUID, user_id: UUID, presentation_id: Any) -> list[dict[str, Any]]:
    """Master Presentation V2 versions of this presentation, newest first."""
    rows = [
        row
        for row in store.list_presentation_versions_for_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        if str(row.get("presentation_id")) == str(presentation_id)
        and isinstance(row.get("generation_source_manifest"), dict)
        and row["generation_source_manifest"].get("kind") == MANIFEST_KIND_V2
    ]
    return sorted(rows, key=lambda row: int(row.get("version_number") or 0), reverse=True)


def _v2_jobs(store: DataStore, opportunity_id: UUID) -> list[dict[str, Any]]:
    lister = getattr(store, "list_generation_jobs_for_opportunity", None)
    rows = [row for row in (lister(opportunity_id) if lister else []) if isinstance(row, dict)]
    return [
        row
        for row in rows
        if isinstance(_job_enqueue(row).get("generation_source_manifest"), dict)
        and _job_enqueue(row)["generation_source_manifest"].get("kind") == MANIFEST_KIND_V2
    ]


def _public_version(row: dict[str, Any]) -> dict[str, Any]:
    manifest = row["generation_source_manifest"]
    return {
        "presentation_version_id": str(row["id"]),
        "version_number": int(row["version_number"]),
        "status": _status_text(row.get("status")),
        "source_hash": manifest.get("source_hash"),
        "generation_fingerprint": manifest.get("generation_fingerprint"),
        "meeting_review_confirmed_at": manifest.get("meeting_review_confirmed_at"),
        "base_presentation_version_id": manifest.get("base_presentation_version_id"),
        "approved_discovery_version_id": manifest.get("approved_discovery_version_id"),
        "approved_discovery2_version_id": manifest.get("approved_discovery2_version_id"),
        "confirmed_finding_count": manifest.get("confirmed_finding_count"),
    }


def build_master_v2_status(store: DataStore, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
    """What exists of Master Presentation V2 and whether it matches the current confirmation."""
    review = build_post_meeting_review(store, opportunity_id=opportunity_id, user_id=user_id)
    presentation_id = review["master_presentation"]["presentation_id"]
    # What a generation requested now would be built from; None while the review is not ready.
    current = None
    if review["readiness"]["ready_for_v2"] and review["v2_sources"] is not None:
        try:
            current = generation_manifest(_freeze(store, opportunity_id=opportunity_id, user_id=user_id, review=review))
        except SnapshotError:
            current = None
    versions = (
        v2_versions(store, opportunity_id=opportunity_id, user_id=user_id, presentation_id=presentation_id)
        if presentation_id
        else []
    )
    ready = [row for row in versions if _status_text(row.get("status")) == "ready"]
    latest_ready = ready[0] if ready else None
    jobs = sorted(_v2_jobs(store, opportunity_id), key=lambda row: str(row.get("created_at") or ""), reverse=True)
    active = next((row for row in jobs if _status_text(row.get("status")) in {"QUEUED", "RUNNING"}), None)
    last = jobs[0] if jobs else None
    if active is not None:
        state = "generating"
    elif latest_ready is not None and current is not None and same_source(latest_ready["generation_source_manifest"], current):
        state = "ready"
    elif last is not None and _status_text(last.get("status")) == "FAILED" and (
        latest_ready is None or str(last.get("created_at") or "") > str(latest_ready.get("created_at") or "")
    ):
        state = "failed"
    elif latest_ready is not None:
        # A ready V2 exists, but it was built from sources that have since been confirmed anew.
        state = "outdated"
    else:
        state = "none"
    job = active or (last if state == "failed" else None)
    return {
        "schema_version": "1.0",
        "opportunity_id": str(opportunity_id),
        "product_version": PRODUCT_VERSION,
        "presentation_id": presentation_id,
        "state": state,
        "can_generate": bool(review["readiness"]["ready_for_v2"]) and not review["finalized"] and state not in {"generating", "ready"},
        "ready_for_v2": bool(review["readiness"]["ready_for_v2"]),
        "blockers": list(review["readiness"]["blockers"]),
        "finalized": bool(review["finalized"]),
        "current_generation_fingerprint": None if current is None else current["generation_fingerprint"],
        "latest_ready": None if latest_ready is None else _public_version(latest_ready),
        "job": None
        if job is None
        else {
            "job_id": str(job["id"]),
            "status": _status_text(job.get("status")),
            "error_code": job.get("error_code"),
            "error_message": job.get("error_message"),
            "generation_fingerprint": _job_enqueue(job)["generation_source_manifest"].get("generation_fingerprint"),
        },
    }


def enqueue_master_presentation_v2(store: DataStore, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
    """Freeze the confirmed post-meeting sources and generate V2 as a new version of the presentation.

    Same confirmed sources and a ready V2: that version is returned. Same sources and a running
    job: that job is returned. Otherwise a new version is generated; earlier versions and their
    files are never touched.
    """
    review = build_post_meeting_review(store, opportunity_id=opportunity_id, user_id=user_id)
    if review["finalized"]:
        raise conflict("WORKFLOW_FINALIZED", "This package is finalized. Master Presentation V2 cannot be generated again.")
    if not review["readiness"]["ready_for_v2"] or review["v2_sources"] is None:
        steps = ", ".join(_BLOCKER_TEXT.get(code, code) for code in review["readiness"]["blockers"])
        raise conflict(
            "MASTER_V2_NOT_READY",
            f"Master Presentation V2 cannot be generated yet. Go back to Post Meeting and {steps}.",
        )
    try:
        snapshot = _freeze(store, opportunity_id=opportunity_id, user_id=user_id, review=review)
    except SnapshotError as exc:
        raise conflict(exc.code, str(exc)) from exc
    # Everything was read after the review; make sure it still is the review the snapshot belongs to.
    again = build_post_meeting_review(store, opportunity_id=opportunity_id, user_id=user_id)
    if again["review_fingerprint"] != review["review_fingerprint"] or again["v2_sources"] != review["v2_sources"]:
        raise conflict(
            "MASTER_V2_SOURCES_CHANGED",
            "A source changed while Master Presentation V2 was being prepared. Review the meeting information and try again.",
        )
    manifest = generation_manifest(snapshot)
    presentation_id = UUID(snapshot["presentation"]["presentation_id"])

    existing = _existing_generation(store, opportunity_id=opportunity_id, user_id=user_id, presentation_id=presentation_id, manifest=manifest)
    if existing is not None:
        return existing
    try:
        plan_json = build_plan(snapshot)
    except (AppendixPlanError, MasterDeckError, SnapshotError) as exc:
        raise bad_request(exc.code, str(exc)) from exc
    from app.services.presentation_generation import _dispatch_task, _framework_for_plan_storage

    framework = _framework_for_plan_storage(store, opportunity_id=opportunity_id, user_id=user_id)
    plan = store.create_presentation_plan(framework_version_id=framework["id"], user_id=user_id, plan_json=plan_json)
    try:
        # Creating the job takes the generation lock. Of several simultaneous requests - in this
        # process or in another API replica - exactly one gets past this line.
        job = job_service.create_job(
            opportunity_id=opportunity_id,
            job_type="presentation_generation",
            presentation_id=presentation_id,
            enqueue={
                "user_id": str(user_id),
                "presentation_id": str(presentation_id),
                "framework_version_id": str(framework["id"]),
                "presentation_plan_id": str(plan["id"]),
                "journey_stage": "post_meeting",
                "prior_stage_presentation_version_id": snapshot["presentation"]["base_presentation_version_id"],
                "generation_source_manifest": manifest,
                "master_v2_snapshot": snapshot,
            },
            repository=store,
            generation_lock_key=LOCK_KEY,
            generation_fingerprint=manifest["generation_fingerprint"],
        )
    except job_service.GenerationLockConflict:
        # Another request won. Its job (or the version it has produced meanwhile) is the answer;
        # the plan row created above stays unused and is never attached to the presentation.
        for _attempt in range(10):
            existing = _existing_generation(store, opportunity_id=opportunity_id, user_id=user_id, presentation_id=presentation_id, manifest=manifest)
            if existing is not None:
                return existing
            time.sleep(0.2)
        raise conflict(
            "PRESENTATION_GENERATION_IN_PROGRESS",
            "Master Presentation V2 is being generated. Reload in a moment.",
        ) from None
    # Only the request that holds the lock points the presentation at its plan. The presentation
    # id stays the one of V1; its earlier versions are not touched.
    presentation = store.retarget_presentation_plan(
        presentation_id=presentation_id,
        presentation_plan_id=plan["id"],
        user_id=user_id,
        name=str(plan_json["title"]),
    )
    from app.worker import run_presentation_generation_task

    _dispatch_task(run_presentation_generation_task, str(job.id), str(presentation["id"]), str(user_id))
    refreshed = job_service.get_job(job.id, repository=store) or job
    version_id = (refreshed.result_json or {}).get("presentation_version_id")
    return _response(presentation_id, job=refreshed, version_id=version_id, existing_job=False, manifest=manifest)


def _existing_generation(
    store: DataStore,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    presentation_id: UUID,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """A running job or a ready version for these inputs; a conflict for any other running generation."""
    held = job_service.active_locked_job(store, opportunity_id, LOCK_KEY)
    if held is not None:
        held_manifest = _job_enqueue(held).get("generation_source_manifest")
        if same_source(held_manifest, manifest):
            job = job_service.get_job(UUID(str(held["id"])), repository=store)
            if job is not None:
                return _response(presentation_id, job=job, version_id=None, existing_job=True, manifest=held_manifest)
        raise conflict(
            "PRESENTATION_GENERATION_IN_PROGRESS",
            "A Master Presentation V2 is being generated from other meeting information. Wait until it has finished.",
        )
    active = job_service.reuse_active_generation_job(store, opportunity_id, stage_group="presentation")
    if active is not None:
        raise conflict(
            "PRESENTATION_GENERATION_IN_PROGRESS",
            "Another presentation is being generated for this opportunity. Wait until it has finished.",
        )
    # Only the newest ready V2 is reused: it is the one the workflow shows and the owner reviews.
    # Inputs that match an older V2 are generated again as the new latest version.
    ready = [
        row
        for row in v2_versions(store, opportunity_id=opportunity_id, user_id=user_id, presentation_id=presentation_id)
        if _status_text(row.get("status")) == "ready"
    ]
    if ready and same_source(ready[0]["generation_source_manifest"], manifest):
        return _response(presentation_id, job=None, version_id=str(ready[0]["id"]), existing_job=False, manifest=ready[0]["generation_source_manifest"])
    return None


def _freeze(store: DataStore, *, opportunity_id: UUID, user_id: UUID, review: dict[str, Any]) -> dict[str, Any]:
    """Resolve the content behind the review's source identities and freeze it."""
    sources = review["v2_sources"]
    approved = store.get_discovery_paper_version(version_id=UUID(sources["approved_discovery_version_id"]), user_id=user_id)
    selected = get_selected_use_cases(store, opportunity_id=opportunity_id, user_id=user_id)
    if list(selected["use_case_ids"]) != list(sources["selected_use_case_ids"]):
        raise SnapshotError("The selected use cases changed after the meeting information was confirmed")
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    record = opportunity.get("meeting_review") if isinstance(opportunity.get("meeting_review"), dict) else {}
    confirmation = review["confirmation"]
    notes = review["personal_notes"]
    extraction = review["extraction"]
    if confirmation["status"] != "current" or confirmation["items"] is None:
        raise SnapshotError("The meeting findings are not confirmed")
    # BAP-7: the approved Discovery 2 the review names is the authoritative post-meeting source.
    discovery2 = store.get_discovery2_version(version_id=UUID(sources["discovery2_version_id"]), user_id=user_id)
    if (
        discovery2["status"] != "approved"
        or discovery2["content_hash"] != sources["discovery2_content_hash"]
        or discovery2["opportunity_id"] != opportunity_id
    ):
        raise SnapshotError("The approved Discovery 2 changed after the review was read")
    return build_snapshot(
        opportunity_id=str(opportunity_id),
        presentation_id=sources["presentation_id"],
        base_presentation_version_id=sources["base_presentation_version_id"],
        approved_discovery=approved,
        transcript={
            "transcript_id": sources["transcript_id"],
            "revision": sources["transcript_revision"],
            "file_name": extraction["transcript_file_name"],
        },
        extraction={
            "generated_at": extraction["generated_at"],
            "execution_mode": extraction["execution_mode"],
            "personal_notes_updated_at": extraction["personal_notes_updated_at"],
        },
        personal_notes={"updated_at": notes["updated_at"], "text": notes["text"]},
        reviewed_items=confirmation["items"],
        use_cases=list(selected["use_cases"]),
        meeting_review={
            "confirmed_at": confirmation["confirmed_at"],
            "confirmed_by": confirmation["confirmed_by"] or record.get("confirmed_by"),
            "review_fingerprint": review["review_fingerprint"],
            "source_hash": sources["source_hash"],
        },
        approved_discovery2=discovery2,
    )


def _response(
    presentation_id: UUID,
    *,
    job: job_service.Job | None,
    version_id: str | None,
    existing_job: bool,
    manifest: Any,
) -> dict[str, Any]:
    manifest = manifest if isinstance(manifest, dict) else {}
    return {
        "presentation_id": str(presentation_id),
        "presentation_version_id": version_id,
        "product_version": PRODUCT_VERSION,
        "journey_stage": "post_meeting",
        "status": "ready" if job is None else _status_text(job.status),
        "job_id": None if job is None else str(job.id),
        "is_existing_job": existing_job,
        "is_existing_version": job is None,
        "source_hash": manifest.get("source_hash"),
        "generation_fingerprint": manifest.get("generation_fingerprint"),
        "base_presentation_version_id": manifest.get("base_presentation_version_id"),
    }


def verify_job_before_generation(store: DataStore, *, enqueue: dict[str, Any], presentation_id: UUID, user_id: UUID) -> None:
    """Worker-side guard: the job renders its own frozen plan for its own presentation, or it fails.

    Reads identities only. The frozen snapshot on the job is the source of the deck; a live
    source that changed after the request is deliberately not consulted.
    """
    manifest = enqueue.get("generation_source_manifest")
    presentation = store.get_presentation(presentation_id=presentation_id, user_id=user_id)
    if str(presentation["presentation_plan_id"]) != str(enqueue.get("presentation_plan_id")):
        raise SnapshotError("The presentation was pointed at another plan after this generation was requested")
    plan = store.get_presentation_plan(presentation_plan_id=presentation["presentation_plan_id"], user_id=user_id)
    verify_frozen_job(manifest=manifest, snapshot=enqueue.get("master_v2_snapshot"), plan_json=plan["plan_json"])
    if str(manifest["presentation_id"]) != str(presentation_id):
        raise SnapshotError("The frozen sources belong to another presentation")
    try:
        base = store.get_presentation_version(
            presentation_version_id=UUID(str(manifest["base_presentation_version_id"])),
            user_id=user_id,
        )
    except HTTPException as exc:
        raise SnapshotError("The Master Presentation V1 version this V2 follows no longer exists") from exc
    if str(base["presentation_id"]) != str(presentation_id):
        raise SnapshotError("The V1 base version belongs to another presentation")
