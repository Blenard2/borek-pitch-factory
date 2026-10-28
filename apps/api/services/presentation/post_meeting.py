"""PPT #2 planning input and planner entry. Not a stored intake model.

The frozen input keeps BT-46 source boundaries. The generation manifest
stores provenance ids only and is built separately.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from packages.contracts.validators import ContractValidationError
from services.presentation.chapter_layout_guidance import (
    prepare_chapter_layout_guidance_for_planner,
)
from services.presentation.first_pitch import PAGE_LAYOUTS
from services.presentation.generatable_layouts import planning_target_schema
from services.presentation.planner import (
    PROMPT_PATH,
    PlanningClient,
    PresentationPlan,
    run_planning_attempts,
)
from services.presentation.discovery_brief_adapter import (
    is_opportunity_analysis,
    planner_pages_from_brief,
)
from services.presentation.ppt1_constraints import DISCOVERY_PAGE_KEYS

MEETING_SECTION_KEYS: tuple[str, ...] = (
    "requirements",
    "challenges",
    "priorities",
    "opportunities",
    "discussed_solutions",
    "decisions",
    "follow_ups",
)

NOTES_LAYOUT = "OPEN_QUESTIONS_01"
EXTRACTION_LAYOUT = "REQUIREMENTS_MATRIX_01"
USE_CASE_LAYOUT = "ARCHITECTURE_01"

PPT2_REFERENCE_RE = re.compile(
    "^(?:discovery\\.(?:"
    + "|".join(DISCOVERY_PAGE_KEYS)
    + ")|meeting\\.(?:"
    + "|".join(MEETING_SECTION_KEYS)
    + ")|notes|use_case\\.[A-Za-z0-9][A-Za-z0-9._-]{0,120})$"
)

PPT2_INSTRUCTIONS = """
PPT #2 ADDITIONAL RULES
- The only content source is ppt2Context. Do not use a FrameworkObject.
- Keep the four sources separate. Do not merge them into one narrative and do not let a later source overwrite an earlier one.
- approved_discovery is the baseline approved pre-meeting narrative.
- personal_notes are the owner's direct post-meeting observations. Omit this source when it is null.
- meeting_extraction is structured evidence from the meeting. Omit it when it is null. A stale-notes warning does not forbid generation.
- selected_use_cases are approved reusable Borek supporting material. Omit them when the id list is empty.
- source_priority is authority order when claims conflict. It is not an instruction to concatenate or replace text.
- Do not invent reconciliation facts.
- frameworkReferences must be discovery.<page>, meeting.<section>, notes, or use_case.<fact_id>.
- Do not invent framework chapter ids.
- Each layoutId may appear once. There is no 8-slide cap.
- A discovery slide uses only that page. A notes slide uses only notes. A meeting slide uses only meeting sections. A use-case slide uses only selected use cases.
""".strip()


def planning_input_from_ppt2_context(context: dict[str, Any]) -> dict[str, Any]:
    """Adapt one BT-46 context into the in-memory planner input."""
    sources = context["sources"]
    discovery = sources["approved_discovery"]
    if discovery.get("status") != "available":
        raise ValueError("PPT #2 requires an approved Discovery")
    notes = sources["personal_notes"]
    extraction = sources["meeting_extraction"]
    selected = sources["selected_use_cases"]
    return {
        "source_kind": "ppt2_context",
        "approved_discovery": {
            "version_id": str(discovery["version_id"]),
            "pages": _discovery_pages(discovery.get("paper_json") or {}),
        },
        # Frozen with the job for provenance and retries. The deck itself never sees them:
        # every planner reads ``customer_ppt2_input`` (audit 2026-10-27, BUG-02).
        "personal_notes": (
            None
            if notes.get("status") != "available"
            else {
                "text": notes.get("text"),
                "updated_at": notes.get("updated_at"),
            }
        ),
        "meeting_extraction": _meeting_input(extraction),
        "selected_use_cases": {
            "use_case_ids": list(selected.get("use_case_ids") or []),
            "use_cases": copy.deepcopy(selected.get("use_cases") or []),
        },
        "source_priority": list(context.get("source_priority") or []),
        "notes_revision_matches_extraction": context.get(
            "notes_revision_matches_extraction"
        ),
        "missing_sources": list(context.get("missing_sources") or []),
        "warnings": copy.deepcopy(context.get("warnings") or []),
    }


def ppt2_generation_manifest(context: dict[str, Any]) -> dict[str, Any]:
    """Provenance identities for the BT-46 context actually frozen on this job."""
    sources = context["sources"]
    discovery = sources["approved_discovery"]
    notes = sources["personal_notes"]
    extraction = sources["meeting_extraction"]
    selected = sources["selected_use_cases"]
    if extraction.get("status") == "available":
        body = extraction.get("extraction") or {}
        transcript_id = extraction.get("transcript_id")
        generated_at = body.get("generated_at")
        extraction_notes = extraction.get("personal_notes_updated_at")
    else:
        transcript_id = None
        generated_at = None
        extraction_notes = None
    return {
        "schema_version": "1.0",
        "kind": "ppt2",
        "approved_discovery_version_id": str(discovery["version_id"]),
        "transcript_id": None if transcript_id in (None, "") else str(transcript_id),
        "meeting_extraction_generated_at": generated_at,
        "extraction_notes_revision": extraction_notes,
        "current_personal_notes_updated_at": (
            notes.get("updated_at") if notes.get("status") == "available" else None
        ),
        "selected_use_case_ids": [str(item) for item in selected.get("use_case_ids") or []],
    }


def plan_post_meeting_from_context(
    ppt2_context: dict[str, Any],
    *,
    planner: PlanningClient | None = None,
) -> PresentationPlan:
    """Plan PPT #2 from a frozen BT-46 adapter. Does not read a framework."""
    if planner is None:
        from llm.client import LlmClient

        planner = LlmClient()
    ppt2_context = customer_ppt2_input(ppt2_context)
    return run_planning_attempts(
        _planner_payload(ppt2_context),
        client=planner,
        extra_validator=lambda plan: _validate_ppt2_plan(plan, ppt2_context),
        retry_validation_errors=True,
    )


def deterministic_post_meeting_plan(ppt2_context: dict[str, Any]) -> dict[str, Any]:
    """Fixture plan: discovery pages, then each present post-meeting source."""
    ppt2_context = customer_ppt2_input(ppt2_context)
    slides: list[dict[str, Any]] = []
    pages = sorted(
        ppt2_context["approved_discovery"]["pages"],
        key=lambda page: int(page.get("order") or 0),
    )
    for page in pages:
        key = str(page["key"])
        slides.append(
            {
                "order": len(slides) + 1,
                "purpose": f"Carry the approved Discovery {key.replace('_', ' ')} as its own source",
                "layoutId": PAGE_LAYOUTS[key],
                "frameworkReferences": [f"discovery.{key}"],
            }
        )
    if ppt2_context.get("personal_notes"):
        slides.append(
            {
                "order": len(slides) + 1,
                "purpose": "Record the owner's post-meeting notes without merging them into Discovery",
                "layoutId": NOTES_LAYOUT,
                "frameworkReferences": ["notes"],
            }
        )
    meeting_refs = [
        f"meeting.{key}"
        for key in MEETING_SECTION_KEYS
        if _section_items(ppt2_context, key)
    ]
    if meeting_refs:
        slides.append(
            {
                "order": len(slides) + 1,
                "purpose": "Present structured meeting extraction without replacing notes or Discovery",
                "layoutId": EXTRACTION_LAYOUT,
                "frameworkReferences": meeting_refs,
            }
        )
    use_case_ids = list((ppt2_context.get("selected_use_cases") or {}).get("use_case_ids") or [])
    if use_case_ids:
        slides.append(
            {
                "order": len(slides) + 1,
                "purpose": "Attach the selected Borek use cases as supporting material",
                "layoutId": USE_CASE_LAYOUT,
                "frameworkReferences": [f"use_case.{fact_id}" for fact_id in use_case_ids],
            }
        )
    return {
        "schema_version": "1.0",
        "title": _title(pages),
        "slides": slides,
    }


def _validate_ppt2_plan(plan: dict[str, Any], ppt2_context: dict[str, Any]) -> None:
    allowed = _allowed_references(ppt2_context)
    for slide in plan.get("slides") or []:
        references = list(slide.get("frameworkReferences") or [])
        if not references:
            raise ContractValidationError("PPT #2 slide is missing a source reference")
        families = set()
        for reference in references:
            if not isinstance(reference, str) or not PPT2_REFERENCE_RE.match(reference):
                raise ContractValidationError(
                    f"PPT #2 reference {reference!r} is not a supported source id"
                )
            if reference not in allowed:
                raise ContractValidationError(
                    f"PPT #2 reference {reference!r} is not in the frozen context"
                )
            families.add(_family(reference))
        if len(families) != 1:
            raise ContractValidationError("PPT #2 slide mixes source families")
        family = families.pop()
        layout_id = slide.get("layoutId")
        if family == "discovery":
            if len(references) != 1:
                raise ContractValidationError("A Discovery slide must cite one page")
            key = references[0].removeprefix("discovery.")
            if layout_id != PAGE_LAYOUTS.get(key):
                raise ContractValidationError(
                    f"PPT #2 page {key} must use layout {PAGE_LAYOUTS.get(key)}"
                )
        elif layout_id != {"notes": NOTES_LAYOUT, "meeting": EXTRACTION_LAYOUT, "use_case": USE_CASE_LAYOUT}[family]:
            raise ContractValidationError(
                f"PPT #2 {family} slides must use their dedicated layout"
            )


def _planner_payload(ppt2_context: dict[str, Any]) -> dict[str, Any]:
    instructions = PROMPT_PATH.read_text(encoding="utf-8").rstrip() + "\n\n" + PPT2_INSTRUCTIONS
    return {
        "instructions": instructions,
        "ppt2Context": copy.deepcopy(ppt2_context),
        "chapterLayoutGuidance": prepare_chapter_layout_guidance_for_planner(),
        "targetSchema": planning_target_schema(),
    }


def _discovery_pages(paper: dict[str, Any]) -> list[dict[str, Any]]:
    if is_opportunity_analysis(paper):
        # Discovery v2 hands over its presentation brief, not its printed pages.
        return planner_pages_from_brief(paper)
    by_key = {
        str(page.get("key")): page
        for page in paper.get("pages") or []
        if isinstance(page, dict)
    }
    pages: list[dict[str, Any]] = []
    for order, key in enumerate(DISCOVERY_PAGE_KEYS, start=1):
        page = by_key.get(key)
        if page is None:
            raise ValueError(f"Approved Discovery is missing page {key}")
        pages.append(
            {
                "key": key,
                "order": order,
                "title": str(page.get("title") or key),
                "content": copy.deepcopy(page.get("content")),
            }
        )
    return pages


def _meeting_input(extraction: dict[str, Any]) -> dict[str, Any] | None:
    if extraction.get("status") != "available":
        return None
    body = copy.deepcopy(extraction.get("extraction") or {})
    return {
        "transcript_id": extraction.get("transcript_id"),
        "generated_at": body.get("generated_at"),
        "personal_notes_updated_at": extraction.get("personal_notes_updated_at"),
        "extraction": body,
    }


# Where a meeting finding comes from (meeting_extraction ``item_sources``). Only the first two
# are customer-safe; a notes-only finding is internal; anything else is unknown.
CLIENT_FINDING_SOURCES = frozenset({"transcript", "both"})
NOTES_FINDING_SOURCE = "personal_notes"


class Ppt2FindingSourcesError(ValueError):
    """The meeting findings carry no complete, valid record of their sources.

    Without it, findings from the owner's personal notes cannot be told apart from what was said
    in the meeting, so PPT #2 is not generated (fail closed, audit 2026-10-27 BUG-02).
    """

    code = "PPT2_FINDING_SOURCES_UNKNOWN"

    def __init__(self, sections: list[str]) -> None:
        self.sections = sections
        names = ", ".join(section.replace("_", " ") for section in sections)
        super().__init__(
            f"PPT #2 was not generated: the meeting findings ({names}) have no complete record of whether they come "
            "from the transcript or from your personal notes, so internal notes could reach the client. Analyse the "
            "meeting again on the Meeting inputs page, then generate PPT #2. Earlier versions are unchanged."
        )
        self.user_message = str(self)


def customer_ppt2_input(ppt2_input: dict[str, Any]) -> dict[str, Any]:
    """What a PPT #2 deck may show: the frozen input without the owner's personal notes.

    The legacy PPT #2 is a customer document (audit 2026-10-27, BUG-02). The owner's notes and the
    meeting findings that rest only on them stay in the frozen job input for provenance, but no
    planner, fixture deck or slide builder receives them - so no notes slide and no notes-only
    finding can reach a slide. Fails closed: findings whose source is missing, mismatched or not
    a known source raise ``Ppt2FindingSourcesError`` instead of being shown. The frozen input is
    never changed. Idempotent.
    """
    view = copy.deepcopy(ppt2_input)
    view["personal_notes"] = None
    meeting = view.get("meeting_extraction")
    if isinstance(meeting, dict) and meeting.get("extraction") is not None:
        if not isinstance(meeting["extraction"], dict):
            raise Ppt2FindingSourcesError(list(MEETING_SECTION_KEYS))
        meeting["extraction"] = _without_notes_only_items(meeting["extraction"])
    return view


def _without_notes_only_items(body: dict[str, Any]) -> dict[str, Any]:
    """The extraction with only the findings verified as said in the meeting.

    A section with findings needs a source per finding (``item_sources``, same length, known
    values). Notes-only findings are dropped; a section without such a record is refused.
    """
    sources = body.get("item_sources")
    unverified: list[str] = []
    kept_sources: dict[str, list[str]] = {}
    for key in MEETING_SECTION_KEYS:
        items = body.get(key)
        if items is None or items == []:
            kept_sources[key] = []
            continue
        origins = sources.get(key) if isinstance(sources, dict) else None
        if (
            not isinstance(items, list)
            or not isinstance(origins, list)
            or len(origins) != len(items)
            or any(origin not in CLIENT_FINDING_SOURCES | {NOTES_FINDING_SOURCE} for origin in origins)
        ):
            unverified.append(key)
            continue
        kept = [(item, origin) for item, origin in zip(items, origins, strict=True) if origin in CLIENT_FINDING_SOURCES]
        body[key] = [item for item, _origin in kept]
        kept_sources[key] = [origin for _item, origin in kept]
    if unverified:
        raise Ppt2FindingSourcesError(unverified)
    body["item_sources"] = kept_sources
    return body


def _section_items(ppt2_context: dict[str, Any], key: str) -> list[str]:
    meeting = ppt2_context.get("meeting_extraction") or {}
    body = meeting.get("extraction") or {}
    return [str(item) for item in body.get(key) or [] if str(item).strip()]


def _allowed_references(ppt2_context: dict[str, Any]) -> set[str]:
    allowed = {
        f"discovery.{page['key']}"
        for page in ppt2_context["approved_discovery"]["pages"]
    }
    if ppt2_context.get("personal_notes"):
        allowed.add("notes")
    allowed.update(
        f"meeting.{key}"
        for key in MEETING_SECTION_KEYS
        if _section_items(ppt2_context, key)
    )
    for fact_id in (ppt2_context.get("selected_use_cases") or {}).get("use_case_ids") or []:
        allowed.add(f"use_case.{fact_id}")
    return allowed


def ppt2_allowed_references(ppt2_context: dict[str, Any]) -> set[str]:
    """Reference ids a PPT #2 slide may cite for this frozen context."""
    return _allowed_references(ppt2_context)


def ppt2_title(ppt2_context: dict[str, Any]) -> str:
    """Deck title for a frozen PPT #2 context (``Post-meeting — <client>``)."""
    return _title(list(ppt2_context["approved_discovery"]["pages"]))


def _family(reference: str) -> str:
    if reference.startswith("discovery."):
        return "discovery"
    if reference.startswith("meeting."):
        return "meeting"
    if reference.startswith("use_case."):
        return "use_case"
    return "notes"


def _title(pages: list[dict[str, Any]]) -> str:
    cover = next((page for page in pages if page.get("key") == "cover"), None)
    content = cover.get("content") if isinstance(cover, dict) else None
    client_name = ""
    if isinstance(content, dict):
        client_name = str(content.get("client_name") or "").strip()
    if client_name:
        return f"Post-meeting — {client_name}"
    return "Post-meeting"
