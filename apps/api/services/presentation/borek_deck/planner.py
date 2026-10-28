"""Plan PPT #1 / PPT #2 with the Borek deck generator.

    pre-meeting   ``make_ai_tech_deck``  ("Ai Tech Borek Presentation" look, at most 8 slides)
                  source: the approved Discovery pages (``first_pitch.planning_input_from_approved_paper``)
    post-meeting  ``make_master_deck``   ("Borek Master Presentation" layouts, no slide cap)
                  source: the frozen BT-46 context (``post_meeting.planning_input_from_ppt2_context``)

Both go through ``llm_planner.plan_deck``: materials -> prompt -> model reply -> normalised
slide list. The model is reached through the backend's ``PlanningClient`` (the same object the
existing planners use), so every call stays egress-filtered, PII-policed and logged. A client
marked ``deterministic`` (the fixture client) skips the model and uses ``fixture.py``.

The result is a ``plan_json`` in the shape described by ``deck_plan``. A reply that cannot be
parsed, cites a source that is not in the frozen input, mixes PPT #2 sources on one slide, carries
PPT #1 pricing, or cannot be laid out is sent back to the model with the reason (three attempts).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from services.presentation.borek_deck import fixture, sources
from services.presentation.borek_deck.deck_plan import (
    FIXED_LAYOUTS,
    PLAN_ENGINE,
    POST_MEETING,
    PRE_MEETING,
    planned_slide,
)
from services.presentation.borek_deck.engine import PRE_MEETING_MAX_SLIDES, engine
from services.presentation.planner import (
    PlanningClient,
    PresentationPlanningCallError,
    PresentationPlanValidationError,
)
from services.presentation.post_meeting import customer_ppt2_input, ppt2_title
from services.presentation.ppt1_constraints import commercial_strings

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
PRE_MEETING_PROMPT_PATH = PROMPT_DIR / "pre_meeting_ai_tech_v1.txt"
POST_MEETING_PROMPT_PATH = PROMPT_DIR / "post_meeting_master_v1.txt"
PRE_MEETING_PROMPT_VERSION = PRE_MEETING_PROMPT_PATH.stem
POST_MEETING_PROMPT_VERSION = POST_MEETING_PROMPT_PATH.stem

MAX_ATTEMPTS = 3
LANG = "EN"
# The reply is free-form deck JSON, validated here. The provider schema only fixes the envelope.
DECK_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"layout": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}}},
                "required": ["layout"],
            },
        }
    },
    "required": ["slides"],
}


class _DeckRejected(Exception):
    """The model reply is unusable; the message is sent back to the model."""


def plan_pre_meeting_deck(approved_discovery: dict[str, Any], *, planner: PlanningClient) -> dict[str, Any]:
    """PPT #1: at most 8 slides in the Ai Tech Borek look, from the approved Discovery pages."""
    pages = approved_discovery["pages"]
    name = sources.client_name(pages)
    return _plan(
        kind=PRE_MEETING,
        planner=planner,
        materials=sources.pre_meeting_materials(approved_discovery),
        rules=PRE_MEETING_PROMPT_PATH.read_text(encoding="utf-8"),
        prompt_version=PRE_MEETING_PROMPT_VERSION,
        max_slides=PRE_MEETING_MAX_SLIDES,
        audience="client - decision makers, before the first meeting",
        title=f"First meeting — {name}" if name else "First meeting",
        allowed=sources.pre_meeting_allowed_references(approved_discovery),
        single_family=False,
        reject_commercial=True,
        fixture_deck=lambda: fixture.deterministic_pre_meeting_deck(approved_discovery),
        finish=_finish_pre_meeting,
    )


def plan_post_meeting_deck(
    ppt2_input: dict[str, Any],
    *,
    planner: PlanningClient,
    ppt1_slides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """PPT #2: Borek Master layouts, from the frozen post-meeting sources (no slide cap).

    ``ppt1_slides`` are the planned slides of the PPT #1 deck (``deck_plan.planned_slides_from_specs``).
    When given, PPT #2 starts with exactly those slides and the model only plans the extension
    (notes, transcript summary, meeting extraction, use cases) plus the closing slide.
    """
    # A customer deck: the owner's personal notes stay in the frozen job input only (BUG-02).
    ppt2_input = customer_ppt2_input(ppt2_input)
    rules = POST_MEETING_PROMPT_PATH.read_text(encoding="utf-8")
    if ppt1_slides:
        rules += _PPT1_CARRY_OVER_RULES
    plan = _plan(
        kind=POST_MEETING,
        planner=planner,
        materials=sources.post_meeting_materials(ppt2_input),
        rules=rules,
        prompt_version=POST_MEETING_PROMPT_VERSION,
        max_slides=None,
        audience="client - after the first meeting",
        title=ppt2_title(ppt2_input),
        allowed=sources.post_meeting_allowed_references(ppt2_input),
        single_family=True,
        reject_commercial=False,
        fixture_deck=lambda: fixture.deterministic_post_meeting_deck(ppt2_input),
        finish=_finish_post_meeting,
    )
    return _carry_over_ppt1(plan, ppt1_slides) if ppt1_slides else plan


_PPT1_CARRY_OVER_RULES = """

PPT #1 CARRY-OVER
- The deck already begins with the slides of the pre-meeting presentation (PPT #1): cover, who_we_are and the Discovery baseline.
  They are added automatically. Do NOT write slides for the Discovery baseline; plan only the extension that follows them:
  personal notes, the transcript summary (what was said, decisions, action items, open questions), the meeting extraction,
  the selected use cases, and the closing slide.
- The transcript summary has the reference id "transcript_summary"; a slide that uses it must cite only that id.
"""

_BOILERPLATE_LAYOUTS = frozenset({"cover", "who_we_are", "closing"})


def _carry_over_ppt1(plan: dict[str, Any], ppt1_slides: list[dict[str, Any]]) -> dict[str, Any]:
    """PPT #2 = every PPT #1 slide (except its closing) + the planned extension + the closing."""
    carried = [slide for slide in ppt1_slides if slide["layoutId"] != "closing"]
    planned = plan["slides"]
    extension = [
        slide
        for slide in planned
        if slide["layoutId"] not in _BOILERPLATE_LAYOUTS
        and not (
            slide["frameworkReferences"]
            and all(reference.startswith("discovery.") for reference in slide["frameworkReferences"])
        )
    ]
    closing = [slide for slide in planned if slide["layoutId"] == "closing"][-1:]
    merged = carried + extension + closing
    result = copy.deepcopy(plan)
    result["slides"] = [
        {**copy.deepcopy(slide), "order": order} for order, slide in enumerate(merged, start=1)
    ]
    return result


def _finish_pre_meeting(slides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fixed slides keep the reference deck's wording (history, team, closing contacts)."""
    return engine().ai_tech.apply_reference_defaults(slides)


def _finish_post_meeting(slides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Images are never asked for interactively on the server: a placeholder is drawn instead."""
    engine().master.resolve_images(slides, {}, interactive=False)
    return slides


def _plan(
    *,
    kind: str,
    planner: PlanningClient,
    materials: dict[str, str],
    rules: str,
    prompt_version: str,
    max_slides: int | None,
    audience: str,
    title: str,
    allowed: frozenset[str],
    single_family: bool,
    reject_commercial: bool,
    fixture_deck: Callable[[], dict[str, Any]],
    finish: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> dict[str, Any]:
    eng = engine()
    deterministic = bool(getattr(planner, "deterministic", False))
    feedback: str | None = None
    previous: str | None = None
    last_error = "no attempt was made"

    for attempt in range(MAX_ATTEMPTS):

        def llm(system: str, user: str, *, _feedback=feedback, _previous=previous, _attempt=attempt) -> str:
            if deterministic:
                return json.dumps(fixture_deck(), ensure_ascii=False)
            brief: dict[str, Any] = {"request": user}
            if _feedback:
                brief["retryFeedback"] = _feedback
            if _previous:
                brief["previousInvalidDeck"] = _previous
            try:
                payload = planner.complete_planning(
                    planning_input={
                        "instructions": system,
                        "borekDeckBrief": brief,
                        "targetSchema": copy.deepcopy(DECK_TARGET_SCHEMA),
                    },
                    prompt_version=prompt_version,
                    retry_count=_attempt,
                )
            except Exception as exc:
                raise PresentationPlanningCallError(f"Presentation planning call failed: {exc}") from exc
            return json.dumps(payload, ensure_ascii=False)

        spec: list[dict[str, Any]] | None = None
        try:
            spec = eng.llm_planner.plan_deck(
                materials,
                max_slides=max_slides,
                lang=LANG,
                audience=audience,
                llm=llm,
                extra_rules=rules,
            )
            slides, references = _checked_slides(
                spec,
                allowed=allowed,
                single_family=single_family,
                reject_commercial=reject_commercial,
            )
            slides = finish(slides)
            _assert_renders(slides)
        except _DeckRejected as exc:
            last_error = str(exc)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # not JSON, no "slides", or slides that are not objects
            last_error = f"the reply is not a usable slide list ({type(exc).__name__}: {exc})"
        else:
            return {
                "schema_version": "1.0",
                "engine": PLAN_ENGINE,
                "deck_kind": kind,
                "prompt_version": prompt_version,
                "lang": LANG,
                "title": title,
                "slides": [
                    planned_slide(order, slide, refs)
                    for order, (slide, refs) in enumerate(zip(slides, references, strict=True), start=1)
                ],
            }
        feedback = (
            f"Your previous reply was rejected: {last_error}. Return the complete corrected JSON object "
            "and follow every rule again."
        )
        previous = json.dumps(spec, ensure_ascii=False) if spec else None

    raise PresentationPlanValidationError(f"Invalid Borek deck plan ({kind}): {last_error}")


def _checked_slides(
    spec: list[dict[str, Any]],
    *,
    allowed: frozenset[str],
    single_family: bool,
    reject_commercial: bool,
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    """Split ``sources`` off every slide, check it against the frozen input, and enforce the stage rules."""
    slides = copy.deepcopy(spec)
    references: list[list[str]] = []
    for index, slide in enumerate(slides, start=1):
        layout = slide["layout"]
        cited = slide.pop("sources", None)
        if layout in FIXED_LAYOUTS:
            references.append([])
            continue
        if cited is None and layout == "cover" and "discovery.cover" in allowed:
            cited = ["discovery.cover"]  # a cover the planner had to add itself
        if not isinstance(cited, list) or not cited or not all(isinstance(item, str) for item in cited):
            raise _DeckRejected(
                f'slide {index} ({layout}) must list the reference ids it draws on in "sources"'
            )
        unknown = [item for item in cited if item not in allowed]
        if unknown:
            raise _DeckRejected(
                f"slide {index} ({layout}) cites {unknown}, which are not in the source material; "
                f"allowed ids: {sorted(allowed)}"
            )
        ids = list(dict.fromkeys(cited))
        if single_family and len({item.split(".", 1)[0] for item in ids}) > 1:
            raise _DeckRejected(
                f"slide {index} ({layout}) mixes sources {ids}; a slide may draw on only one source"
            )
        references.append(ids)
        if reject_commercial:
            hits = commercial_strings(slide)
            if hits:
                raise _DeckRejected(
                    f"slide {index} ({layout}) contains prohibited commercial content "
                    f'(prices, currency, ROI, cost/budget/savings figures): "{hits[0][:80]}"'
                )
    return slides, references


def _assert_renders(slides: list[dict[str, Any]]) -> None:
    """Lay every slide out once in memory so malformed fields fail here, with a slide number."""
    bp = engine().bp
    deck = bp.Deck(template=None)
    for index, slide in enumerate(slides, start=1):
        try:
            bp.LAYOUTS[slide["layout"]](deck, slide)
        except Exception as exc:
            raise _DeckRejected(
                f"slide {index} ({slide['layout']}) has malformed fields and cannot be laid out "
                f"({type(exc).__name__}: {exc})"
            ) from exc
