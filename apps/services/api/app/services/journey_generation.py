"""BT-36 / TSK-013 fixture-backed Stage 1/2 outputs and email drafts."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import jsonschema

from app.config import settings
from app.services.api_errors import bad_request, conflict
from app.services.first_contact_inputs import require_first_contact_client_documents
from app.services.knowledge_access import resolve_active_corpus
from app.services import presentation_generation
from app.services.stage1 import get_company_research_provider
from services.framework.stage1_intake import resolve_meeting_purpose
from services.framework.stage1_research import generate_stage1_research
from services.followup.extraction import FollowupExtractionError, extract_followup
from services.followup.rendering import render_first_contact_draft, render_three_lengths
from services.followup.summary_extraction import default_meeting_date, extract_followup_from_summary
from services.transcript.summarize import (
    PROMPT_VERSION as TRANSCRIPT_SUMMARY_PROMPT_VERSION,
    format_transcript_summary_for_prompt,
    summarize_speaker_sections,
)

CONTRACTS = Path(__file__).resolve().parents[5] / "packages" / "contracts"
JOURNEY_STAGES = ("first_contact", "deepening", "concretisation")
QUESTION_TEMPLATES = (
    "What problem does {topic} currently create for the operating teams?",
    "Who owns {topic} decisions today, and who must be in the first meeting?",
    "Which systems or vendors sit next to {topic} in the current process?",
    "What does a successful first meeting on {topic} look like for you?",
    "Where does {topic} already work well, and where does it fail?",
    "What evidence or KPIs do you already collect around {topic}?",
    "Which constraints (security, works council, budget cycle) apply to {topic}?",
    "What happens if {topic} is not improved in the next 12 months?",
    "Which neighbouring processes must stay unchanged if we discuss {topic}?",
    "What have you already tried for {topic}, and what did you learn?",
    "How should Borek prepare for a first conversation about {topic}?",
    "What would you want a first-meeting agenda on {topic} to cover first?",
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _schema(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _validate(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    jsonschema.Draft202012Validator(_schema(name)).validate(payload)
    return payload


def empty_stage1(opportunity_id: UUID) -> dict[str, Any]:
    return _validate(
        "stage1_outputs.schema.json",
        {
            "schema_version": "1.0",
            "opportunity_id": str(opportunity_id),
            "status": "not_generated",
            "outputs": None,
        },
    )


def empty_stage2(opportunity_id: UUID) -> dict[str, Any]:
    return _validate(
        "stage2_outputs.schema.json",
        {
            "schema_version": "1.0",
            "opportunity_id": str(opportunity_id),
            "status": "not_generated",
            "outputs": None,
        },
    )


def empty_email(opportunity_id: UUID, journey_stage: str) -> dict[str, Any]:
    return _validate(
        "email_draft.schema.json",
        {
            "schema_version": "1.0",
            "opportunity_id": str(opportunity_id),
            "journey_stage": journey_stage,
            "draft": None,
        },
    )


def empty_client_preparation_email(opportunity_id: UUID) -> dict[str, Any]:
    return _validate(
        "client_preparation_email.schema.json",
        {
            "schema_version": "1.0",
            "opportunity_id": str(opportunity_id),
            "status": "not_generated",
            "email": None,
        },
    )


def client_preparation_envelope(opportunity_id: UUID, stored: dict[str, Any] | None) -> dict[str, Any]:
    if not stored:
        return empty_client_preparation_email(opportunity_id)
    return _validate(
        "client_preparation_email.schema.json",
        {
            "schema_version": "1.0",
            "opportunity_id": str(opportunity_id),
            "status": "ready",
            "email": stored,
        },
    )


def generate_client_preparation_email(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
) -> dict[str, Any]:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    stage1 = opportunity.get("stage1_outputs") or {}
    if stage1.get("status") != "ready" or not stage1.get("outputs"):
        raise bad_request(
            "STAGE1_OUTPUTS_REQUIRED",
            "Generate the meeting brief before creating the client preparation email.",
        )
    statics = _require_followup_statics(opportunity)
    name = str(opportunity.get("opportunity_name") or "this opportunity")
    topic = resolve_meeting_purpose(opportunity) or name
    meeting_date = datetime.now(UTC).strftime("%d.%m.%Y")
    lengths = render_first_contact_draft(statics, topic=topic, meeting_date=meeting_date)
    medium = lengths["medium"]
    email = {
        "subject": medium["subject"],
        "body": medium["body"],
        "word_count": medium["word_count"],
        "generated_at": _now(),
    }
    store.update_opportunity(
        opportunity_id=opportunity_id,
        user_id=user_id,
        updates={"client_preparation_email": email},
    )
    return client_preparation_envelope(opportunity_id, email)


def get_meeting_feedback(opportunity: dict[str, Any]) -> dict[str, Any]:
    updated = opportunity.get("meeting_feedback_updated_at")
    if hasattr(updated, "isoformat"):
        updated = updated.isoformat().replace("+00:00", "Z")
    return _validate(
        "meeting_feedback.schema.json",
        {
            "schema_version": "1.0",
            "opportunity_id": str(opportunity["id"]),
            "text": opportunity.get("meeting_feedback_text"),
            "updated_at": updated,
        },
    )


def _stage1_presentation_update(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    status: str,
    presentation_id: UUID | str | None,
    code: str | None,
) -> dict[str, Any] | None:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    envelope = opportunity.get("stage1_outputs")
    if not isinstance(envelope, dict) or not isinstance(envelope.get("outputs"), dict):
        return None
    payload = json.loads(json.dumps(envelope, default=str))
    payload["outputs"]["presentation"] = {
        "status": status,
        "profile": presentation_generation.FIRST_CONTACT_PRESENTATION_PROFILE,
        "code": code,
        "presentation_id": str(presentation_id) if presentation_id else None,
        "download_url": None,
    }
    validated = _validate("stage1_outputs.schema.json", payload)
    store.update_opportunity(
        opportunity_id=opportunity_id,
        user_id=user_id,
        updates={"stage1_outputs": validated},
    )
    return validated


def update_stage1_presentation_state(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    status: str,
    presentation_id: UUID | str | None,
    code: str | None = None,
) -> dict[str, Any] | None:
    """Worker-safe terminal/intermediate update for the Stage 1 presentation reference."""
    return _stage1_presentation_update(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
        status=status,
        presentation_id=presentation_id,
        code=code,
    )


def generate_stage1_outputs(store: Any, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    existing = opportunity.get("stage1_outputs") or {}
    existing_presentation = ((existing.get("outputs") or {}).get("presentation") or {})
    if (
        existing.get("status") == "ready"
        and existing_presentation.get("presentation_id")
        and existing_presentation.get("status") in {"queued", "generating", "ready"}
    ):
        return existing
    client_document_sources = require_first_contact_client_documents(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
    )
    research = generate_stage1_research(
        opportunity,
        corpus=resolve_active_corpus(store),
        provider=get_company_research_provider(),
        use_llm=settings.AI_EXECUTION_MODE == "live",
        client_document_sources=client_document_sources,
    )
    topic = resolve_meeting_purpose(opportunity) or "this engagement"
    questions = [
        {"id": f"Q{index + 1}", "text": template.format(topic=topic)}
        for index, template in enumerate(QUESTION_TEMPLATES[:12])
    ]
    envelope = {
        "schema_version": "1.0",
        "opportunity_id": str(opportunity_id),
        "status": "ready",
        "outputs": {
            "hypothesis": {
                "statement": str(research.get("hypothesis", {}).get("text") or ""),
                "origin": "AI_HYPOTHESIS",
            },
            "product_relevance": {
                "statement": str(research.get("product_relevance", {}).get("text") or ""),
                "origin": "AI_HYPOTHESIS",
            },
            "discovery_questions": questions,
            "use_cases": [
                {
                    "title": "Past Borek use cases",
                    "rationale": "No approved use-case match is stored for this opportunity yet.",
                    "availability": "unknown",
                }
            ],
            "agenda": {
                "title": f"First meeting agenda — {topic}",
                "items": [
                    {"order": 1, "label": "Confirm the sales topic and desired outcomes"},
                    {"order": 2, "label": "Walk the discovery questions grounded in uploaded client documents"},
                    {"order": 3, "label": "Agree next step; no invented commercial terms"},
                ],
            },
            "presentation": {
                "status": "unfrozen",
                "profile": "first_meeting_3",
                "code": "FIRST_MEETING_PPT_PROFILE_UNFROZEN",
                "presentation_id": None,
                "download_url": None,
            },
            "research": research,
            "generated_at": _now(),
        },
    }
    payload = _validate("stage1_outputs.schema.json", envelope)
    store.update_opportunity(
        opportunity_id=opportunity_id,
        user_id=user_id,
        updates={"stage1_outputs": payload},
    )

    queued_presentation_id: UUID | str | None = None

    def mark_queued(presentation: dict[str, Any], _job: Any) -> None:
        nonlocal queued_presentation_id
        queued_presentation_id = presentation["id"]
        _stage1_presentation_update(
            store,
            opportunity_id=opportunity_id,
            user_id=user_id,
            status="queued",
            presentation_id=presentation["id"],
            code="FIRST_MEETING_PRESENTATION_QUEUED",
        )

    try:
        presentation_generation.enqueue_first_contact_presentation_generate(
            store,
            opportunity_id=opportunity_id,
            user_id=user_id,
            on_enqueued=mark_queued,
        )
    except Exception as exc:
        logging.getLogger(__name__).exception("PPT #1 enqueue failed: %s", exc)
        detail = getattr(exc, "detail", None)
        eligibility_code = detail.get("code") if isinstance(detail, dict) else None
        _stage1_presentation_update(
            store,
            opportunity_id=opportunity_id,
            user_id=user_id,
            status="failed",
            presentation_id=queued_presentation_id,
            code=str(
                eligibility_code
                or getattr(exc, "code", None)
                or "PRESENTATION_GENERATION_FAILED"
            ),
        )
        if eligibility_code == "DISCOVERY_PAPER_APPROVAL_REQUIRED":
            raise
    refreshed = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    return refreshed.get("stage1_outputs") or payload


def regenerate_stage1_presentation(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
) -> dict[str, Any]:
    """Explicit PPT #1 regenerate: latest approved Discovery, same Stage 1 id."""

    def mark_queued(presentation: dict[str, Any], _job: Any) -> None:
        _stage1_presentation_update(
            store,
            opportunity_id=opportunity_id,
            user_id=user_id,
            status="queued",
            presentation_id=presentation["id"],
            code="FIRST_MEETING_PRESENTATION_QUEUED",
        )

    presentation_generation.enqueue_first_contact_presentation_generate(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
        on_enqueued=mark_queued,
    )
    refreshed = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    return refreshed.get("stage1_outputs") or empty_stage1(opportunity_id)


def generate_stage2_outputs(store: Any, *, opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    sources = store.list_transcript_sources(opportunity_id=opportunity_id, user_id=user_id)
    if not sources:
        raise bad_request(
            "TRANSCRIPT_REQUIRED",
            "Upload at least one meeting transcript before generating Deepening outputs.",
        )
    source = sources[-1]
    summary = summarize_speaker_sections(
        source.get("sections") or [],
        opportunity_id=opportunity_id,
        transcript_id=source["id"],
    )
    feedback = opportunity.get("meeting_feedback_text")
    call_summary_parts = [summary.get("narrative") or "No narrative facts were extracted from the transcript."]
    if feedback:
        call_summary_parts.append("Sales meeting feedback: " + str(feedback).strip())
    mom = {
        "title": f"Minutes — {opportunity.get('opportunity_name')}",
        "participants": list(summary.get("participants") or []),
        "decisions": list(summary.get("decisions") or []),
        "action_items": [
            str(item.get("text") or "").strip()
            for item in summary.get("action_items") or []
            if str(item.get("text") or "").strip()
        ],
        "open_questions": list(summary.get("open_questions") or []),
        "meeting_feedback": feedback,
    }
    envelope = {
        "schema_version": "1.0",
        "opportunity_id": str(opportunity_id),
        "status": "ready",
        "outputs": {
            "call_summary": "\n\n".join(call_summary_parts).strip(),
            "mom": mom,
            "presentation": {
                "status": "unfrozen",
                "code": "ADJUSTED_PPT_PENDING_EXISTING_DECK_PATH",
                "presentation_id": None,
                "download_url": None,
            },
            "transcript_summary": summary,
            "generated_at": _now(),
        },
    }
    payload = _validate("stage2_outputs.schema.json", envelope)
    store.update_opportunity(
        opportunity_id=opportunity_id,
        user_id=user_id,
        updates={"stage2_outputs": payload},
    )
    upsert = getattr(store, "upsert_transcript_summary", None)
    if callable(upsert):
        upsert(
            opportunity_id=opportunity_id,
            user_id=user_id,
            transcript_id=UUID(str(source["id"])),
            conversation_id=str(source.get("conversation_id") or ""),
            summary_json=summary,
            prompt_version=TRANSCRIPT_SUMMARY_PROMPT_VERSION,
        )
    return payload


def _transcript_text(sections: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for section in sections:
        speaker = str(section.get("speaker_role") or section.get("speaker") or "Speaker").strip()
        text = str(section.get("content") or section.get("text") or "").strip()
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def _require_followup_statics(opportunity: dict[str, Any]) -> dict[str, Any]:
    statics = opportunity.get("followup_statics")
    if not isinstance(statics, dict) or not statics.get("project_name"):
        raise bad_request(
            "FOLLOWUP_STATICS_REQUIRED",
            "Set opportunity.followup_statics (project_name, recipients, sender) before generating the MS-32 email draft.",
        )
    return statics


def _extract_followup_payload(
    *,
    opportunity_id: UUID,
    transcript_id: UUID | str,
    sections: list[dict[str, Any]],
    meeting_topic: str,
) -> dict[str, Any]:
    summary = summarize_speaker_sections(
        sections,
        opportunity_id=opportunity_id,
        transcript_id=transcript_id,
    )
    meeting_date = default_meeting_date(summary)
    if settings.AI_EXECUTION_MODE == "live":
        transcript = _transcript_text(sections)
        try:
            return extract_followup(
                transcript,
                calendar_meeting_date=meeting_date,
                opportunity_id=str(opportunity_id),
            )
        except FollowupExtractionError as exc:
            raise bad_request("FOLLOWUP_EXTRACTION_FAILED", exc.user_message) from exc
    return extract_followup_from_summary(
        summary,
        meeting_topic=meeting_topic,
        calendar_meeting_date=meeting_date,
        transcript_haystack=_transcript_text(sections),
        sections=sections,
    )


def generate_email_draft(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    journey_stage: str,
    overwrite_edits: bool = False,
    language: str | None = None,
) -> dict[str, Any]:
    from app.services import followup_email

    if journey_stage not in JOURNEY_STAGES:
        raise bad_request("INVALID_JOURNEY_STAGE", "journey_stage must be first_contact, deepening, or concretisation")
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    name = str(opportunity.get("opportunity_name") or "this opportunity")
    topic = resolve_meeting_purpose(opportunity) or name
    email_source: dict[str, Any] | None = None
    review_flags: list[str] = []
    written_by: dict[str, Any] | None = None
    text_variants: dict[str, Any] | None = None
    # A Master Presentation opportunity writes the email from its finalized V2 package; an
    # earlier opportunity with a standalone PPT #2 keeps the transcript-based draft below.
    followup_email.existing_draft_for_generation(
        store, opportunity_id=opportunity_id, user_id=user_id, journey_stage=journey_stage, overwrite_edits=overwrite_edits
    )
    master = (
        followup_email.master_generation(store, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id, journey_stage=journey_stage, language=language)
        if journey_stage == "deepening"
        else None
    )
    if master is None and language not in (None, "en"):
        # BAP-13: German is written from the finalized Master Presentation package. The earlier
        # email flows (first contact, standalone PPT #2, concretisation) are English only.
        raise conflict(
            "LANGUAGE_UNSUPPORTED",
            "German follow-up emails are written from a finalized Master Presentation package. This email flow is available in English only.",
        )
    statics = _require_followup_statics(opportunity)
    meeting_date = datetime.now(UTC).strftime("%d.%m.%Y")

    if master is not None:
        lengths, review_flags, email_source = master["lengths"], master["review_flags"], master["source"]
        written_by, text_variants = master["writer"], master["variants"]
    elif journey_stage == "deepening":
        sources = store.list_transcript_sources(opportunity_id=opportunity_id, user_id=user_id)
        if not sources:
            raise bad_request(
                "TRANSCRIPT_REQUIRED",
                "Upload a meeting transcript before generating the Deepening email draft.",
            )
        source = sources[-1]
        sections = list(source.get("sections") or [])
        extraction = _extract_followup_payload(
            opportunity_id=opportunity_id,
            transcript_id=source["id"],
            sections=sections,
            meeting_topic=topic,
        )
        lengths = render_three_lengths(extraction, statics)
    elif journey_stage == "first_contact":
        lengths = render_first_contact_draft(statics, topic=topic, meeting_date=meeting_date)
    else:
        extraction = extract_followup_from_summary(
            {
                "narrative": f"Optional post-proposal note for {name}. No prices or commercial terms were invented.",
                "participants": [],
                "decisions": [],
                "action_items": [],
                "open_questions": [],
            },
            meeting_topic=topic,
            calendar_meeting_date=meeting_date,
        )
        lengths = render_three_lengths(extraction, statics)
    return followup_email.store_generated(
        store,
        opportunity=opportunity,
        opportunity_id=opportunity_id,
        user_id=user_id,
        journey_stage=journey_stage,
        lengths=lengths,
        source=email_source,
        review_flags=review_flags,
        overwrite_edits=overwrite_edits,
        writer=written_by,
        variants=text_variants,
    )


def envelope_from_stored(
    opportunity_id: UUID,
    journey_stage: str,
    stored: dict[str, Any] | None,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if stored is None:
        return empty_email(opportunity_id, journey_stage)
    created = stored.get("created_at")
    updated = stored.get("updated_at")
    confirmed = stored.get("confirmed_at")
    draft = {
        "id": str(stored["id"]),
        "status": stored.get("status") or "draft",
        "send_status": "not_sent",
        "selected_length": stored.get("selected_length"),
        "lengths": stored["lengths"],
        "confirmed_at": confirmed.isoformat().replace("+00:00", "Z") if hasattr(confirmed, "isoformat") else confirmed,
        "created_at": created.isoformat().replace("+00:00", "Z") if hasattr(created, "isoformat") else str(created),
        "updated_at": updated.isoformat().replace("+00:00", "Z") if hasattr(updated, "isoformat") else str(updated),
        **(extra or {"revision": int(stored.get("revision") or 1)}),
    }
    return _validate(
        "email_draft.schema.json",
        {
            "schema_version": "1.0",
            "opportunity_id": str(opportunity_id),
            "journey_stage": journey_stage,
            "draft": draft,
        },
    )


def confirm_email_draft(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    draft_id: UUID,
    selected_length: str,
    expected_revision: int | None = None,
    review_checks: list[str] | None = None,
    acknowledged_flags: list[str] | None = None,
) -> dict[str, Any]:
    from app.services import followup_email

    return followup_email.confirm_draft(
        store,
        opportunity_id=opportunity_id,
        user_id=user_id,
        draft_id=draft_id,
        selected_length=selected_length,
        expected_revision=expected_revision,
        review_checks=review_checks,
        acknowledged_flags=acknowledged_flags,
    )


def prompt_uses_summary_only(prompt: str) -> None:
    if "UNTRUSTED_TRANSCRIPT_BEGIN" in prompt or "TRANSCRIPT_SUMMARY_BEGIN" not in prompt:
        raise AssertionError("LLM prompt must use TRANSCRIPT_SUMMARY only")


def summary_prompt_block(summary: dict[str, Any]) -> str:
    return format_transcript_summary_for_prompt(summary)
