"""PPT #1 / PPT #2 are planned and rendered by the Borek deck generator."""

from __future__ import annotations

import copy
import io
import json
import zipfile
from typing import Any

import pytest

from services.presentation.borek_deck import deck_plan
from services.presentation.borek_deck.planner import (
    MAX_ATTEMPTS,
    plan_post_meeting_deck,
    plan_pre_meeting_deck,
)
from services.presentation.borek_deck.render import render_deck_bundle
from services.presentation.planner import PresentationPlanValidationError

NOTE = "Revision A owner observation."
REQUIREMENT = "Keep slotting manual"
PAGE_KEYS = (
    "cover",
    "client_context",
    "opportunity",
    "borek_approach",
    "relevant_use_case",
    "pilot_proposal",
    "next_steps",
)


def _pages() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "order": index,
            "title": key,
            "content": (
                {"client_name": "Harbor Mills", "meeting_purpose": "Warehouse review"}
                if key == "cover"
                else {"text": f"{key} for Harbor Mills. A second sentence about the work."}
            ),
        }
        for index, key in enumerate(PAGE_KEYS, start=1)
    ]


def _discovery() -> dict[str, Any]:
    return {"version_id": "11111111-1111-4111-8111-111111111111", "pages": _pages()}


def _ppt2_context() -> dict[str, Any]:
    return {
        "source_kind": "ppt2_context",
        "approved_discovery": _discovery(),
        "personal_notes": {"text": NOTE, "updated_at": "2026-01-01T00:00:00Z"},
        "meeting_extraction": {
            "transcript_id": "22222222-2222-4222-8222-222222222222",
            "generated_at": "2026-01-02T00:00:00Z",
            "personal_notes_updated_at": "2026-01-01T00:00:00Z",
            "extraction": {
                "generated_at": "2026-01-02T00:00:00Z",
                "requirements": [REQUIREMENT],
                "challenges": [],
                "priorities": [],
                "opportunities": [],
                "discussed_solutions": [],
                "decisions": [],
                "follow_ups": [],
            },
        },
        "selected_use_cases": {
            "use_case_ids": ["reference.invoice-3way.delivery-pattern"],
            "use_cases": [
                {
                    "fact_id": "reference.invoice-3way.delivery-pattern",
                    "statement": "Invoice pattern statement",
                    "status": "resolved",
                }
            ],
        },
        "source_priority": ["approved_discovery", "personal_notes", "meeting_extraction", "selected_use_cases"],
        "notes_revision_matches_extraction": True,
        "missing_sources": [],
        "warnings": [],
    }


class _Planner:
    """A ``PlanningClient`` that replays canned replies and records what it was sent."""

    def __init__(self, *replies: dict[str, Any], deterministic: bool = False) -> None:
        self.replies = list(replies)
        self.deterministic = deterministic
        self.calls: list[dict[str, Any]] = []

    def complete_planning(self, *, planning_input, prompt_version, retry_count=0):
        self.calls.append(
            {"input": planning_input, "prompt_version": prompt_version, "retry_count": retry_count}
        )
        return copy.deepcopy(self.replies.pop(0))


def _list_slide(sources: list[str], text: str = "A short line") -> dict[str, Any]:
    return {
        "layout": "matrix_notes",
        "kicker": "Context",
        "title": "What we know",
        "lead": text,
        "rows": [{"title": "Point", "text": text}],
        "sources": sources,
    }


def _closing() -> dict[str, Any]:
    return {"layout": "closing", "title": "Let’s talk", "tagline": "Thank you."}


def _cover() -> dict[str, Any]:
    return {
        "layout": "cover",
        "kicker": "Borek AI Tech",
        "title": "Harbor Mills\nFirst meeting",
        "intro": "Warehouse review",
        "sources": ["discovery.cover"],
    }


def _layouts(plan: dict[str, Any]) -> list[str]:
    return [slide["layoutId"] for slide in plan["slides"]]


def _by_layout(plan: dict[str, Any], layout: str) -> dict[str, Any]:
    return next(slide for slide in plan["slides"] if slide["layoutId"] == layout)


# --- planning: deterministic (fixture) client ---------------------------------------------


def test_fixture_pre_meeting_deck_is_the_ai_tech_deck_within_eight_slides() -> None:
    planner = _Planner(deterministic=True)
    plan = plan_pre_meeting_deck(_discovery(), planner=planner)
    assert planner.calls == []  # the fixture client never reaches a model
    assert plan["engine"] == deck_plan.PLAN_ENGINE
    assert plan["deck_kind"] == deck_plan.PRE_MEETING
    assert plan["title"] == "First meeting — Harbor Mills"
    assert 3 <= len(plan["slides"]) <= 8
    layouts = _layouts(plan)
    assert layouts[0] == "cover"
    assert layouts[1] == "who_we_are"
    assert layouts[-1] == "closing"
    assert plan["slides"][0]["frameworkReferences"] == ["discovery.cover"]
    assert "Harbor Mills" in json.dumps(plan["slides"][0]["borekSlide"])


def test_fixture_post_meeting_deck_keeps_sources_apart_and_has_no_slide_cap() -> None:
    plan = plan_post_meeting_deck(_ppt2_context(), planner=_Planner(deterministic=True))
    assert plan["deck_kind"] == deck_plan.POST_MEETING
    assert plan["title"].startswith("Post-meeting")
    assert len(plan["slides"]) > 8
    families = {
        slide["frameworkReferences"][0].split(".", 1)[0]
        for slide in plan["slides"]
        if slide["frameworkReferences"]
    }
    # Audit 2026-10-27 (BUG-02): the owner's personal notes are internal, never a slide.
    assert families == {"discovery", "meeting", "use_case"}
    for slide in plan["slides"]:
        assert len({ref.split(".", 1)[0] for ref in slide["frameworkReferences"]}) <= 1
    assert NOTE not in json.dumps(plan)


def test_post_meeting_omits_a_source_that_was_not_provided() -> None:
    source = _ppt2_context()
    source["personal_notes"] = None
    source["meeting_extraction"] = None
    source["selected_use_cases"] = None
    plan = plan_post_meeting_deck(source, planner=_Planner(deterministic=True))
    refs = {ref.split(".", 1)[0] for slide in plan["slides"] for ref in slide["frameworkReferences"]}
    assert refs == {"discovery"}


# --- planning: model replies are validated ------------------------------------------------


def test_model_reply_is_sent_through_the_planning_client_and_normalised() -> None:
    reply = {"slides": [_cover(), {"layout": "who_we_are"}, _list_slide(["discovery.client_context"]), _closing()]}
    planner = _Planner(reply)
    plan = plan_pre_meeting_deck(_discovery(), planner=planner)
    assert _layouts(plan) == ["cover", "who_we_are", "matrix_notes", "closing"]
    assert plan["slides"][2]["frameworkReferences"] == ["discovery.client_context"]
    assert plan["slides"][1]["frameworkReferences"] == []
    # egress-classified payload: instructions + borekDeckBrief + targetSchema only
    sent = planner.calls[0]["input"]
    assert set(sent) == {"instructions", "borekDeckBrief", "targetSchema"}
    assert "Harbor Mills" in sent["borekDeckBrief"]["request"]
    assert planner.calls[0]["prompt_version"] == "pre_meeting_ai_tech_v1"
    # "sources" is a planning aid and does not reach the stored slide
    assert all("sources" not in slide["borekSlide"] for slide in plan["slides"])


def test_unknown_source_is_rejected_then_corrected() -> None:
    bad = {"slides": [_cover(), _list_slide(["discovery.made_up"]), _closing()]}
    good = {"slides": [_cover(), _list_slide(["discovery.opportunity"]), _closing()]}
    planner = _Planner(bad, good)
    plan = plan_pre_meeting_deck(_discovery(), planner=planner)
    assert len(planner.calls) == 2
    retry = planner.calls[1]["input"]["borekDeckBrief"]
    assert "discovery.made_up" in retry["retryFeedback"]
    assert retry["previousInvalidDeck"]
    assert planner.calls[1]["retry_count"] == 1
    assert _by_layout(plan, "matrix_notes")["frameworkReferences"] == ["discovery.opportunity"]


def test_pre_meeting_deck_with_prices_is_rejected() -> None:
    priced = {
        "slides": [
            _cover(),
            _list_slide(["discovery.pilot_proposal"], text="The pilot costs EUR 40,000."),
            _closing(),
        ]
    }
    planner = _Planner(priced, priced, priced)
    with pytest.raises(PresentationPlanValidationError) as raised:
        plan_pre_meeting_deck(_discovery(), planner=planner)
    assert "commercial" in str(raised.value)
    assert len(planner.calls) == MAX_ATTEMPTS


def test_post_meeting_may_carry_prices_but_not_mix_sources_on_a_slide() -> None:
    priced = {
        "slides": [
            _cover(),
            _list_slide(["meeting.requirements"], text="The price is EUR 500."),
            _closing(),
        ]
    }
    plan = plan_post_meeting_deck(_ppt2_context(), planner=_Planner(priced))
    assert "EUR 500" in json.dumps(_by_layout(plan, "matrix_notes")["borekSlide"])

    # The owner's notes are not a source of the customer deck (BUG-02): citing them is refused.
    notes = {"slides": [_cover(), _list_slide(["notes"]), _closing()]}
    with pytest.raises(PresentationPlanValidationError):
        plan_post_meeting_deck(_ppt2_context(), planner=_Planner(notes, notes, notes))

    mixed = {"slides": [_cover(), _list_slide(["discovery.opportunity", "meeting.requirements"]), _closing()]}
    planner = _Planner(mixed, mixed, mixed)
    with pytest.raises(PresentationPlanValidationError, match="mixes sources"):
        plan_post_meeting_deck(_ppt2_context(), planner=planner)


def test_source_that_is_not_in_the_frozen_post_meeting_input_is_rejected() -> None:
    source = _ppt2_context()
    source["meeting_extraction"] = None
    reply = {"slides": [_cover(), _list_slide(["meeting.requirements"]), _closing()]}
    with pytest.raises(PresentationPlanValidationError):
        plan_post_meeting_deck(source, planner=_Planner(reply, reply, reply))


def test_unparseable_reply_is_retried() -> None:
    good = {"slides": [_cover(), _list_slide(["discovery.opportunity"]), _closing()]}
    planner = _Planner({"unexpected": True}, good)
    plan = plan_pre_meeting_deck(_discovery(), planner=planner)
    assert len(planner.calls) == 2
    assert _layouts(plan)[0] == "cover"
    assert _layouts(plan)[-1] == "closing"
    assert "matrix_notes" in _layouts(plan)


def test_slide_without_sources_is_rejected() -> None:
    slide = _list_slide([])
    slide.pop("sources")
    reply = {"slides": [_cover(), slide, _closing()]}
    with pytest.raises(PresentationPlanValidationError, match="sources"):
        plan_pre_meeting_deck(_discovery(), planner=_Planner(reply, reply, reply))


def test_model_failure_is_reported_as_a_planning_call_error() -> None:
    from services.presentation.planner import PresentationPlanningCallError

    class Broken:
        deterministic = False

        def complete_planning(self, **_kwargs):
            raise RuntimeError("provider down")

    with pytest.raises(PresentationPlanningCallError):
        plan_pre_meeting_deck(_discovery(), planner=Broken())


# --- storage shape -------------------------------------------------------------------------


def test_plan_slides_become_spec_wrappers() -> None:
    plan = plan_pre_meeting_deck(_discovery(), planner=_Planner(deterministic=True))
    assert deck_plan.is_borek_plan(plan)
    assert not deck_plan.is_borek_plan({"slides": []})
    specs = [deck_plan.borek_slide_spec(slide) for slide in plan["slides"]]
    assert deck_plan.is_borek_slide_specs(specs)
    assert [spec["slideId"] for spec in specs] == [f"slide_{i:02d}" for i in range(1, len(specs) + 1)]
    assert specs[0]["sourceChapterIds"] == ["discovery.cover"]
    assert specs[0]["layoutId"] == "cover"
    assert deck_plan.borek_slides_from_specs(specs)[0] == plan["slides"][0]["borekSlide"]


def test_generatable_filter_keeps_every_borek_slide() -> None:
    from services.presentation.generatable_layouts import filter_generatable_planned_slides

    plan = plan_pre_meeting_deck(_discovery(), planner=_Planner(deterministic=True))
    kept, skipped = filter_generatable_planned_slides(plan)
    assert skipped == []
    assert [slide["order"] for slide in kept] == list(range(1, len(plan["slides"]) + 1))


# --- rendering -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [deck_plan.PRE_MEETING, deck_plan.POST_MEETING])
def test_deck_renders_to_pptx_pdf_and_previews(kind: str) -> None:
    pytest.importorskip("pptx")
    pytest.importorskip("PIL")
    if kind == deck_plan.PRE_MEETING:
        plan = plan_pre_meeting_deck(_discovery(), planner=_Planner(deterministic=True))
    else:
        plan = plan_post_meeting_deck(_ppt2_context(), planner=_Planner(deterministic=True))
    specs = [deck_plan.borek_slide_spec(slide) for slide in plan["slides"]]

    bundle = render_deck_bundle(specs, deck_kind=kind)

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        names = set(archive.namelist())
        assert {"deck.pptx", "deck.pdf", "manifest.json"} <= names
        previews = sorted(name for name in names if name.startswith("preview-"))
        assert len(previews) == len(specs)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["validation"]["status"] == "VALID"
        assert manifest["slideCount"] == len(specs)
        assert archive.read("deck.pdf").startswith(b"%PDF")
        assert archive.read(previews[0]).startswith(b"\x89PNG")
        from pptx import Presentation

        deck = Presentation(io.BytesIO(archive.read("deck.pptx")))
        assert len(deck.slides) == len(specs)


def test_render_rejects_specs_that_are_not_borek_slides() -> None:
    from services.presentation.borek_deck.render import BorekDeckRenderError

    with pytest.raises(BorekDeckRenderError) as raised:
        render_deck_bundle([{"layoutId": "cover_01", "title": "x"}], deck_kind=deck_plan.PRE_MEETING)
    assert raised.value.code == "PPTX_RENDER_FAILED"
    assert raised.value.retryable is False


def test_post_meeting_uses_transcript_summary_and_starts_with_ppt1_slides() -> None:
    from services.presentation.borek_deck.deck_plan import planned_slide

    source = _ppt2_context()
    source["transcript_summary"] = {
        "narrative": "The client wants invoices read automatically.",
        "participants": ["Client"],
        "decisions": ["Start with a pilot."],
        "action_items": [{"text": "Send the pilot scope.", "owner": "Borek"}],
        "open_questions": [],
    }
    ppt1 = [
        planned_slide(1, {"layout": "cover", "title": "Acme\nFirst meeting"}, ["discovery.cover"]),
        planned_slide(2, {"layout": "who_we_are"}, []),
        planned_slide(3, {"layout": "matrix_notes", "kicker": "Opportunity", "title": "PPT1 slide"}, ["discovery.opportunity"]),
        planned_slide(4, {"layout": "closing", "title": "Bye"}, []),
    ]
    plan = plan_post_meeting_deck(source, planner=_Planner(deterministic=True), ppt1_slides=ppt1)
    layouts = [slide["layoutId"] for slide in plan["slides"]]
    titles = [slide["borekSlide"].get("title") for slide in plan["slides"]]
    assert titles[:3] == ["Acme\nFirst meeting", None, "PPT1 slide"]
    assert layouts[-1] == "closing" and layouts.count("closing") == 1
    assert layouts.count("cover") == 1 and layouts.count("who_we_are") == 1
    assert ["transcript_summary"] in [slide["frameworkReferences"] for slide in plan["slides"]]
    assert [slide["order"] for slide in plan["slides"]] == list(range(1, len(layouts) + 1))
