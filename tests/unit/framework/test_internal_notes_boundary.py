"""Internal notes stop at the generation boundary (product audit 2026-10-27, BUG-02).

* Discovery 1: the salesperson's "Additional information" is internal background. The fixture
  never prints it; the live prompts say so, and a live answer that copies it is rejected.
* Legacy PPT #2: the owner's personal notes and findings that rest only on them never reach the
  planner of this customer deck.
"""

from __future__ import annotations

import json

from services.framework.discovery_analysis import live
from services.framework.discovery_analysis.fixture import load_library
from services.presentation.borek_deck.fixture import deterministic_post_meeting_deck
from services.presentation.post_meeting import (
    DISCOVERY_PAGE_KEYS,
    customer_ppt2_input,
    deterministic_post_meeting_plan,
    planning_input_from_ppt2_context,
)
from services.framework.discovery_analysis.pipeline import generate_discovery_analysis
from tests.unit.framework.test_bap9_customer_language import FIXED, generate, opportunity, printed, profile
from tests.unit.framework.test_discovery_analysis import FakeModel

NOTE_EN = "Internal: the COO decides alone and is very price-sensitive, so push the premium package early."
NOTE_DE = "Intern: Herr Weiß entscheidet allein und ist sehr preissensibel, also früh das Premiumpaket anbieten."


def test_the_fixture_never_prints_the_intake_note_in_either_language() -> None:
    for language, note in (("en", NOTE_EN), ("de", NOTE_DE)):
        paper = generate_discovery_analysis(opportunity("Wahl & CO", about_company=note), persist=lambda _paper: None, language=language, **FIXED)
        assert paper["status"] == "ready" and paper["language"] == language
        text = printed(paper)
        assert note not in text and "price-sensitive" not in text and "preissensibel" not in text
        assert "Background to this conversation" not in text
        assert paper["analysis"]["research"]["company_profile"] == {"text": note, "origin": "USER_INPUT"}, "kept for review"
    overview = load_library()["chapters"]["overview"]
    assert "profile_provided" not in overview and "profile_provided" not in load_library("de")["chapters"]["overview"]
    assert profile(generate("Wahl & CO", about_company=NOTE_EN)) == overview["profile_unknown_named"].format(client="Wahl & CO")


def test_website_research_still_replaces_the_neutral_paragraph_when_a_note_exists() -> None:
    paper = generate("Hansa Präzisionsteile", "https://www.hansa-praezision.test", research=True, about_company=NOTE_EN)
    assert profile(paper).startswith("From the Hansa Präzisionsteile website (sources in the company background table): ")
    assert NOTE_EN not in printed(paper)
    assert paper["analysis"]["research"]["company_profile"]["origin"] == "USER_INPUT"


def test_every_live_prompt_marks_the_note_as_internal() -> None:
    for prompt in (live.SYSTEM_PROMPT, live.SYSTEM_PROMPT_WEBSITE, live.SYSTEM_PROMPT_DE, live.SYSTEM_PROMPT_WEBSITE_DE):
        assert "additional_information is the salesperson's own internal background note" in prompt
        assert "never repeat what it says about people, roles, who decides, budgets" in prompt
    assert live.PROMPT_VERSION == "discovery-analysis:v5"


def test_copying_the_note_is_detected_by_word_runs() -> None:
    assert live.copied_internal_notes({"a": "x " + NOTE_EN.lower()}, NOTE_EN) == ["a: copies the internal note"]
    assert live.copied_internal_notes(["Herr Weiß entscheidet allein und ist sehr preissensibel, also"], NOTE_DE) == ["/0: copies the internal note"]
    # Shared words or a short phrase are not a copy.
    assert live.copied_internal_notes({"a": "Pricing decisions take long; the COO decides on investments."}, NOTE_EN) == []
    assert live.copied_internal_notes({"a": "anything"}, None) == [] and live.copied_internal_notes({"a": "x"}, "too short") == []


class CopyingModel(FakeModel):
    """Copies the salesperson's note into the overview on its first answer."""

    def answer(self, stage: str):
        answer = super().answer(stage)
        if stage == "research" and self.calls.count("research") == 1:
            answer = {**answer, "overview_context": f"{NOTE_EN} The lever is the work around the core work."}
        return answer


def test_a_live_answer_that_copies_the_note_is_asked_again() -> None:
    model = CopyingModel()
    paper = generate("Wahl & CO", complete=model, about_company=NOTE_EN)
    assert paper["status"] == "ready"
    assert model.calls.count("research") == 2, "the copying answer was rejected once"
    assert NOTE_EN not in printed(paper) and "premium package" not in json.dumps(paper["analysis"]["framing"], ensure_ascii=False)


def _ppt2_context() -> dict:
    return {
        "sources": {
            "approved_discovery": {"status": "available", "version_id": "11111111-1111-4111-8111-111111111111", "paper_json": {"pages": [{"key": key, "title": key, "content": {"text": key}} for key in DISCOVERY_PAGE_KEYS]}},
            "personal_notes": {"status": "available", "text": "The COO decides alone; price-sensitive.", "updated_at": "2026-10-27T10:00:00Z"},
            "meeting_extraction": {
                "status": "available",
                "transcript_id": "22222222-2222-4222-8222-222222222222",
                "personal_notes_updated_at": "2026-10-27T10:00:00Z",
                "extraction": {
                    "generated_at": "2026-10-27T11:00:00Z",
                    "requirements": ["Quotes within one day.", "The COO decides alone."],
                    "challenges": ["Pricing data sits in three systems."],
                    "priorities": [], "opportunities": [], "discussed_solutions": [], "decisions": [], "follow_ups": [],
                    "item_sources": {
                        "requirements": ["transcript", "personal_notes"], "challenges": ["both"],
                        "priorities": [], "opportunities": [], "discussed_solutions": [], "decisions": [], "follow_ups": [],
                    },
                },
            },
            "selected_use_cases": {"use_case_ids": [], "use_cases": []},
        },
        "source_priority": ["approved_discovery", "personal_notes", "meeting_extraction", "selected_use_cases"],
    }


def test_legacy_ppt2_never_shows_the_owners_notes() -> None:
    frozen = planning_input_from_ppt2_context(_ppt2_context())
    # Frozen with the job for provenance and retries ...
    assert frozen["personal_notes"]["text"] == "The COO decides alone; price-sensitive."
    # ... but every planner and slide builder reads the customer view.
    view = customer_ppt2_input(frozen)
    assert view["personal_notes"] is None and frozen["personal_notes"] is not None, "the frozen input is not changed"
    extraction = view["meeting_extraction"]["extraction"]
    assert extraction["requirements"] == ["Quotes within one day."] and extraction["item_sources"]["requirements"] == ["transcript"]
    assert extraction["challenges"] == ["Pricing data sits in three systems."], "a point also in the transcript stays"
    assert "COO" not in json.dumps(view)
    assert customer_ppt2_input(view) == view
    plan = deterministic_post_meeting_plan(frozen)
    assert all(slide["frameworkReferences"] != ["notes"] for slide in plan["slides"])
    deck = deterministic_post_meeting_deck(frozen)
    assert "COO" not in json.dumps(deck) and "Our notes" not in json.dumps(deck)
