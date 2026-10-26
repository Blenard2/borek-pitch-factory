"""BAP-9 Phase 2: reference-based calibration of the Discovery content.

The client-approved reference analyses set the bar: industry-specific wording, a concrete
mechanism and a named human decision per opportunity, measurable discovery questions, chapter
texts in the company's own terms, and no invented company facts. These tests cover what code
can verify; whether a real model writes good industry text needs a live run and PM review.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from services.framework.discovery_analysis import live
from services.framework.discovery_analysis.fixture import load_library
from services.framework.discovery_analysis.model import LIMITS
from services.framework.discovery_analysis.pipeline import DiscoveryAnalysisStageError
from tests.unit.framework.test_bap5_discovery_website import WebsiteAwareModel, analyse
from tests.unit.framework.test_discovery_analysis import POPULATED, FakeModel, generate

LIBRARY_ORDER_WORDING = ("which order gets the contested slot", "the inquiry becomes the quote")


def printed(paper: dict[str, Any]) -> str:
    return json.dumps([page["content"] for page in paper["page_manifest"]], ensure_ascii=False)


def library_deep_dive(area_index: int = 0) -> dict[str, Any]:
    return {"opportunities": copy.deepcopy(load_library()["areas"][area_index]["opportunities"])}


# --------------------------------------------------------------- the quality bar, as code


def test_the_fixture_library_meets_the_reference_quality_bar() -> None:
    library = load_library()
    for area in library["areas"]:
        assert live.quality_problems("deep_dives", {"opportunities": area["opportunities"]}) == [], area["id"]
    titles = [item["title"] for area in library["areas"] for item in area["opportunities"]]
    assert live.quality_problems("overview", {"areas": [{"opportunity_titles": titles}]}) == []
    assert live.quality_problems("target_picture", {"workflows": library["target_workflows"]}, {"opportunities": titles}) == []
    assert live.quality_problems("shadow_processes", {"rows": library["shadow_processes"]}) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda item: item.update(discovery_questions=["Is quoting important to you?", "Do you use spreadsheets?"]), "no discovery question asks for a number"),
        (lambda item: item.update(how_it_works="Reads the drawing, extracts features and drafts the quotation."), "does not end with the person or role"),
        (lambda item: item.update(opportunity_signal="Opportunity if quotes take days."), "repeats the printed label"),
    ],
)
def test_weak_deep_dives_are_named_precisely(mutate, expected: str) -> None:
    answer = library_deep_dive()
    mutate(answer["opportunities"][0])
    problems = live.quality_problems("deep_dives", answer)
    assert any(expected in problem and answer["opportunities"][0]["title"] in problem for problem in problems), problems


def test_repetition_generic_titles_and_unanchored_chains_are_named() -> None:
    answer = library_deep_dive()
    answer["opportunities"][1]["result"] = answer["opportunities"][0]["result"]
    assert "two opportunities share the same result" in live.quality_problems("deep_dives", answer)
    overview = {"areas": [{"opportunity_titles": ["AI analytics", "Process automation", "Drawing-based quotation"]}, {"opportunity_titles": ["Drawing-based quotation"]}]}
    problems = live.quality_problems("overview", overview)
    assert any("repeat across areas: Drawing-based quotation" in problem for problem in problems)
    assert any("AI analytics, Process automation" in problem for problem in problems)
    chains = {"workflows": [{"name": "Order to cash", "stages": ["Synergy engine", "Digital twin", "Smart hub"]}, {"name": "Learning loop", "stages": ["Outcomes", "Patterns", "Standards"]}]}
    problems = live.quality_problems("target_picture", chains, {"opportunities": ["Drawing-based quotation", "Invoice matching"]})
    assert problems == ["workflow 'Order to cash': its blocks are not opportunities from part 2"], "the learning loop is exempt"


# --------------------------------------------------------------- the bounded retry


class WeakFirstModel(FakeModel):
    """Answers the first deep-dive call below the bar, then (optionally) corrects it."""

    def __init__(self, *, second: str = "good") -> None:
        super().__init__()
        self.second = second
        self.users: list[str] = []
        self.deep_calls = 0

    def __call__(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        answer = super().__call__(system, user, schema)
        if self.calls[-1] != "deep_dives":
            return answer
        self.deep_calls += 1
        self.users.append(user)
        if self.deep_calls == 1:
            weak = copy.deepcopy(answer)
            weak["opportunities"][0]["discovery_questions"] = ["Is this relevant?", "Do you agree?"]
            self.area_index -= 1  # the retry answers the same area
            return weak
        if self.deep_calls == 2 and self.second == "weak":
            answer["opportunities"][0]["discovery_questions"] = ["Is this still relevant?", "Do you agree now?"]
        if self.deep_calls == 2 and self.second == "broken":
            return {"unexpected": True}
        return answer


def test_an_answer_below_the_bar_is_retried_once_with_the_reasons() -> None:
    model = WeakFirstModel()
    paper = generate(POPULATED, complete=model)
    assert paper["status"] == "ready"
    assert model.deep_calls == 8, "seven areas, one retry"
    assert "MISSED THE QUALITY BAR" in model.users[1] and "number, duration, share, frequency or cost" in model.users[1]
    assert "Is this relevant?" not in printed(paper)


def test_the_quality_bar_never_fails_an_analysis(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING", logger=live.__name__)
    still_weak = generate(POPULATED, complete=WeakFirstModel(second="weak"))
    assert still_weak["status"] == "ready" and "Is this still relevant?" in printed(still_weak)
    broken_retry = generate(POPULATED, complete=WeakFirstModel(second="broken"))
    assert broken_retry["status"] == "ready", "the first, valid answer is used when the retry breaks"
    assert "Is this relevant?" in printed(broken_retry)
    # PR #12 review: what is still missing is kept with the analysis and logged - never printed.
    for paper in (still_weak, broken_retry):
        warnings = paper["generation"]["quality_warnings"]
        assert len(warnings) == 1 and warnings[0].startswith("deep_dives: opportunity ")
        assert "number, duration, share, frequency or cost" in warnings[0]
        assert "quality_warnings" not in printed(paper) and warnings[0] not in printed(paper)
    assert sum("below the quality bar after one retry" in record.getMessage() for record in caplog.records) == 2


def test_no_quality_warning_is_recorded_when_the_bar_is_met() -> None:
    corrected = generate(POPULATED, complete=WeakFirstModel())
    assert "quality_warnings" not in corrected["generation"], "a corrected retry leaves nothing to review"
    assert "quality_warnings" not in generate(POPULATED, complete=FakeModel())["generation"]
    assert "quality_warnings" not in generate(POPULATED)["generation"], "fixture mode has no model answers to judge"


# --------------------------------------------------------------- industry wording in the chapter texts


class ManufacturingModel(WebsiteAwareModel):
    """A model that writes the research and chapter texts for a precision-parts manufacturer."""

    def __init__(self, label: str = "Precision CNC parts") -> None:
        super().__init__()
        self.label = label

    def answer(self, stage: str) -> dict[str, Any]:
        answer = super().answer(stage)
        if stage == "research":
            answer.update(
                core_thesis="Working hypothesis: margin is decided between the customer drawing and the released quotation.",
                in_short_thesis="Working hypothesis: quotation and first-article checks bind the most days.",
                sector_label=self.label,
            )
        if stage == "shadow_processes":
            answer.update(
                chapter_paragraph="Which job gets the five-axis machine, how tight a tolerance can be promised, when a deviation becomes a customer notice - these calls typically run on the experience of a few planners and inspectors.",
                chapter_key_message="Machine priorities and tolerance promises live in heads, not in the planning system.",
            )
        if stage == "target_picture":
            answer.update(
                chapter_paragraph="The drawing becomes the quotation, the quotation the routing, the routing the inspection plan and the measured part the certificate - every block hands its output to the next.",
                chapter_key_message="From drawing to certificate, people stand at the price and release gates.",
            )
        return answer


def test_live_chapter_texts_and_the_contents_thesis_come_from_the_model() -> None:
    paper = analyse("Hansa Präzisionsteile", "https://www.hansa-praezision.test", complete=ManufacturingModel())
    assert paper["status"] == "ready"
    framing = paper["analysis"]["framing"]
    assert framing["document"]["eyebrow"] == "Precision CNC parts · Discussion basis"
    assert framing["document"]["in_short"][1].startswith("Working hypothesis: quotation and first-article")
    assert framing["chapters"]["shadow"]["paragraphs"][0].startswith("Which job gets the five-axis machine")
    assert framing["chapters"]["target"]["key_message"].startswith("From drawing to certificate")
    text = printed(paper)
    for wording in LIBRARY_ORDER_WORDING:
        assert wording not in text, "no generic order-business wording left in a live document"
    running = {page["content"]["running"]["left"] for page in paper["page_manifest"]}
    assert running == {"Precision CNC parts · Discussion basis 2026"}, "the running head carries the industry"


@pytest.mark.parametrize(
    ("label", "website", "reason"),
    [
        ("Pharmaceutical development", "https://www.hansa-praezision.test", "an industry the website never names"),
        ("Precision CNC parts", None, "no website and no description: nothing says what the company does"),
        ("", "https://www.hansa-praezision.test", "the model leaves the label empty"),
    ],
)
def test_an_industry_nobody_stated_is_never_printed(label: str, website: str | None, reason: str) -> None:
    model = ManufacturingModel(label)
    paper = analyse("Hansa Präzisionsteile", website, complete=model) if website else generate(
        {"id": "11111111-1111-4111-8111-111111111111", "client_name": "Hansa Präzisionsteile", "opportunity_name": "", "stage1_intake": None},
        complete=model,
    )
    assert paper["status"] == "ready", reason
    assert paper["analysis"]["framing"]["document"]["eyebrow"] == load_library()["cover"]["eyebrow"], reason


def test_the_label_is_grounded_in_the_users_own_description() -> None:
    opportunity = {
        "id": "11111111-1111-4111-8111-111111111111",
        "client_name": "Nordlicht Logistik",
        "opportunity_name": "",
        "stage1_intake": {"about_company": "E-commerce fulfilment and returns handling for fashion brands."},
    }
    paper = generate(opportunity, complete=ManufacturingModel("E-commerce fulfilment"))
    assert paper["analysis"]["framing"]["document"]["eyebrow"] == "E-commerce fulfilment · Discussion basis"
    assert len(paper["analysis"]["framing"]["document"]["eyebrow"]) <= LIMITS["eyebrow"]


def test_a_company_figure_in_a_live_chapter_text_is_still_rejected() -> None:
    class Inventing(ManufacturingModel):
        def answer(self, stage: str) -> dict[str, Any]:
            answer = super().answer(stage)
            if stage == "shadow_processes":
                answer["chapter_paragraph"] = "With 240 employees on two shifts, Hansa plans every machine by hand."
            return answer

    with pytest.raises(DiscoveryAnalysisStageError, match="Validation"):
        analyse("Hansa Präzisionsteile", "https://www.hansa-praezision.test", complete=Inventing())


def test_process_commentary_in_the_new_fields_is_caught() -> None:
    class Leaking(ManufacturingModel):
        def __init__(self) -> None:
            super().__init__()
            self.target_calls = 0

        def answer(self, stage: str) -> dict[str, Any]:
            answer = super().answer(stage)
            if stage == "target_picture":
                self.target_calls += 1
                if self.target_calls == 1:
                    answer["chapter_paragraph"] = "Based on the limited context provided, the chains stay generic."
            return answer

    model = Leaking()
    paper = analyse("Hansa Präzisionsteile", "https://www.hansa-praezision.test", complete=model)
    assert paper["status"] == "ready" and model.target_calls == 2
    assert "context provided" not in printed(paper)


# --------------------------------------------------------------- what each stage asks for


def test_every_stage_asks_for_the_reference_qualities() -> None:
    model = WebsiteAwareModel()
    analyse("Hansa Präzisionsteile", "https://www.hansa-praezision.test", complete=model)
    tasks = {}
    for (_system, user), stage in zip(model.prompts, model.calls):
        tasks.setdefault(stage, user[user.index("TASK:") :])
    assert "industry's terms" in tasks["research"] and "sector_label" in tasks["research"]
    assert "never a capability" in tasks["overview"] and "value chain" in tasks["overview"]
    deep = tasks["deep_dives"]
    for requirement in ("documents, records or data", "reviews, decides, approves or releases", "number, duration, share, frequency or cost", "never state it as a fact"):
        assert requirement in deep, requirement
    assert "who makes it" in tasks["shadow_processes"] and "chapter_paragraph" in tasks["shadow_processes"]
    assert "learning loop" in tasks["target_picture"] and "one of the opportunities listed" in tasks["target_picture"]
    assert "concrete measures" in tasks["closing"]
    for system, _user in model.prompts:
        assert "would read the same for another industry" in system and "do not guess one" in system


# --------------------------------------------------------------- hand-off to Presentation V1


def test_an_approved_calibrated_analysis_feeds_master_presentation_v1() -> None:
    from services.presentation.master_deck.plan import build_master_plan

    paper = analyse("Hansa Präzisionsteile", "https://www.hansa-praezision.test", complete=ManufacturingModel())
    row = {"id": "33333333-3333-4333-8333-333333333333", "opportunity_id": paper["opportunity_id"], "document_id": paper["document_id"], "status": "approved", "paper_json": paper}
    plan = build_master_plan(row)
    assert plan["slides"]
    deck = json.dumps(plan, ensure_ascii=False)
    analysis = paper["analysis"]
    # Master V1 takes the areas, opportunities and decision gates - unchanged by this calibration.
    for text in (analysis["areas"][0]["name"], analysis["areas"][0]["opportunities"][0]["title"], analysis["target_workflows"][0]["human_decision_gate"]):
        assert text in deck, text
    assert paper["presentation_brief"]["core_thesis"] == analysis["research"]["core_thesis"]["text"]


# --------------------------------------------------------------- PR #12 review findings


AI_ONLY_STEPS = [
    "Reads the drawing, validates the dimensions and sends the quote automatically.",
    "The model classifies each claim and approves clean ones.",
    "The system reviews every record and releases compliant batches.",
    "Extracts positions, adjusts prices and confirms the order.",
    "AI decides the priority and steers the schedule.",
    "The assistant checks the invoice and releases payment.",
    "Matches orders against stock and sends confirmations to customers.",
]
HUMAN_STEPS = [
    "Reads the request, extracts scope and drafts the proposal; business development leads refine the strategy.",
    "Structures inclusion criteria, checks records and flags likely fails; clinicians decide every case.",
    "Collects results against specifications and assembles the release pack; the Qualified Person reviews and releases.",
    "Translates call-offs into staffing needs per shift and proposes deployments; operations management decides.",
    "Generates documents from approved templates; HR checks and signs.",
    "Extracts, codes and matches; clean cases flow through, deviations go to a person with context.",
    "Proposes the route and the loading order; the shift lead confirms before release.",
    "Drafts the customs papers per shipment; customs specialists check special cases.",
    "Builds the tour plan from orders and driver hours; dispatchers approve the plan.",
    "Prepares the dose record; the duty pharmacist releases it.",
    "Ranks candidates for the slot; the site manager chooses.",
    "Drafts the change order with evidence; it is approved by the account manager.",
    "Structures the event and drafts the assessment; QA classifies, decides and approves.",
    "Completes missing fields and routes the request; buyers see only cases that need a decision.",
]


@pytest.mark.parametrize("text", AI_ONLY_STEPS)
def test_finding_1_an_automated_step_is_not_a_human_decision(text: str) -> None:
    assert not live.names_human_step(text)
    answer = library_deep_dive()
    answer["opportunities"][0]["how_it_works"] = text
    assert any("not the AI or the system" in problem for problem in live.quality_problems("deep_dives", answer))


@pytest.mark.parametrize("text", HUMAN_STEPS)
def test_finding_1_a_named_person_or_function_is_recognised(text: str) -> None:
    assert live.names_human_step(text), "no false rejection of a legitimate human step"


@pytest.mark.parametrize(
    "question",
    ["Does this cost you days?", "Is the error rate known?", "Do you track hours per batch?", "Are weekly costs reported?", "Can you see the costs?"],
)
def test_finding_2_a_yes_no_question_is_not_measurable(question: str) -> None:
    assert not live.measurable_question(question)
    answer = library_deep_dive()
    answer["opportunities"][0]["discovery_questions"] = [question, "Do you use spreadsheets for it?"]
    assert any("yes/no questions do not count" in problem for problem in live.quality_problems("deep_dives", answer))


@pytest.mark.parametrize(
    "question",
    [
        "How many days does a full quotation take today?",
        "What does one week of unplanned overtime cost?",
        "When do you learn that an order will slip?",
        "Which share of claims is audited per month?",
        "Do you know how many credit notes are checked line by line?",
        "What is the no-show rate per cohort?",
        "Is planning based on call-offs - and how often does it change per week?",
        "Can you estimate the cost per order?",
    ],
)
def test_finding_2_open_and_quantity_questions_stay_measurable(question: str) -> None:
    assert live.measurable_question(question)


def test_the_library_names_a_person_for_every_decision() -> None:
    for area in load_library()["areas"]:
        for item in area["opportunities"]:
            assert live.names_human_step(item["how_it_works"]), item["id"]
            assert any(live.measurable_question(question) for question in item["discovery_questions"]), item["id"]


def test_finding_3_warnings_are_stored_bounded_and_accepted_by_the_contract() -> None:
    from services.framework.discovery_analysis.model import validate_discovery_analysis

    paper = generate(POPULATED, complete=WeakFirstModel(second="weak"))
    validate_discovery_analysis(paper)
    paper["generation"]["quality_warnings"] = ["x" * 300] * live.MAX_QUALITY_WARNINGS
    validate_discovery_analysis(paper)
    paper["generation"]["quality_warnings"].append("one too many")
    with pytest.raises(Exception, match="quality_warnings"):
        validate_discovery_analysis(paper)
