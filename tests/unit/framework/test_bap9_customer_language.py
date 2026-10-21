"""BAP-9 (phase 1): a newly generated Discovery document reads as a customer document.

No process commentary, no talk about inputs, research or how the document was produced, and no
"the client" - while website facts stay attributed to the website with their sources, the user's
own text stays recognisable as background, and benchmarks and hypotheses stay labelled as such.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from services.framework.discovery_analysis import live
from services.framework.discovery_analysis.fixture import load_library
from services.framework.discovery_analysis.layout import background_lead
from services.framework.discovery_analysis.pipeline import DiscoveryAnalysisStageError, generate_discovery_analysis
from services.research.website.provider import WebsiteResearchProvider
from tests.fixtures.website_fixtures import company_web
from tests.unit.framework.test_discovery_analysis import FakeModel

FIXED = {"generated_at": "2026-10-21T10:00:00Z", "document_id": "22222222-2222-4222-8222-222222222222"}
LIBRARY = load_library()
OVERVIEW = LIBRARY["chapters"]["overview"]

# Wording that must never reach a customer document generated from now on.
INTERNAL = (
    "no company research", "no verified company research", "no web or company research", "research was performed",
    "Context provided for this analysis", "the client's", "the client ", "no company context was available",
    "nothing here is a statement", "generic discussion basis", "Some approaches pick up", "here the object is",
    "famous spreadsheet", "classic first win", "the user", "fixture", "CONTEXT", "placeholder", "prompt",
)


def opportunity(name: str, website: str | None = None, **intake: Any) -> dict[str, Any]:
    stage1 = {key: value for key, value in {"client_web_page": website, **intake}.items() if value}
    return {"id": "11111111-1111-4111-8111-111111111111", "client_name": name, "opportunity_name": "", "stage1_intake": stage1 or None}


def generate(name: str, website: str | None = None, *, research: bool = False, complete=None, **intake: Any) -> dict[str, Any]:
    provider = WebsiteResearchProvider(fetcher_factory=company_web().fetcher) if research else None
    return generate_discovery_analysis(opportunity(name, website, **intake), persist=lambda _paper: None, research_provider=provider, complete=complete, **FIXED)


def printed(paper: dict[str, Any]) -> str:
    """Every text of the document as printed (preview and PDF are rendered from these pages)."""
    return json.dumps([page["content"] for page in paper["page_manifest"]], ensure_ascii=False)


def profile(paper: dict[str, Any]) -> str:
    return paper["analysis"]["framing"]["chapters"]["overview"]["paragraphs"][0]


SCENARIOS = {
    "researched": lambda: generate("Hansa Präzisionsteile", "https://www.hansa-praezision.test", research=True),
    "context_only": lambda: generate("Wahl & CO", about_company="Family-owned electrical engineering group with four locations."),
    "name_only": lambda: generate("Wahl & CO"),
    "blank": lambda: generate(""),
    "research_failed": lambda: generate("Restricted AG", "https://restricted.test", research=True),
    "research_disabled": lambda: generate("Hansa Präzisionsteile", "https://www.hansa-praezision.test"),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_no_internal_language_is_printed(scenario: str) -> None:
    paper = SCENARIOS[scenario]()
    assert paper["status"] == "ready"
    text = printed(paper)
    for phrase in INTERNAL:
        assert phrase not in text, f"{scenario}: {phrase!r}"


def test_fallbacks_name_the_company_and_disclose_the_basis() -> None:
    expected = OVERVIEW["profile_unknown_named"].format(client="Wahl & CO")
    assert profile(SCENARIOS["name_only"]()) == expected
    assert profile(SCENARIOS["research_failed"]()) == OVERVIEW["profile_unknown_named"].format(client="Restricted AG")
    assert profile(SCENARIOS["research_disabled"]()) == OVERVIEW["profile_unknown_named"].format(client="Hansa Präzisionsteile")
    assert profile(SCENARIOS["blank"]()) == OVERVIEW["profile_unknown"]
    for text in (expected, OVERVIEW["profile_unknown"]):
        assert "patterns we see in" in text and "confirmed or set aside together" in text, "the basis is disclosed, without process talk"


def test_sources_of_the_company_information_stay_distinguishable() -> None:
    researched = SCENARIOS["researched"]()
    # Website: attributed to the website, with the sources table.
    assert profile(researched).startswith("From the Hansa Präzisionsteile website (sources in the company background table): ")
    facts = [fact for fact in researched["analysis"]["research"]["known_facts"] if fact["origin"] == "SOURCE_FACT"]
    assert facts and all(fact["source_refs"][0]["source_id"].startswith("https://www.hansa-praezision.test/") for fact in facts)
    background = next(page for page in researched["page_manifest"] if page["id"] == "company-background-1")
    assert background["content"]["lead"].startswith("What Hansa Präzisionsteile publishes about itself on its website, quoted")
    assert all(row["source_url"].startswith("https://www.hansa-praezision.test/") for row in background["content"]["rows"])
    # User-provided: recognisable as the background given for this conversation.
    context = SCENARIOS["context_only"]()
    assert profile(context) == "Background to this conversation: Family-owned electrical engineering group with four locations."
    assert context["analysis"]["research"]["company_profile"]["origin"] == "USER_INPUT"
    # Hypotheses and benchmarks stay labelled.
    for paper in (researched, context):
        text = printed(paper)
        assert "Working hypothesis" in text and "(industry benchmark)" in text
        assert "Industry benchmarks as working hypotheses" in text and "not commitments" in paper["analysis"]["framing"]["document"]["scope_note"]


def test_scope_notes_are_customer_facing() -> None:
    blank = SCENARIOS["blank"]()["analysis"]["framing"]["document"]["scope_note"]
    assert blank == LIBRARY["in_short"]["scope_generic"] and "patterns from comparable organisations" in blank
    named = SCENARIOS["name_only"]()["analysis"]["framing"]["document"]["scope_note"]
    assert named == LIBRARY["in_short"]["scope_named"].format(client="Wahl & CO")
    long_name = "Wahl & CO Elektro-, Informations- und Sicherheitstechnik Gesellschaft mit beschränkter Haftung und Compagnie Kommanditgesellschaft, Niederlassung Hannover-Seelze und Braunschweig"
    assert generate(long_name)["analysis"]["framing"]["document"]["scope_note"] == LIBRARY["in_short"]["scope_context"] == (
        "Prepared as a discussion basis for our conversation. All impact figures are industry benchmarks as working hypotheses, "
        "not commitments. People retain every decision, release and sign-off."
    )


def test_the_background_lead_is_versioned_with_the_research_record() -> None:
    paper = SCENARIOS["researched"]()
    assert paper["generation"]["website_research"]["schema_version"] == "1.2"
    earlier = {**paper, "generation": {**paper["generation"], "website_research": {**paper["generation"]["website_research"], "schema_version": "1.1"}}}
    assert background_lead(earlier) == OVERVIEW["background_lead_1_1"], "documents stored by BAP-10 keep their sentence"
    unnamed = {**paper, "intake_context": {**paper["intake_context"], "client_name": ""}}
    assert background_lead(unnamed).startswith("What the company publishes about itself")


def test_a_company_name_with_braces_is_printed_as_text() -> None:
    web = company_web()
    paper = generate_discovery_analysis(
        opportunity("ACME {Test} GmbH", "https://www.hansa-praezision.test"),
        persist=lambda _paper: None,
        research_provider=WebsiteResearchProvider(fetcher_factory=web.fetcher),
        **FIXED,
    )
    assert profile(paper).startswith("From the ACME {Test} GmbH website")


# --- live generation: the model's own wording is checked, with the existing bounded retry --------


class LeakingModel(FakeModel):
    """Writes process commentary into its first research answers."""

    def __init__(self, leaks: int, text: str) -> None:
        super().__init__()
        self.leaks = leaks
        self.text = text
        self.users: list[str] = []

    def __call__(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        answer = super().__call__(system, user, schema)
        stage = self.calls[-1]
        if stage == "research":
            self.users.append(user)
            if self.leaks > 0:
                self.leaks -= 1
                answer = {**answer, "overview_context": self.text}
        return answer


def test_live_process_commentary_is_retried_once_and_then_corrected() -> None:
    model = LeakingModel(1, "Based on the limited context provided, Wahl & CO probably loses time around its core work.")
    paper = generate("Wahl & CO", complete=model)
    assert paper["status"] == "ready"
    assert model.calls.count("research") == 2, "one bounded retry"
    assert "is not written for the reader" in model.users[1] and "overview_context" in model.users[1]
    assert "context provided" not in printed(paper)


def test_live_process_commentary_that_persists_fails_the_stage() -> None:
    model = LeakingModel(5, "As CONTEXT is thin, this section stays generic.")
    with pytest.raises(DiscoveryAnalysisStageError, match="Research"):
        generate("Wahl & CO", complete=model)
    assert model.calls.count("research") == 2, "never more than the existing two attempts"


def test_live_wording_about_the_companys_own_clients_is_not_rejected() -> None:
    text = "Wahl & CO's clients expect fast quotes; the client portal and research on past orders make that possible in context."
    model = LeakingModel(1, text)
    paper = generate("Wahl & CO", complete=model)
    assert model.calls.count("research") == 1, "legitimate words like client, research and context are not banned"
    assert paper["analysis"]["framing"]["chapters"]["overview"]["paragraphs"][1] == text


def test_the_live_rules_ask_for_customer_ready_prose() -> None:
    for prompt in (live.SYSTEM_PROMPT, live.SYSTEM_PROMPT_WEBSITE):
        assert "The reader is the company's management" in prompt
        assert "Do not call the company 'the client'" in prompt
        assert "never write notes, placeholders or planning comments" in prompt
    assert live.PROMPT_VERSION == "discovery-analysis:v3"
    assert not re.search(r"keep the wording generic", live.SYSTEM_PROMPT)
