"""Presentation planning/generation orchestration (AT-42 / AT-43).

HTTP handlers only enqueue jobs. Workers invoke the owner planner and layout
generators, then render validated artifacts. Unknown layout ids are stripped
from the persisted approved plan so AT-10 can compare that plan to generated
SlideSpecs without a silent count lie.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from app.config import settings
from app.schemas.jobs import JobStatus
from app.schemas.presentations import LAYOUT_REGISTRY, VALID_LAYOUT_IDS
from app.services import job_service
from app.services.api_errors import bad_request, conflict, not_found
from app.services.data import DataStore
from app.services.journey_stage import require_startable_journey_stage
from app.services.renderer_client import render_borek_deck_assets, render_deck_assets
from app.services.stage_b_orchestration import plan_json_from_confirmed_framework
from app.services.stage_b_providers import install_runtime_stage_b_providers
from services.framework.stage1_intake import resolve_meeting_purpose
from services.presentation.borek_deck.deck_plan import PRE_MEETING, is_borek_plan
from services.presentation.master_deck.plan import is_master_plan
from services.presentation.generatable_layouts import filter_generatable_planned_slides


# Stage 1 schema still requires this historical profile id. New PPT #1 decks
# are planned from the approved Discovery Paper, not from the legacy 3-slide stamp.
FIRST_CONTACT_PRESENTATION_PROFILE = "first_meeting_3"

FIRST_CONTACT_PRESENTATION_PLAN = {
    "schema_version": "1.0",
    "title": "First-meeting presentation",
    "slides": [
        {
            "order": 1,
            "purpose": "Introduce Borek and the first-meeting topic",
            "layoutId": "COVER_01",
            "frameworkReferences": ["opportunity"],
        },
        {
            "order": 2,
            "purpose": "Present the evidence-bound opening hypothesis",
            "layoutId": "PROBLEM_SOLUTION_01",
            "frameworkReferences": ["chapter_4"],
        },
        {
            "order": 3,
            "purpose": "Connect the client context to a relevant Borek use case",
            "layoutId": "CONTEXT_01",
            "frameworkReferences": ["chapter_1"],
        },
    ],
}


def _first_contact_plan_from_framework(
    framework: dict[str, Any],
    opportunity: dict[str, Any],
) -> dict[str, Any]:
    """Legacy three-slide stamp. Not used by new PPT #1 generation.

    Kept so historical callers and fixtures can still read the old profile shape.
    Active PPT #1 planning goes through plan_first_pitch_from_discovery.
    """
    stamp = framework.get("framework_json", {}).get("stage1_intake") or {}
    if not isinstance(stamp, dict):
        stamp = {}
    title_name = str(
        stamp.get("opportunity_name") or opportunity.get("opportunity_name") or "Opportunity"
    ).strip() or "Opportunity"
    purpose = str(
        stamp.get("sales_topic_description") or resolve_meeting_purpose(opportunity) or ""
    ).strip()
    plan_json = copy.deepcopy(FIRST_CONTACT_PRESENTATION_PLAN)
    plan_json["title"] = f"First meeting — {title_name}"
    if purpose:
        plan_json["slides"][0]["purpose"] = (
            "Introduce Borek and the first-meeting topic: " + purpose
        )
    return plan_json


def _dispatch_task(task, *args: str) -> None:
    if settings.API_DATA_BACKEND == "memory":
        task.run(*args)
    else:
        task.delay(*args)


def _require_confirmed_framework(
    store: DataStore,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    framework_version_id: UUID | None,
) -> dict:
    if framework_version_id is not None:
        row = store.get_framework_version(
            framework_version_id=framework_version_id,
            user_id=user_id,
        )
        if row["opportunity_id"] != opportunity_id:
            raise not_found(
                "FRAMEWORK_NOT_FOUND",
                f"Framework version {framework_version_id} was not found",
            )
    else:
        row = store.get_latest_framework(opportunity_id=opportunity_id, user_id=user_id)

    if row["status"] != "confirmed":
        raise bad_request(
            "FRAMEWORK_NOT_CONFIRMED",
            "Framework must be confirmed before presentation planning or generation",
        )
    return row


def _resolve_presentation_plan(
    store: DataStore,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    framework_version_id: UUID,
    presentation_plan_id: UUID | None,
) -> dict:
    if presentation_plan_id is not None:
        plan = store.get_presentation_plan(
            presentation_plan_id=presentation_plan_id,
            user_id=user_id,
        )
        if plan["framework_version_id"] != framework_version_id:
            raise not_found(
                "PRESENTATION_PLAN_NOT_FOUND",
                f"Presentation plan {presentation_plan_id} was not found",
            )
        return plan

    plan = store.get_latest_presentation_plan(
        framework_version_id=framework_version_id,
        user_id=user_id,
    )
    if plan is None:
        raise bad_request(
            "PRESENTATION_PLAN_NOT_FOUND",
            "Generate a presentation plan before generating the presentation",
        )
    return plan


def unimplemented_layouts_in_plan(plan_json: dict[str, Any]) -> list[str]:
    """Return unimplemented layout ids still sitting on the saved approved plan."""
    kept, skipped = filter_generatable_planned_slides(plan_json)
    planned = list(plan_json.get("slides") or [])
    if skipped or len(planned) != len(kept):
        return sorted(set(skipped)) or ["unknown"]
    return []


def _raise_if_plan_not_generatable(plan_json: dict[str, Any], *, as_http: bool) -> None:
    names = unimplemented_layouts_in_plan(plan_json)
    if not names:
        return
    message = (
        "Approved plan includes unimplemented layouts "
        f"({', '.join(names)}); regenerate the presentation plan before generating slides"
    )
    if as_http:
        raise bad_request("PRESENTATION_PLAN_NOT_GENERATABLE", message)
    raise RuntimeError(f"PRESENTATION_PLAN_NOT_GENERATABLE: {message}")


def _assert_plan_matches_generated_specs(
    plan_json: dict[str, Any],
    slide_specs: list[dict[str, Any]] | None,
) -> None:
    planned = list(plan_json.get("slides") or [])
    specs = list(slide_specs or [])
    if len(planned) != len(specs):
        raise RuntimeError(
            "AT-10: approved plan slide count "
            f"({len(planned)}) does not match generated SlideSpecs ({len(specs)}); "
            "regenerate the presentation plan so every planned layout is generatable"
        )
    for index, (planned_slide, spec) in enumerate(zip(planned, specs, strict=True), start=1):
        planned_id = planned_slide.get("layoutId")
        spec_id = spec.get("layoutId")
        if planned_id != spec_id:
            raise RuntimeError(
                f"AT-10: approved plan slide {index} is {planned_id} "
                f"but generated SlideSpec is {spec_id}"
            )


def _existing_plan_payload(job: job_service.Job) -> dict[str, Any]:
    enqueue = dict((job.result_json or {}).get("_enqueue") or {})
    plan_id = enqueue.get("presentation_plan_id") or (job.result_json or {}).get(
        "presentation_plan_id"
    )
    if plan_id:
        return {"id": UUID(str(plan_id))}
    return {"id": None}


def _enable_auto_continue_on_reused_job(
    store: DataStore,
    job: job_service.Job,
) -> job_service.Job:
    updated = job_service.update_job_auto_continue(
        job.id,
        True,
        repository=store,
    )
    if updated.status == JobStatus.COMPLETED:
        from app.services.presentation_pipeline import continue_after_planning

        continue_after_planning(store, planning_job_id=updated.id)
    return updated


def enqueue_presentation_plan_generate(
    store: DataStore,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    framework_version_id: UUID | None,
    auto_continue: bool = False,
    journey_stage: str | None = None,
):
    eligibility = require_startable_journey_stage(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
        journey_stage=journey_stage,
    )
    framework = _require_confirmed_framework(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
        framework_version_id=framework_version_id,
    )
    existing = job_service.reuse_active_generation_job(
        store,
        opportunity_id,
        stage_group="presentation",
        job_type="presentation_planning",
    )
    existing_enqueue = dict((existing.result_json or {}).get("_enqueue") or {}) if existing is not None else {}
    same_stage = str(existing_enqueue.get("journey_stage") or "first_contact") == str(
        eligibility["requested_journey_stage"]
    )
    same_prior = str(existing_enqueue.get("prior_stage_presentation_version_id") or "") == str(
        eligibility["prior_stage_presentation_version_id"] or ""
    )
    if (
        existing is not None
        and str(existing_enqueue.get("framework_version_id") or "") == str(framework["id"])
        and same_stage
        and same_prior
    ):
        if auto_continue:
            existing = _enable_auto_continue_on_reused_job(store, existing)
        return _existing_plan_payload(existing), existing, True

    plan_id = uuid.uuid4()
    job = job_service.create_job(
        opportunity_id=opportunity_id,
        job_type="presentation_planning",
        auto_continue=auto_continue,
        enqueue={
            "framework_version_id": str(framework["id"]),
            "user_id": str(user_id),
            "presentation_plan_id": str(plan_id),
            "journey_stage": eligibility["requested_journey_stage"],
            "prior_stage_presentation_version_id": (
                str(eligibility["prior_stage_presentation_version_id"])
                if eligibility["prior_stage_presentation_version_id"]
                else None
            ),
        },
        repository=store,
    )
    from app.worker import run_presentation_planning_task

    try:
        _dispatch_task(
            run_presentation_planning_task,
            str(job.id),
            str(framework["id"]),
            str(user_id),
            str(plan_id),
        )
    except Exception:
        # Eager memory-backend runs fail the job in-process; the HTTP handler
        # should still return the queued job so the UI can poll/retry.
        if settings.API_DATA_BACKEND != "memory":
            raise
        refreshed = job_service.get_job(job.id, repository=store)
        if refreshed is not None:
            job = refreshed
    return {"id": plan_id}, job, False


def execute_presentation_planning(
    store: DataStore,
    *,
    framework_version_id: UUID,
    user_id: UUID,
    presentation_plan_id: UUID,
) -> dict:
    install_runtime_stage_b_providers()
    framework = store.get_framework_version(
        framework_version_id=framework_version_id,
        user_id=user_id,
    )
    plan_json = plan_json_from_confirmed_framework(framework["framework_json"])
    return store.create_presentation_plan(
        framework_version_id=framework_version_id,
        user_id=user_id,
        plan_json=plan_json,
        presentation_plan_id=presentation_plan_id,
    )


def _existing_presentation_payload(
    store: DataStore,
    *,
    user_id: UUID,
    job: job_service.Job,
) -> tuple[dict[str, Any], dict[str, Any]]:
    presentation: dict[str, Any] = {"id": job.presentation_id}
    plan: dict[str, Any] = {"id": None}
    if job.presentation_id is None:
        return presentation, plan
    try:
        presentation = store.get_presentation(
            presentation_id=job.presentation_id,
            user_id=user_id,
        )
        plan = store.get_presentation_plan(
            presentation_plan_id=presentation["presentation_plan_id"],
            user_id=user_id,
        )
    except HTTPException:
        presentation = {"id": job.presentation_id}
        plan = {"id": None}
    return presentation, plan


def enqueue_presentation_generate(
    store: DataStore,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    framework_version_id: UUID | None,
    presentation_plan_id: UUID | None,
    name: str | None,
    journey_stage: str | None = None,
    enqueue_metadata: dict[str, Any] | None = None,
    on_enqueued: Callable[[dict[str, Any], job_service.Job], None] | None = None,
    existing_presentation_id: UUID | None = None,
):
    store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    framework = _require_confirmed_framework(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
        framework_version_id=framework_version_id,
    )
    eligibility = require_startable_journey_stage(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
        journey_stage=journey_stage,
    )
    plan: dict[str, Any] | None = None
    plan_error: HTTPException | None = None
    try:
        plan = _resolve_presentation_plan(
            store,
            opportunity_id=opportunity_id,
            user_id=user_id,
            framework_version_id=framework["id"],
            presentation_plan_id=presentation_plan_id,
        )
    except HTTPException as exc:
        plan_error = exc
    existing = job_service.reuse_active_generation_job(
        store,
        opportunity_id,
        stage_group="presentation",
    )
    if existing is not None:
        existing_enqueue = dict((existing.result_json or {}).get("_enqueue") or {})
        presentation, existing_plan = _existing_presentation_payload(
            store,
            user_id=user_id,
            job=existing,
        )
        existing_framework_id = (
            existing_enqueue.get("framework_version_id")
            or existing_plan.get("framework_version_id")
        )
        existing_plan_id = existing_enqueue.get("presentation_plan_id") or existing_plan.get("id")
        intended_plan_id = plan.get("id") if plan is not None else None
        same_requested_plan = str(existing_plan_id or "") == str(intended_plan_id or "")
        same_stage = str(existing_enqueue.get("journey_stage") or "first_contact") == str(
            eligibility["requested_journey_stage"]
        )
        same_prior = str(existing_enqueue.get("prior_stage_presentation_version_id") or "") == str(
            eligibility["prior_stage_presentation_version_id"] or ""
        )
        if (
            str(existing_framework_id or "") == str(framework["id"])
            and same_requested_plan
            and same_stage
            and same_prior
        ):
            return presentation, existing_plan, existing, True

    if plan is None:
        assert plan_error is not None
        raise plan_error

    if settings.RENDERER_EXECUTION_MODE == "live":
        _raise_if_plan_not_generatable(plan["plan_json"], as_http=True)
    presentation_name = name or str(plan["plan_json"].get("title") or "Presentation")
    if existing_presentation_id is not None:
        presentation = store.retarget_presentation_plan(
            presentation_id=existing_presentation_id,
            presentation_plan_id=plan["id"],
            user_id=user_id,
            name=presentation_name,
        )
    else:
        presentation = store.create_presentation(
            presentation_plan_id=plan["id"],
            user_id=user_id,
            name=presentation_name,
        )
    enqueue_payload = {
        "user_id": str(user_id),
        "presentation_id": str(presentation["id"]),
        "framework_version_id": str(framework["id"]),
        "presentation_plan_id": str(plan["id"]),
        "journey_stage": eligibility["requested_journey_stage"],
        "prior_stage_presentation_version_id": (
            str(eligibility["prior_stage_presentation_version_id"])
            if eligibility["prior_stage_presentation_version_id"]
            else None
        ),
    }
    enqueue_payload.update(enqueue_metadata or {})
    job = job_service.create_job(
        opportunity_id=opportunity_id,
        job_type="presentation_generation",
        presentation_id=presentation["id"],
        enqueue=enqueue_payload,
        repository=store,
    )
    if on_enqueued is not None:
        on_enqueued(presentation, job)
    from app.worker import run_presentation_generation_task

    _dispatch_task(
        run_presentation_generation_task,
        str(job.id),
        str(presentation["id"]),
        str(user_id),
    )
    return presentation, plan, job, False


def _require_approved_discovery_paper(
    store: DataStore,
    *,
    opportunity_id: UUID,
    user_id: UUID,
) -> None:
    """PPT #1 requires an approved Discovery Paper version before any deck side effect."""
    from app.services.discovery_paper import get_latest_approved_discovery_paper

    try:
        get_latest_approved_discovery_paper(
            store,
            opportunity_id=opportunity_id,
            user_id=user_id,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if exc.status_code == 404 and detail.get("code") == "DISCOVERY_PAPER_NOT_APPROVED":
            raise bad_request(
                "DISCOVERY_PAPER_APPROVAL_REQUIRED",
                "An approved Discovery Paper is required before PPT #1 can be generated.",
            ) from exc
        raise


def _stage1_presentation_id(opportunity: dict[str, Any]) -> UUID | None:
    presentation = (
        ((opportunity.get("stage1_outputs") or {}).get("outputs") or {}).get("presentation")
        or {}
    )
    raw = presentation.get("presentation_id")
    if not raw:
        return None
    return UUID(str(raw))


def _framework_for_plan_storage(
    store: DataStore,
    *,
    opportunity_id: UUID,
    user_id: UUID,
) -> dict[str, Any]:
    """Return a confirmed framework row to satisfy the plan foreign key.

    The row is not the PPT #1 content source. Discovery is not copied into it.
    """
    try:
        framework = store.get_latest_framework(
            opportunity_id=opportunity_id,
            user_id=user_id,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        framework = store.generate_framework_stub(
            opportunity_id=opportunity_id,
            user_id=user_id,
        )
    if framework["status"] != "confirmed":
        # Framework rows are immutable (migration 025); confirming appends a
        # confirmed successor version instead of patching the row in place.
        from app.services.framework_generation import confirm_framework

        framework = confirm_framework(
            store,
            opportunity_id=opportunity_id,
            user_id=user_id,
            framework_version_id=framework["id"],
        )
    return framework


def enqueue_first_contact_presentation_generate(
    store: DataStore,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    on_enqueued: Callable[[dict[str, Any], job_service.Job], None] | None = None,
):
    """Plan PPT #1 from the latest approved Discovery and render it internally.

    The pre-meeting deck is written by the Borek AI Tech deck generator
    (make_ai_tech_deck, at most 8 slides) from the approved Discovery pages.

    The first call allocates the Stage 1 presentation. A later call reuses that
    id and appends a version. An in-flight job for the same approved version is
    reused so a retry does not switch sources.
    """
    from app.services.discovery_paper import get_latest_approved_discovery_paper
    from app.services.stage_b_orchestration import get_live_planning_client
    from services.presentation.borek_deck.planner import plan_pre_meeting_deck
    from services.presentation.first_pitch import (
        planning_input_from_approved_paper,
        ppt1_generation_manifest,
    )
    from services.presentation.discovery_brief_adapter import is_opportunity_analysis
    from services.presentation.planner import PresentationPlanValidationError

    _require_approved_discovery_paper(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
    )
    approved = get_latest_approved_discovery_paper(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
    )
    if is_opportunity_analysis(approved.get("paper_json")):
        # Discovery v2 leads to the Master Presentation. It never falls back to the PPT #1 deck.
        return _enqueue_master_presentation_v1(
            store,
            opportunity_id=opportunity_id,
            user_id=user_id,
            approved=approved,
            on_enqueued=on_enqueued,
        )
    manifest = ppt1_generation_manifest(str(approved["id"]))
    existing = job_service.reuse_active_generation_job(
        store,
        opportunity_id,
        stage_group="presentation",
    )
    if existing is not None:
        existing_enqueue = dict((existing.result_json or {}).get("_enqueue") or {})
        existing_manifest = existing_enqueue.get("generation_source_manifest") or {}
        if (
            existing_enqueue.get("stage1_output_integration")
            and str(existing_manifest.get("approved_discovery_version_id"))
            == manifest["approved_discovery_version_id"]
        ):
            presentation, plan = _existing_presentation_payload(
                store,
                user_id=user_id,
                job=existing,
            )
            return presentation, plan, existing, True

    source = planning_input_from_approved_paper(approved)
    try:
        plan_json = plan_pre_meeting_deck(source, planner=get_live_planning_client())
    except PresentationPlanValidationError as exc:
        raise bad_request("PPT1_PLAN_INVALID", str(exc)) from exc
    framework = _framework_for_plan_storage(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
    )
    plan = store.create_presentation_plan(
        framework_version_id=framework["id"],
        user_id=user_id,
        plan_json=plan_json,
    )
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    return enqueue_presentation_generate(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
        framework_version_id=framework["id"],
        presentation_plan_id=plan["id"],
        name=str(plan["plan_json"]["title"]),
        journey_stage="first_contact",
        enqueue_metadata={
            "stage1_output_integration": True,
            "generation_source_manifest": manifest,
        },
        on_enqueued=on_enqueued,
        existing_presentation_id=_stage1_presentation_id(opportunity),
    )


def _master_presentation_id(store: DataStore, opportunity: dict[str, Any], opportunity_id: UUID) -> UUID | None:
    """The one Master Presentation of this opportunity, if a generation ever created it.

    The Stage 1 reference is the usual pointer; earlier generation jobs are the fallback, so a
    retry after a failure keeps the identity instead of creating a second presentation.
    """
    current = _stage1_presentation_id(opportunity)
    if current is not None:
        return current
    for row in _generation_jobs_for_opportunity(store, opportunity_id):
        manifest = _job_enqueue(row).get("generation_source_manifest") or {}
        if manifest.get("kind") == "master_presentation_v1" and row.get("presentation_id"):
            return UUID(str(row["presentation_id"]))
    return None


def _latest_master_v1_version(
    store: DataStore,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    presentation_id: UUID,
) -> dict[str, Any] | None:
    """The newest V1 version of the Master Presentation.

    The presentation also holds its V2 versions, so "the latest version" is not necessarily a
    V1; a pre-meeting request must compare with the newest V1, whatever was generated after it.
    """
    rows = [
        row
        for row in store.list_presentation_versions_for_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        if str(row.get("presentation_id")) == str(presentation_id)
        and isinstance(row.get("generation_source_manifest"), dict)
        and row["generation_source_manifest"].get("kind") == "master_presentation_v1"
    ]
    return max(rows, key=lambda row: int(row.get("version_number") or 0)) if rows else None


def _enqueue_master_presentation_v1(
    store: DataStore,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    approved: dict[str, Any],
    on_enqueued: Callable[[dict[str, Any], job_service.Job], None] | None,
):
    """Master Presentation V1: the canonical Borek deck plus an appendix from the approved analysis.

    Same opportunity, same master and same approved Discovery version always mean the same deck:
    a queued or running job is reused, a ready version is returned as it is, and only a missing
    or failed one is generated. Every generation appends a version to the one presentation.
    """
    from services.presentation.master_deck.appendix import AppendixPlanError
    from services.presentation.master_deck.plan import build_master_plan, generation_manifest, same_source
    from services.presentation.master_deck.registry import MasterDeckError

    if str(approved.get("opportunity_id")) != str(opportunity_id):
        raise bad_request(
            "DISCOVERY_PAPER_APPROVAL_REQUIRED",
            "The approved Discovery version does not belong to this opportunity.",
        )
    try:
        manifest = generation_manifest(approved)
    except (AppendixPlanError, MasterDeckError) as exc:
        raise bad_request(exc.code, str(exc)) from exc

    existing = job_service.reuse_active_generation_job(store, opportunity_id, stage_group="presentation")
    if existing is not None:
        existing_enqueue = dict((existing.result_json or {}).get("_enqueue") or {})
        if same_source(existing_enqueue.get("generation_source_manifest"), manifest):
            presentation, plan = _existing_presentation_payload(store, user_id=user_id, job=existing)
            return presentation, plan, existing, True

    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    presentation_id = _master_presentation_id(store, opportunity, opportunity_id)
    if presentation_id is not None:
        latest = _latest_master_v1_version(
            store,
            opportunity_id=opportunity_id,
            user_id=user_id,
            presentation_id=presentation_id,
        )
        if (
            latest is not None
            and _status_text(latest.get("status")) == "ready"
            and same_source(latest.get("generation_source_manifest"), manifest)
        ):
            presentation = store.get_presentation(presentation_id=presentation_id, user_id=user_id)
            plan = store.get_presentation_plan(
                presentation_plan_id=presentation["presentation_plan_id"], user_id=user_id
            )
            if _stage1_presentation_id(opportunity) is None:
                from app.services.journey_generation import update_stage1_presentation_state

                update_stage1_presentation_state(
                    store,
                    opportunity_id=opportunity_id,
                    user_id=user_id,
                    status="ready",
                    presentation_id=presentation_id,
                    code=None,
                )
            return presentation, plan, None, True

    try:
        plan_json = build_master_plan(approved)
    except (AppendixPlanError, MasterDeckError) as exc:
        raise bad_request(exc.code, str(exc)) from exc
    framework = _framework_for_plan_storage(store, opportunity_id=opportunity_id, user_id=user_id)
    plan = store.create_presentation_plan(
        framework_version_id=framework["id"],
        user_id=user_id,
        plan_json=plan_json,
    )
    return enqueue_presentation_generate(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
        framework_version_id=framework["id"],
        presentation_plan_id=plan["id"],
        name=str(plan["plan_json"]["title"]),
        journey_stage="first_contact",
        enqueue_metadata={
            "stage1_output_integration": True,
            "generation_source_manifest": manifest,
        },
        on_enqueued=on_enqueued,
        existing_presentation_id=presentation_id,
    )


_ACTIVE_PPT2_JOB_STATUSES = {JobStatus.QUEUED.value, JobStatus.RUNNING.value}


def _status_text(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _job_enqueue(row: dict[str, Any]) -> dict[str, Any]:
    result = row.get("result_json") or {}
    enqueue = result.get("_enqueue") if isinstance(result, dict) else None
    return dict(enqueue or {})


def _generation_jobs_for_opportunity(store: DataStore, opportunity_id: UUID) -> list[dict[str, Any]]:
    lister = getattr(store, "list_generation_jobs_for_opportunity", None)
    if lister is None:
        return []
    return [row for row in lister(opportunity_id) if isinstance(row, dict)]


def _ppt2_job_presentation_id(row: dict[str, Any]) -> str:
    return str(row.get("presentation_id") or _job_enqueue(row).get("presentation_id") or "")


def _is_ppt2_generation_job(row: dict[str, Any], *, stage1_id: str | None) -> bool:
    if str(row.get("job_type") or "") != "presentation_generation":
        return False
    if _job_enqueue(row).get("journey_stage") != "post_meeting":
        return False
    presentation_id = _ppt2_job_presentation_id(row)
    if not presentation_id:
        return False
    return stage1_id is None or presentation_id != stage1_id


def _ppt2_identity_ids(
    store: DataStore,
    *,
    opportunity: dict[str, Any],
    opportunity_id: UUID,
    user_id: UUID,
) -> list[str]:
    """Distinct PPT #2 presentation ids. Order is not a selection."""
    stage1 = _stage1_presentation_id(opportunity)
    stage1_text = None if stage1 is None else str(stage1)
    identities: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "")
        if not text or text == str(stage1_text or ""):
            return
        if text not in identities:
            identities.append(text)

    for row in store.list_presentation_versions_for_opportunity(
        opportunity_id=opportunity_id,
        user_id=user_id,
    ):
        if row.get("journey_stage") == "post_meeting":
            add(row.get("presentation_id"))
    for row in _generation_jobs_for_opportunity(store, opportunity_id):
        if _is_ppt2_generation_job(row, stage1_id=stage1_text):
            add(_ppt2_job_presentation_id(row))
    return identities


def _ppt2_http_error(code: str, message: str, *, detail: dict[str, Any] | None = None) -> HTTPException:
    payload: dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        payload["detail"] = detail
    return HTTPException(status_code=400, detail=payload)


def _public_ppt2_job_response(row: dict[str, Any], *, existing: bool) -> dict[str, Any]:
    result = row.get("result_json") if isinstance(row.get("result_json"), dict) else {}
    enqueue = _job_enqueue(row)
    manifest = enqueue.get("generation_source_manifest")
    if not isinstance(manifest, dict):
        manifest = result.get("generation_source_manifest")
    return {
        "presentation_id": _ppt2_job_presentation_id(row),
        "presentation_version_id": result.get("presentation_version_id"),
        "status": _status_text(row.get("status")),
        "journey_stage": "post_meeting",
        "generation_source_manifest": manifest if isinstance(manifest, dict) else None,
        "job_id": str(row["id"]),
        "is_existing_job": existing,
    }


def _initial_ppt2_gate(
    store: DataStore,
    *,
    opportunity: dict[str, Any],
    opportunity_id: UUID,
    user_id: UUID,
) -> dict[str, Any] | None:
    """Refuse a second PPT #2 identity. Reuse one in-progress job when that is the only identity."""
    identities = _ppt2_identity_ids(
        store,
        opportunity=opportunity,
        opportunity_id=opportunity_id,
        user_id=user_id,
    )
    if not identities:
        return None
    if len(identities) > 1:
        raise _ppt2_http_error(
            "PPT2_IDENTITY_AMBIGUOUS",
            "More than one PPT #2 presentation exists. This request will not choose one. "
            "Regenerate a specific presentation id.",
        )
    presentation_id = identities[0]
    versions = [
        row
        for row in store.list_presentation_versions_for_opportunity(
            opportunity_id=opportunity_id,
            user_id=user_id,
        )
        if row.get("journey_stage") == "post_meeting"
        and str(row.get("presentation_id")) == presentation_id
    ]
    stage1 = _stage1_presentation_id(opportunity)
    jobs = [
        row
        for row in _generation_jobs_for_opportunity(store, opportunity_id)
        if _is_ppt2_generation_job(row, stage1_id=None if stage1 is None else str(stage1))
        and _ppt2_job_presentation_id(row) == presentation_id
    ]
    active = [row for row in jobs if _status_text(row.get("status")) in _ACTIVE_PPT2_JOB_STATUSES]
    if versions or not active:
        raise _ppt2_http_error(
            "PPT2_ALREADY_EXISTS",
            "PPT #2 already exists. Further content updates must regenerate "
            f"POST /opportunities/{opportunity_id}/ppt2/{presentation_id}/regenerate.",
            detail={
                "presentation_id": presentation_id,
                "regenerate_path": f"/opportunities/{opportunity_id}/ppt2/{presentation_id}/regenerate",
            },
        )
    active.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")))
    return _public_ppt2_job_response(active[0], existing=True)


def enqueue_post_meeting_presentation_generate(
    store: DataStore,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    presentation_id: UUID | None = None,
) -> dict[str, Any]:
    """Plan PPT #2 from the BT-46 context frozen at this request.

    The post-meeting deck is written by the Borek Master deck generator
    (make_master_deck) from the four frozen sources, kept separate
    (see services.presentation.post_meeting).

    The worker consumes that frozen input. It does not call build_ppt2_context
    again. This path does not use journey-stage eligibility and does not write
    the Stage 1 presentation pointer. A second initial generate does not create
    another presentation.
    """
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    if presentation_id is None:
        existing = _initial_ppt2_gate(
            store,
            opportunity=opportunity,
            opportunity_id=opportunity_id,
            user_id=user_id,
        )
        if existing is not None:
            return existing
    from app.services.ppt2_context import build_ppt2_context
    from app.services.stage_b_orchestration import get_live_planning_client
    from services.presentation.borek_deck.planner import plan_post_meeting_deck
    from services.presentation.planner import PresentationPlanValidationError
    from services.presentation.post_meeting import (
        Ppt2FindingSourcesError,
        planning_input_from_ppt2_context,
        ppt2_generation_manifest,
    )

    context = build_ppt2_context(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
    )
    discovery = context["sources"]["approved_discovery"]
    if discovery.get("status") != "available":
        raise bad_request(
            "DISCOVERY_PAPER_APPROVAL_REQUIRED",
            "An approved Discovery Paper is required before PPT #2 can be generated.",
        )
    planner_input = planning_input_from_ppt2_context(context)
    manifest = ppt2_generation_manifest(context)
    transcript_summary = _ppt2_transcript_summary(
        store,
        context=context,
        opportunity_id=opportunity_id,
        user_id=user_id,
    )
    if transcript_summary is not None:
        planner_input["transcript_summary"] = transcript_summary
    try:
        plan_json = plan_post_meeting_deck(
            planner_input,
            planner=get_live_planning_client(),
            ppt1_slides=_ppt1_planned_slides(store, opportunity=opportunity, user_id=user_id),
        )
    except PresentationPlanValidationError as exc:
        raise bad_request("PPT2_PLAN_INVALID", str(exc)) from exc
    except Ppt2FindingSourcesError as exc:
        # Fail closed: nothing is planned, stored or rendered; earlier versions are unchanged.
        raise conflict(exc.code, exc.user_message) from exc
    framework = _framework_for_plan_storage(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
    )
    plan = store.create_presentation_plan(
        framework_version_id=framework["id"],
        user_id=user_id,
        plan_json=plan_json,
    )
    presentation = _ppt2_presentation(
        store,
        opportunity=opportunity,
        opportunity_id=opportunity_id,
        user_id=user_id,
        presentation_id=presentation_id,
        plan_id=plan["id"],
        name=str(plan["plan_json"]["title"]),
    )
    enqueue_payload = {
        "user_id": str(user_id),
        "presentation_id": str(presentation["id"]),
        "framework_version_id": str(framework["id"]),
        "presentation_plan_id": str(plan["id"]),
        "journey_stage": "post_meeting",
        "prior_stage_presentation_version_id": None,
        "generation_source_manifest": manifest,
        "ppt2_generation_input": planner_input,
    }
    job = job_service.create_job(
        opportunity_id=opportunity_id,
        job_type="presentation_generation",
        presentation_id=presentation["id"],
        enqueue=enqueue_payload,
        repository=store,
    )
    from app.worker import run_presentation_generation_task

    _dispatch_task(
        run_presentation_generation_task,
        str(job.id),
        str(presentation["id"]),
        str(user_id),
    )
    refreshed = job_service.get_job(job.id, repository=store) or job
    result = refreshed.result_json or {}
    return {
        "presentation_id": str(presentation["id"]),
        "presentation_version_id": result.get("presentation_version_id"),
        "status": refreshed.status.value if hasattr(refreshed.status, "value") else str(refreshed.status),
        "journey_stage": "post_meeting",
        "generation_source_manifest": manifest,
        "job_id": str(refreshed.id),
        "is_existing_job": False,
    }


def _ppt2_transcript_summary(
    store: DataStore,
    *,
    context: dict[str, Any],
    opportunity_id: UUID,
    user_id: UUID,
) -> dict[str, Any] | None:
    """BT-36 summary of the transcript the meeting extraction was built from (None when absent)."""
    from services.transcript.summarize import summarize_speaker_sections

    extraction = context["sources"]["meeting_extraction"]
    transcript_id = extraction.get("transcript_id")
    if extraction.get("status") != "available" or not transcript_id:
        return None
    source = next(
        (
            row
            for row in store.list_transcript_sources(opportunity_id=opportunity_id, user_id=user_id)
            if str(row["id"]) == str(transcript_id)
        ),
        None,
    )
    if not source or not source.get("sections"):
        return None
    return summarize_speaker_sections(
        source["sections"],
        opportunity_id=opportunity_id,
        transcript_id=transcript_id,
    )


def _ppt1_planned_slides(
    store: DataStore,
    *,
    opportunity: dict[str, Any],
    user_id: UUID,
) -> list[dict[str, Any]] | None:
    """The slides of the latest PPT #1 version, so PPT #2 can start with them (None when there is none)."""
    from services.presentation.borek_deck.deck_plan import planned_slides_from_specs

    stage1 = _stage1_presentation_id(opportunity)
    if stage1 is None:
        return None
    try:
        version = store.get_latest_presentation_version(presentation_id=stage1, user_id=user_id)
    except HTTPException:
        return None
    if isinstance(version.get("generation_source_manifest"), dict) and version[
        "generation_source_manifest"
    ].get("kind") in ("master_presentation_v1", "master_presentation_v2"):
        # A Master Presentation is not a PPT #1 deck; the legacy post-meeting deck does not
        # extend it (the Master Presentation gets its own second version later).
        return None
    return planned_slides_from_specs(version.get("slides_json")) or None


def _ppt2_presentation(
    store: DataStore,
    *,
    opportunity: dict[str, Any],
    opportunity_id: UUID,
    user_id: UUID,
    presentation_id: UUID | None,
    plan_id: UUID,
    name: str,
) -> dict[str, Any]:
    if presentation_id is None:
        return store.create_presentation(
            presentation_plan_id=plan_id,
            user_id=user_id,
            name=name,
        )
    stage1_id = _stage1_presentation_id(opportunity)
    if stage1_id is not None and presentation_id == stage1_id:
        raise bad_request(
            "PPT2_MUST_NOT_USE_STAGE1_PRESENTATION",
            "PPT #2 cannot use the Stage 1 presentation.",
        )
    existing = store.get_presentation(presentation_id=presentation_id, user_id=user_id)
    owner_id = _presentation_opportunity_id(
        store,
        presentation=existing,
        user_id=user_id,
    )
    if owner_id != opportunity_id:
        raise not_found(
            "PRESENTATION_NOT_FOUND",
            f"Presentation {presentation_id} was not found",
        )
    versions = [
        row
        for row in store.list_presentation_versions_for_opportunity(
            opportunity_id=opportunity_id,
            user_id=user_id,
        )
        if str(row.get("presentation_id")) == str(presentation_id)
    ]
    if any(row.get("journey_stage") not in (None, "post_meeting") for row in versions):
        raise bad_request(
            "PPT2_PRESENTATION_REQUIRED",
            "PPT #2 regenerate must target the existing post-meeting presentation.",
        )
    return store.retarget_presentation_plan(
        presentation_id=presentation_id,
        presentation_plan_id=plan_id,
        user_id=user_id,
        name=name,
    )


def _presentation_opportunity_id(
    store: DataStore,
    *,
    presentation: dict[str, Any],
    user_id: UUID,
) -> UUID:
    plan = store.get_presentation_plan(
        presentation_plan_id=presentation["presentation_plan_id"],
        user_id=user_id,
    )
    framework = store.get_framework_version(
        framework_version_id=plan["framework_version_id"],
        user_id=user_id,
    )
    return framework["opportunity_id"]


def execute_presentation_generation(
    store: DataStore,
    *,
    presentation_id: UUID,
    user_id: UUID,
    journey_stage: str | None = None,
    prior_stage_presentation_version_id: UUID | None = None,
    discovery_pages: list[dict[str, Any]] | None = None,
    generation_source_manifest: dict[str, Any] | None = None,
    ppt2_generation_input: dict[str, Any] | None = None,
) -> tuple[dict, dict]:
    install_runtime_stage_b_providers()
    presentation = store.get_presentation(presentation_id=presentation_id, user_id=user_id)
    plan = store.get_presentation_plan(
        presentation_plan_id=presentation["presentation_plan_id"],
        user_id=user_id,
    )
    # PPT #1 / PPT #2 written by the Borek deck generator carry their finished slides in the
    # plan, so the source bodies are not needed to build the SlideSpecs. The frozen sources
    # stay on the job (manifest + ppt2_generation_input) for provenance and retries.
    borek_plan = is_borek_plan(plan["plan_json"])
    if (
        not borek_plan
        and _plan_uses_discovery(plan["plan_json"])
        and discovery_pages is None
        and ppt2_generation_input is None
    ):
        raise RuntimeError(
            "PPT1_DISCOVERY_SOURCE_MISSING: approved Discovery pages were not "
            "loaded for this PPT #1 generation"
        )
    if (
        isinstance(generation_source_manifest, dict)
        and generation_source_manifest.get("kind") == "ppt2"
        and ppt2_generation_input is None
    ):
        raise RuntimeError(
            "PPT2_GENERATION_INPUT_MISSING: frozen PPT #2 context was not "
            "loaded for this generation"
        )
    if settings.RENDERER_EXECUTION_MODE == "live":
        _raise_if_plan_not_generatable(plan["plan_json"], as_http=False)
    if (
        isinstance(generation_source_manifest, dict)
        and generation_source_manifest.get("kind") == "master_presentation_v2"
        and (plan["plan_json"].get("appendix") or {}).get("source_hash") != generation_source_manifest.get("snapshot_hash")
    ):
        # The version must be built from the plan that was frozen with this job, never from a later one.
        raise RuntimeError(
            "MASTER_V2_SNAPSHOT_INVALID: the stored plan does not belong to the frozen sources of this generation"
        )
    version = store.create_presentation_version_with_slides(
        presentation_id=presentation_id,
        user_id=user_id,
        plan_json=plan["plan_json"],
        journey_stage=journey_stage,
        prior_stage_presentation_version_id=prior_stage_presentation_version_id,
        discovery_pages=None if borek_plan else discovery_pages,
        generation_source_manifest=generation_source_manifest,
        ppt2_generation_input=None if borek_plan else ppt2_generation_input,
    )
    return version, plan


def _plan_uses_discovery(plan_json: dict[str, Any]) -> bool:
    slides = list(plan_json.get("slides") or [])
    if not slides:
        return False
    return all(
        all(str(reference).startswith("discovery.") for reference in slide.get("frameworkReferences") or [])
        for slide in slides
    )


def load_ppt1_discovery_pages(
    store: DataStore,
    *,
    user_id: UUID,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """Load the Discovery body frozen on the generation job, not the latest draft."""
    from services.presentation.first_pitch import planning_input_from_approved_paper

    version_id = manifest.get("approved_discovery_version_id")
    if not version_id:
        raise RuntimeError("PPT1_DISCOVERY_SOURCE_MISSING: generation manifest has no version id")
    row = store.get_discovery_paper_version(
        version_id=UUID(str(version_id)),
        user_id=user_id,
    )
    if row.get("status") != "approved":
        raise RuntimeError(
            "PPT1_DISCOVERY_SOURCE_MISSING: frozen Discovery version is not approved"
        )
    return list(planning_input_from_approved_paper(row)["pages"])


def load_presentation_generation_checkpoint(
    store: DataStore,
    *,
    presentation_id: UUID,
    user_id: UUID,
) -> tuple[dict, dict]:
    """Load slides already persisted by an earlier successful generation stage."""
    presentation = store.get_presentation(
        presentation_id=presentation_id,
        user_id=user_id,
    )
    plan = store.get_presentation_plan(
        presentation_plan_id=presentation["presentation_plan_id"],
        user_id=user_id,
    )
    version = store.get_latest_presentation_version(
        presentation_id=presentation_id,
        user_id=user_id,
    )
    if version is None:
        raise RuntimeError(
            "PRESENTATION_CHECKPOINT_NOT_FOUND: "
            f"presentation {presentation_id} has no persisted version to resume"
        )
    return version, plan


def render_presentation_version(
    store: DataStore,
    *,
    version: dict,
    plan: dict,
) -> dict:
    # A Master Presentation is assembled in-process from the canonical deck; it is always
    # rendered for real, so no mode can make a placeholder deck look like a finished one.
    if settings.RENDERER_EXECUTION_MODE != "live" and not is_master_plan(plan["plan_json"]):
        return version
    _assert_plan_matches_generated_specs(plan["plan_json"], version.get("slides_json"))
    if is_borek_plan(plan["plan_json"]):
        # PPT #1 (Ai Tech deck) and PPT #2 (Master deck) are rendered by the Borek generator.
        assets = render_borek_deck_assets(
            version_id=version["id"],
            slide_specs=version["slides_json"],
            deck_kind=str(plan["plan_json"].get("deck_kind") or PRE_MEETING),
        )
    else:
        assets = render_deck_assets(
            version_id=version["id"],
            presentation_plan=plan["plan_json"],
            slide_specs=version["slides_json"],
        )
    updated = store.update_presentation_version_assets(
        presentation_version_id=version["id"],
        assets=assets,
        status="ready",
    )
    updated["storage_size_bytes"] = int(assets.get("storage_size_bytes") or 0)
    return updated


def _reject_borek_deck_slide_edit(
    store: DataStore,
    *,
    presentation_id: UUID,
    user_id: UUID,
) -> None:
    """Per-slide regenerate / change-layout run the registered layout generators.

    PPT #1 and PPT #2 are written by the Borek deck generator in one planning step, so
    they are changed by regenerating the whole deck, not one slide.
    """
    presentation = store.get_presentation(presentation_id=presentation_id, user_id=user_id)
    plan = store.get_presentation_plan(
        presentation_plan_id=presentation["presentation_plan_id"],
        user_id=user_id,
    )
    if is_borek_plan(plan["plan_json"]):
        raise bad_request(
            "BOREK_DECK_SLIDE_EDIT_UNSUPPORTED",
            "This deck is generated as a whole. Regenerate the deck instead of "
            "regenerating or re-laying out a single slide.",
        )


def enqueue_slide_regenerate(
    store: DataStore,
    *,
    presentation_id: UUID,
    slide_id: UUID,
    user_id: UUID,
):
    _reject_borek_deck_slide_edit(store, presentation_id=presentation_id, user_id=user_id)
    opportunity_id = store.get_presentation_opportunity_id(
        presentation_id=presentation_id,
        user_id=user_id,
    )
    slide = store.get_slide(
        presentation_id=presentation_id,
        slide_id=slide_id,
        user_id=user_id,
    )
    job = job_service.create_job(
        opportunity_id=opportunity_id,
        job_type="slide_regenerate",
        presentation_id=presentation_id,
        enqueue={
            "user_id": str(user_id),
            "presentation_id": str(presentation_id),
            "slide_id": str(slide_id),
        },
        repository=store,
    )
    from app.worker import run_slide_regenerate_task

    _dispatch_task(
        run_slide_regenerate_task,
        str(job.id),
        str(presentation_id),
        str(slide_id),
        str(user_id),
    )
    return slide, job


def execute_slide_regenerate(
    store: DataStore,
    *,
    presentation_id: UUID,
    slide_id: UUID,
    user_id: UUID,
) -> dict:
    install_runtime_stage_b_providers()
    return store.regenerate_slide(
        presentation_id=presentation_id,
        slide_id=slide_id,
        user_id=user_id,
    )


def enqueue_slide_change_layout(
    store: DataStore,
    *,
    presentation_id: UUID,
    slide_id: UUID,
    user_id: UUID,
    layout_id: str,
):
    _reject_borek_deck_slide_edit(store, presentation_id=presentation_id, user_id=user_id)
    if layout_id not in VALID_LAYOUT_IDS:
        raise bad_request(
            "INVALID_LAYOUT_ID",
            f"layout_id must be a registered layout; got {layout_id}",
        )
    current = store.get_slide(
        presentation_id=presentation_id,
        slide_id=slide_id,
        user_id=user_id,
    )
    current_category = LAYOUT_REGISTRY[current["layout_id"]]["category"]
    target_category = LAYOUT_REGISTRY[layout_id]["category"]
    if current_category != target_category:
        raise bad_request(
            "LAYOUT_CATEGORY_MISMATCH",
            "A slide can only change to another layout in the same category",
        )
    opportunity_id = store.get_presentation_opportunity_id(
        presentation_id=presentation_id,
        user_id=user_id,
    )
    slide = current
    job = job_service.create_job(
        opportunity_id=opportunity_id,
        job_type="slide_change_layout",
        presentation_id=presentation_id,
        enqueue={
            "user_id": str(user_id),
            "presentation_id": str(presentation_id),
            "slide_id": str(slide_id),
            "layout_id": layout_id,
        },
        repository=store,
    )
    from app.worker import run_slide_change_layout_task

    _dispatch_task(
        run_slide_change_layout_task,
        str(job.id),
        str(presentation_id),
        str(slide_id),
        str(user_id),
        layout_id,
    )
    return slide, job


def execute_slide_change_layout(
    store: DataStore,
    *,
    presentation_id: UUID,
    slide_id: UUID,
    user_id: UUID,
    layout_id: str,
) -> dict:
    install_runtime_stage_b_providers()
    return store.change_slide_layout(
        presentation_id=presentation_id,
        slide_id=slide_id,
        user_id=user_id,
        layout_id=layout_id,
    )
