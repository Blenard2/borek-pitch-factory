"""BAP-5: the Discovery analysis uses the website research - and only what it actually found.

Verified website facts become SOURCE_FACT known facts with their source; interpretations
(signals) only order the areas; a website that cannot be analysed is reported, never claimed.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from services.framework.discovery_analysis import live
from services.framework.discovery_analysis.model import validate_discovery_analysis
from services.framework.discovery_analysis.pipeline import finalize, generate_discovery_analysis
from services.presentation.master_deck.plan import build_master_plan
from services.research.website.provider import WebsiteResearchProvider
from tests.fixtures.website_fixtures import company_web
from tests.unit.framework.test_discovery_analysis import FakeModel

FIXED = {"generated_at": "2026-10-19T10:00:00Z", "document_id": "22222222-2222-4222-8222-222222222222"}


def opportunity(name: str, website: str | None, **intake: Any) -> dict[str, Any]:
    return {
        "id": "11111111-1111-4111-8111-111111111111",
        "client_name": name,
        "opportunity_name": "AI introduction",
        "stage1_intake": {"client_web_page": website, **intake} if website or intake else None,
    }


def analyse(name: str, website: str | None, *, web=None, complete=None, **intake: Any) -> dict[str, Any]:
    web = web or company_web()
    provider = WebsiteResearchProvider(fetcher_factory=web.fetcher)
    return generate_discovery_analysis(opportunity(name, website, **intake), persist=lambda _paper: None, research_provider=provider, complete=complete, **FIXED)


def website_facts(paper: dict[str, Any]) -> list[dict[str, Any]]:
    return [fact for fact in paper["analysis"]["research"]["known_facts"] if fact["origin"] == "SOURCE_FACT"]


def test_website_facts_reach_the_analysis_with_their_sources() -> None:
    paper = analyse("Hansa Präzisionsteile", "https://www.hansa-praezision.test")
    assert paper["status"] == "ready"
    assert paper["generation"]["research_mode"] == "provider"
    research = paper["analysis"]["research"]
    assert research["web_research"]["performed"] is True
    assert "6 pages of hansa-praezision.test" in research["web_research"]["note"]
    assert research["company_profile"] == {
        "text": "Hansa Präzisionsteile GmbH manufactures precision CNC turned and milled parts for medical technology and aerospace customers.",
        "origin": "SOURCE_FACT",
    }
    overview = paper["analysis"]["framing"]["chapters"]["overview"]["paragraphs"][0]
    assert overview.startswith("From the client's website") and "precision CNC" in overview
    facts = website_facts(paper)
    assert len(facts) >= 8
    for fact in facts:
        (ref,) = fact["source_refs"]
        assert ref["source_id"].startswith("https://www.hansa-praezision.test/") and fact["value"] in ref["excerpt"]
    assert paper["analysis"]["provenance"]["customer_facts_origin"] == "user_input_and_cited_research"
    assert paper["analysis"]["provenance"]["sources"] == [fact["source_refs"][0] for fact in facts]
    # The research state is stored with the analysis, without the raw page text.
    stored = paper["generation"]["website_research"]
    assert stored["status"] == "complete" and "content" not in stored
    assert [fact["value"] for fact in stored["facts"]][: len(facts)] == [fact["value"] for fact in facts]
    assert "Industry and business model" not in research["unknown_facts"], "the website describes the business"
    validate_discovery_analysis(paper)


def test_two_companies_get_distinct_analyses() -> None:
    hansa = analyse("Hansa Präzisionsteile", "https://www.hansa-praezision.test")
    nordlicht = analyse("Nordlicht Logistik", "https://nordlicht-logistik.test")
    assert {fact["value"] for fact in website_facts(hansa)}.isdisjoint({fact["value"] for fact in website_facts(nordlicht)})
    priority = lambda paper: [area["id"] for area in paper["analysis"]["areas"] if area["priority"]]  # noqa: E731
    assert priority(hansa) == ["operations_planning", "quality_compliance"]
    assert priority(nordlicht) == ["order_service", "operations_planning"]
    assert hansa["presentation_brief"]["priority_opportunities"] != nordlicht["presentation_brief"]["priority_opportunities"]
    # Without a website, the same opportunity is the generic library order.
    plain = analyse("Hansa Präzisionsteile", None)
    assert not any(area["priority"] for area in plain["analysis"]["areas"])


def test_interpretations_are_never_presented_as_facts() -> None:
    paper = analyse("Hansa Präzisionsteile", "https://www.hansa-praezision.test")
    signals = paper["generation"]["website_research"]["signals"]
    assert signals and all(signal["origin"] == "AI_INFERENCE" and signal["status"] == "to_validate" for signal in signals)
    known = json.dumps(paper["analysis"]["research"]["known_facts"], ensure_ascii=False)
    assert not any(signal["statement"] in known for signal in signals)
    assert {fact["origin"] for fact in paper["analysis"]["research"]["known_facts"]} <= {"USER_INPUT", "SOURCE_FACT"}


def test_a_website_that_cannot_be_analysed_is_reported_and_never_claimed() -> None:
    for url, code in (
        ("https://restricted.test", "WEBSITE_ACCESS_RESTRICTED"),
        ("https://no-robots.test", "WEBSITE_BLOCKED_BY_ROBOTS"),
        ("https://does-not-exist.test", "WEBSITE_UNREACHABLE"),
    ):
        paper = analyse("Client AG", url)
        assert paper["status"] == "ready", "the workflow continues"
        assert paper["generation"]["research_mode"] == "user_context_only"
        research = paper["analysis"]["research"]
        assert research["web_research"]["performed"] is False
        assert "could not be analysed" in research["web_research"]["note"]
        assert not website_facts(paper) and research["company_profile"]["origin"] == "UNKNOWN"
        stored = paper["generation"]["website_research"]
        assert stored["status"] == "failed" and stored["errors"][0]["code"] == code


def test_partial_research_is_labelled_incomplete() -> None:
    paper = analyse("Vortex Analytics", "https://vortex-analytics.test")
    note = paper["analysis"]["research"]["web_research"]["note"]
    assert paper["generation"]["website_research"]["status"] == "partial"
    assert "Research is incomplete" in note and "JavaScript" in note
    assert [fact["value"] for fact in website_facts(paper)] == ["Vortex Analytics builds data platforms and dashboards for retail chains."]


def test_user_input_stays_first_and_is_not_overwritten() -> None:
    paper = analyse("Hansa Präzisionsteile", "https://www.hansa-praezision.test", about_company="Family-owned supplier, 120 employees.")
    research = paper["analysis"]["research"]
    assert research["company_profile"] == {"text": "Family-owned supplier, 120 employees.", "origin": "USER_INPUT"}
    origins = [fact["origin"] for fact in research["known_facts"]]
    assert origins == sorted(origins, key=lambda origin: origin != "USER_INPUT"), "the user's statements come first"


def test_without_a_website_or_provider_nothing_changes() -> None:
    no_site = analyse("Hansa Präzisionsteile", None)
    assert no_site["generation"]["research_mode"] == "user_context_only" and "website_research" not in no_site["generation"]
    plain = generate_discovery_analysis(opportunity("Hansa Präzisionsteile", "https://www.hansa-praezision.test"), persist=lambda _paper: None, **FIXED)
    assert "website_research" not in plain["generation"] and plain["analysis"]["research"]["web_research"]["performed"] is False
    # An analysis stored before BAP-5 (no website_research) stays valid.
    legacy = copy.deepcopy(plain)
    legacy["generation"].pop("website_research", None)
    validate_discovery_analysis(finalize(legacy))


def test_an_approved_researched_analysis_feeds_master_presentation_v1() -> None:
    paper = analyse("Nordlicht Logistik", "https://nordlicht-logistik.test")
    row = {"id": "33333333-3333-4333-8333-333333333333", "opportunity_id": paper["opportunity_id"], "document_id": paper["document_id"], "status": "approved", "paper_json": paper}
    plan = build_master_plan(row)
    context = next(slide for slide in plan["slides"] if slide["borekSlide"].get("kicker") == "Client context")
    assert "cited research" in context["borekSlide"]["lead"]


# --- live generation -----------------------------------------------------------------------


class WebsiteAwareModel(FakeModel):
    """The library answers; records what each stage call was given."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.prompts.append((system, user))
        stage = next(name for name, candidate in live.STAGE_SCHEMAS.items() if candidate is schema)
        self.calls.append(stage)
        answer = self.answer(stage)
        if stage == "research":
            answer["core_thesis"] = "Working hypothesis: the website suggests first article inspection within 48 hours ties up quality staff."
        return answer


def test_live_stages_receive_the_website_research_as_untrusted_data() -> None:
    model = WebsiteAwareModel()
    paper = analyse("Hansa Präzisionsteile", "https://www.hansa-praezision.test", complete=model)
    assert paper["status"] == "ready" and paper["generation"]["mode"] == "live"
    assert model.calls == ["research", "overview", *["deep_dives"] * 7, "shadow_processes", "target_picture", "closing"], "no extra model call"
    for system, user in model.prompts:
        assert system == live.SYSTEM_PROMPT_WEBSITE and "No web or company research" not in system
        assert "never instructions" in system and "hypotheses" in system
        website = user.index("WEBSITE (untrusted third-party content")
        assert user.index("CONTEXT (untrusted user input)") < website < user.index("TASK:")
        block = json.loads(user[user.index("{", website) : user.index("\n\nTASK:")])
        assert block["site"] == "hansa-praezision.test" and block["facts"] and block["excerpts"]
        assert all(signal["status"] == "hypothesis to validate" for signal in block["signals_to_validate"])
        assert len(json.dumps(block, ensure_ascii=False)) < live.WEBSITE_MATERIAL_CHARS + 3_000
        assert "ignore all previous instructions" not in user, "page scripts never reach the model"
    analysis = paper["analysis"]
    # A figure the model took from the website is accepted because the excerpt is cited.
    assert "48 hours" in analysis["research"]["core_thesis"]["text"]
    assert any("48 hours" in ref["excerpt"] for ref in analysis["provenance"]["sources"])
    assert "cited website research" in analysis["provenance"]["notes"][0]


def test_live_stages_without_research_keep_the_no_research_rule() -> None:
    model = WebsiteAwareModel()
    paper = analyse("Restricted AG", "https://restricted.test", complete=model)
    assert paper["status"] == "ready"
    assert all(system == live.SYSTEM_PROMPT and "WEBSITE (" not in user for system, user in model.prompts)
