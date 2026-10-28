"""Legacy PPT #2 fails closed on findings without a valid source record (PR #16 review, BUG-02).

Notes-only findings are internal. A finding can only be shown when its source is recorded and
known: ``item_sources`` must exist for the section, have one entry per finding, and name
``transcript`` or ``both`` (shown) or ``personal_notes`` (left out). Anything else stops the
generation with ``Ppt2FindingSourcesError`` - on every planner path - and never changes the
frozen input or an earlier version.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from services.presentation.borek_deck.fixture import deterministic_post_meeting_deck
from services.presentation.borek_deck.planner import plan_post_meeting_deck
from services.presentation.post_meeting import (
    Ppt2FindingSourcesError,
    customer_ppt2_input,
    deterministic_post_meeting_plan,
    plan_post_meeting_from_context,
)
from services.presentation.post_meeting_slide_content import build_post_meeting_slide_specs
from tests.unit.presentation.test_jj35_post_meeting_planner import _context

SAID = "Keep slotting manual"
NOTE_ONLY = "The COO decides alone; very price-sensitive."
EMPTY = {"challenges": [], "priorities": [], "opportunities": [], "discussed_solutions": [], "decisions": [], "follow_ups": []}


def context(requirements: list[Any], sources: Any, **extra: Any) -> dict[str, Any]:
    ctx = _context()
    body = {"generated_at": "2026-01-02T00:00:00Z", "requirements": requirements, **EMPTY, **extra}
    if sources is not ...:
        body["item_sources"] = sources
    ctx["meeting_extraction"]["extraction"] = body
    return ctx


def all_sources(requirements: list[Any]) -> dict[str, list[Any]]:
    return {"requirements": requirements, **{key: [] for key in EMPTY}}


UNVERIFIABLE = {
    "item_sources missing (legacy record)": context([SAID, NOTE_ONLY], ...),
    "item_sources not an object": context([SAID], ["transcript"]),
    "section missing from item_sources": context([SAID], {key: [] for key in EMPTY}),
    "fewer sources than findings": context([SAID, NOTE_ONLY], all_sources(["transcript"])),
    "more sources than findings": context([SAID], all_sources(["transcript", "personal_notes"])),
    "unknown source value": context([SAID], all_sources(["owner"])),
    "missing source value": context([SAID], all_sources([None])),
    "sources not a list": context([SAID], {**all_sources([]), "requirements": "transcript"}),
    "findings not a list": context("Keep slotting manual", all_sources(["transcript"])),
}


@pytest.mark.parametrize("case", sorted(UNVERIFIABLE))
def test_unverifiable_findings_stop_generation_and_leave_the_frozen_input_alone(case: str) -> None:
    ctx = UNVERIFIABLE[case]
    frozen = copy.deepcopy(ctx)
    with pytest.raises(Ppt2FindingSourcesError) as raised:
        customer_ppt2_input(ctx)
    assert raised.value.code == "PPT2_FINDING_SOURCES_UNKNOWN" and raised.value.sections == ["requirements"]
    assert "Analyse the meeting again" in raised.value.user_message and "Earlier versions are unchanged" in raised.value.user_message
    assert ctx == frozen, "the frozen job input is never changed"


def test_an_extraction_body_that_is_not_an_object_is_refused() -> None:
    ctx = _context()
    ctx["meeting_extraction"]["extraction"] = ["Keep slotting manual"]
    with pytest.raises(Ppt2FindingSourcesError):
        customer_ppt2_input(ctx)


def test_verified_transcript_findings_are_kept_and_notes_only_findings_left_out() -> None:
    ctx = context([SAID, NOTE_ONLY, "Quotes within a day."], all_sources(["transcript", "personal_notes", "both"]))
    view = customer_ppt2_input(ctx)
    extraction = view["meeting_extraction"]["extraction"]
    assert extraction["requirements"] == [SAID, "Quotes within a day."]
    assert extraction["item_sources"]["requirements"] == ["transcript", "both"]
    assert view["personal_notes"] is None and NOTE_ONLY not in json.dumps(view)
    assert customer_ppt2_input(view) == view, "idempotent"
    assert ctx["meeting_extraction"]["extraction"]["requirements"][1] == NOTE_ONLY, "the frozen input keeps it"


def test_sections_without_findings_need_no_source_record() -> None:
    ctx = context([], ...)
    assert customer_ppt2_input(ctx)["meeting_extraction"]["extraction"]["requirements"] == []
    missing = _context()
    missing["meeting_extraction"] = None
    assert customer_ppt2_input(missing)["meeting_extraction"] is None


class _NeverCalled:
    """A planner that must not be reached: the check runs before any model or fixture call."""

    def __getattr__(self, name: str):
        raise AssertionError(f"the planner was called ({name}) although the sources are unverified")


PATHS = {
    "borek deck planner (live and fixture)": lambda ctx: plan_post_meeting_deck(ctx, planner=_NeverCalled()),
    "legacy planner (live)": lambda ctx: plan_post_meeting_from_context(ctx, planner=_NeverCalled()),
    "legacy planner (fixture)": deterministic_post_meeting_plan,
    "fixture deck": deterministic_post_meeting_deck,
    "slide builder (worker)": lambda ctx: build_post_meeting_slide_specs(
        {"slides": [{"order": 1, "layoutId": "REQUIREMENTS_MATRIX_01", "frameworkReferences": ["meeting.requirements"]}]}, ctx
    ),
}


@pytest.mark.parametrize("path", sorted(PATHS))
def test_no_planner_path_can_bypass_the_check(path: str) -> None:
    ctx = UNVERIFIABLE["item_sources missing (legacy record)"]
    with pytest.raises(Ppt2FindingSourcesError):
        PATHS[path](copy.deepcopy(ctx))
