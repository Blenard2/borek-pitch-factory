"""BAP-10: the Discovery document shows what the website research found, with its sources.

* Successful research prints a "Company background" table (an M6 page in part 1): topic, the
  statement quoted from the website, and the page it comes from.
* After successful research no "no research was performed" wording is left anywhere.
* Failed or disabled research is never presented as successful, and earlier analyses keep the
  pages they were stored with.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from services.framework.discovery_analysis.fixture import load_library
from services.framework.discovery_analysis.layout import build_page_manifest, company_background, source_label
from services.framework.discovery_analysis.model import M6_ROWS_PER_PAGE, validate_discovery_analysis
from services.framework.discovery_analysis.pipeline import finalize, generate_discovery_analysis
from services.presentation.master_deck.plan import build_master_plan
from services.research.website.provider import WebsiteResearchProvider
from tests.fixtures.website_fixtures import HANSA_PAGES, FixtureWeb, company_web
from tests.unit.framework.test_bap5_discovery_website import WebsiteAwareModel

FIXED = {"generated_at": "2026-10-20T10:00:00Z", "document_id": "22222222-2222-4222-8222-222222222222"}
HANSA = "https://www.hansa-praezision.test"
LIBRARY = load_library()
GENERIC = (
    LIBRARY["chapters"]["overview"]["profile_unknown"],
    LIBRARY["in_short"]["scope_context"],
    LIBRARY["research"]["web_research_note"],
    "no company research was performed",
    "no verified company research",
    "No web or company research",
)


def opportunity(name: str, website: str | None, **intake: Any) -> dict[str, Any]:
    return {
        "id": "11111111-1111-4111-8111-111111111111",
        "client_name": name,
        "opportunity_name": "",
        "stage1_intake": {"client_web_page": website, **intake} if website or intake else None,
    }


def analyse(name: str, website: str | None, *, web: FixtureWeb | None = None, provider: bool = True, complete=None, **intake: Any) -> dict[str, Any]:
    research = WebsiteResearchProvider(fetcher_factory=(web or company_web()).fetcher) if provider else None
    return generate_discovery_analysis(opportunity(name, website, **intake), persist=lambda _paper: None, research_provider=research, complete=complete, **FIXED)


def printed(paper: dict[str, Any]) -> str:
    return json.dumps(paper["page_manifest"], ensure_ascii=False)


def background_pages(paper: dict[str, Any]) -> list[dict[str, Any]]:
    return [page for page in paper["page_manifest"] if page["id"].startswith("company-background-")]


def test_successful_research_prints_the_company_background_with_sources() -> None:
    paper = analyse("Hansa Präzisionsteile", HANSA)
    assert paper["status"] == "ready" and paper["generation"]["website_research"]["schema_version"] == "1.1"
    pages = background_pages(paper)
    assert pages and all(page["type"] == "M6" and not page["dark"] for page in pages)
    ids = [page["id"] for page in paper["page_manifest"]]
    assert ids.index("ch-overview") < ids.index(pages[0]["id"]) < ids.index("overview-map-1"), "part 1, before the opportunity map"
    assert pages[0]["chapter"]["name"] == paper["analysis"]["framing"]["chapters"]["overview"]["name"]
    assert pages[0]["edit_targets"] == [], "quotes from the website are not edited in the document"
    content = pages[0]["content"]
    assert content["columns"] == ["Topic", "Statement on the website", "Source"]
    assert "quoted" in content["lead"] and "hypotheses to validate" in content["lead"]
    assert content["footnote"] == "Read from hansa-praezision.test on 20 October 2026. Statements are quoted as published on the website and were not verified independently."
    rows = [row for page in pages for row in page["content"]["rows"]]
    facts = [fact for fact in paper["analysis"]["research"]["known_facts"] if fact["origin"] == "SOURCE_FACT"]
    assert len(rows) == len(facts)
    for row, fact in zip(rows, facts, strict=True):
        assert row["description"] == fact["value"], "the statement is printed as quoted"
        assert row["source_url"] == fact["source_refs"][0]["source_id"]
        assert row["verdict"] == source_label(row["source_url"]) and "(website)" not in row["subject"]
    topics = {row["subject"] for row in rows}
    assert {"Company description", "Service", "Product", "Industry served", "Address", "Founded"} <= topics
    assert any("CNC turning" in row["description"] for row in rows) and any(row["description"] == "Medical technology" for row in rows)
    validate_discovery_analysis(paper)


def test_no_generic_no_research_wording_after_successful_research() -> None:
    paper = analyse("Hansa Präzisionsteile", HANSA)
    text = printed(paper) + json.dumps(paper["analysis"], ensure_ascii=False)
    for phrase in GENERIC:
        assert phrase not in text, phrase
    assert paper["analysis"]["framing"]["chapters"]["overview"]["paragraphs"][0].startswith("From the client's website (quoted; the sources are listed in the company background table)")


def _site_without_description() -> FixtureWeb:
    home = "<html><body><a href='/services'>Services</a><h1>Kessler Metallbau</h1><p>Metallbau und Stahlbau aus Kassel für Industrie und Gewerbe.</p></body></html>"
    services = "<html><body><main><h1>Services</h1><h2>Stahlbau</h2><h2>Treppen und Geländer</h2><h2>Schweißarbeiten nach EN 1090</h2></main></body></html>"
    return FixtureWeb(sites={"kessler-metallbau.test": {"/": home, "/services": services, "/robots.txt": None}})


def test_research_without_a_description_still_gives_a_specific_profile() -> None:
    paper = analyse("Kessler Metallbau", "https://kessler-metallbau.test", web=_site_without_description())
    research = paper["analysis"]["research"]
    assert paper["generation"]["research_mode"] == "provider"
    assert not any(fact["label"].startswith(("Company description", "About")) for fact in research["known_facts"])
    assert research["company_profile"] == {
        "text": "Services and products named on the website: Stahlbau; Treppen und Geländer; Schweißarbeiten nach EN 1090.",
        "origin": "SOURCE_FACT",
    }
    overview = paper["analysis"]["framing"]["chapters"]["overview"]["paragraphs"][0]
    assert "Stahlbau; Treppen und Geländer" in overview
    for phrase in GENERIC:
        assert phrase not in printed(paper), phrase
    validate_discovery_analysis(paper)


def test_a_long_client_name_does_not_bring_back_no_research_wording() -> None:
    name = (
        "Hansa Präzisionsteile Fertigungs- und Vertriebsgesellschaft für Medizintechnik, Luftfahrt, "
        "Sondermaschinenbau und Prototypenfertigung Bremen-Nord, Zweigniederlassung Hafenstraße"
    )
    named = LIBRARY["in_short"]["scope_named"].format(client=name)
    assert len(named) > 320, "the name is too long for the named scope wording"
    researched = analyse(name, HANSA)
    assert researched["analysis"]["framing"]["document"]["scope_note"] == LIBRARY["in_short"]["scope_researched"]
    # Without research the long name keeps the honest wording.
    plain = analyse(name, HANSA, provider=False)
    assert plain["analysis"]["framing"]["document"]["scope_note"] == LIBRARY["in_short"]["scope_context"]


def test_failed_research_is_not_presented_as_successful() -> None:
    for url in ("https://restricted.test", "https://does-not-exist.test", "https://no-robots.test"):
        paper = analyse("Client AG", url)
        assert paper["generation"]["research_mode"] == "user_context_only"
        assert not background_pages(paper)
        research = paper["analysis"]["research"]
        assert research["web_research"]["performed"] is False and research["company_profile"]["origin"] == "UNKNOWN"
        assert paper["analysis"]["framing"]["chapters"]["overview"]["paragraphs"][0] == LIBRARY["chapters"]["overview"]["profile_unknown"]


def test_disabled_research_keeps_the_document_unchanged() -> None:
    off = analyse("Hansa Präzisionsteile", HANSA, provider=False)
    assert "website_research" not in off["generation"] and not background_pages(off)
    assert off["analysis"]["research"]["web_research"]["note"] == LIBRARY["research"]["web_research_note"]
    # The same document as without any website at all, apart from the website itself.
    without = analyse("Hansa Präzisionsteile", None, provider=False)
    assert [page["id"] for page in off["page_manifest"]] == [page["id"] for page in without["page_manifest"]]


def test_analyses_stored_before_bap10_keep_their_pages() -> None:
    """A BAP-5 analysis (research 1.0) was stored without the background page; it stays valid."""
    paper = analyse("Hansa Präzisionsteile", HANSA)
    earlier = copy.deepcopy(paper)
    earlier["generation"]["website_research"]["schema_version"] = "1.0"
    earlier["page_manifest"] = build_page_manifest(earlier)
    assert not background_pages(earlier) and company_background(earlier) == ([], None)
    validate_discovery_analysis(earlier)  # what approval checks
    assert len(earlier["page_manifest"]) == len(paper["page_manifest"]) - len(background_pages(paper))


def test_many_facts_continue_on_further_m6_pages_without_loss() -> None:
    services = "<html><body><main><h1>Services</h1>" + "".join(
        f"<h2>Service line {number}: precision machining of {'large ' * (number % 4)}housings for aerospace and medical customers</h2>" for number in range(1, 9)
    ) + "</main></body></html>"
    pages = {**HANSA_PAGES, "/services": services}
    paper = analyse("Hansa Präzisionsteile", HANSA, web=FixtureWeb(sites={"www.hansa-praezision.test": pages}))
    background = background_pages(paper)
    rows = [row for page in background for row in page["content"]["rows"]]
    assert len(background) >= 2 and all(len(page["content"]["rows"]) <= M6_ROWS_PER_PAGE for page in background)
    assert len(rows) == len({row["id"] for row in rows}) == len([f for f in paper["analysis"]["research"]["known_facts"] if f["origin"] == "SOURCE_FACT"])
    assert [page["nav_label"] for page in background] == [f"Company background ({index}/{len(background)})" for index in range(1, len(background) + 1)]
    validate_discovery_analysis(finalize(copy.deepcopy(paper)))


def test_long_source_addresses_are_shortened_for_print_but_linked_in_full() -> None:
    long_url = "https://www.hansa-praezision.test/de/unternehmen/leistungen/cnc-fertigung/fuenfachsfraesen-fuer-luftfahrtkomponenten/"
    assert source_label("https://www.hansa-praezision.test/") == "hansa-praezision.test"
    assert source_label("https://www.hansa-praezision.test/about-us") == "hansa-praezision.test/about-us"
    label = source_label(long_url)
    assert len(label) <= 48 and label.startswith("hansa-praezision.test/…/")


def test_two_companies_get_distinct_documents() -> None:
    hansa = analyse("Hansa Präzisionsteile", HANSA)
    nordlicht = analyse("Nordlicht Logistik", "https://nordlicht-logistik.test")
    rows = lambda paper: {row["description"] for page in background_pages(paper) for row in page["content"]["rows"]}  # noqa: E731
    assert rows(hansa) and rows(nordlicht) and rows(hansa).isdisjoint(rows(nordlicht))
    overview = lambda paper: paper["analysis"]["framing"]["chapters"]["overview"]["paragraphs"][0]  # noqa: E731
    assert "CNC" in overview(hansa) and "logistics" in overview(nordlicht)


def test_live_generation_prints_the_same_background_and_sources() -> None:
    model = WebsiteAwareModel()
    paper = analyse("Hansa Präzisionsteile", HANSA, complete=model)
    assert paper["status"] == "ready" and paper["generation"]["mode"] == "live"
    assert background_pages(paper), "the background page is code, not model output"
    for _system, user in model.prompts:
        assert "CNC turning up to 65 mm diameter" in user, "the services reach the model"
    for phrase in GENERIC:
        assert phrase not in printed(paper), phrase


def test_an_approved_researched_document_still_feeds_master_presentation_v1() -> None:
    paper = analyse("Nordlicht Logistik", "https://nordlicht-logistik.test")
    row = {"id": "33333333-3333-4333-8333-333333333333", "opportunity_id": paper["opportunity_id"], "document_id": paper["document_id"], "status": "approved", "paper_json": paper}
    plan = build_master_plan(row)
    assert plan["slides"][-1]["borekSlide"]["title"] == "Let’s talk"
    context = next(slide for slide in plan["slides"] if slide["borekSlide"].get("kicker") == "Client context")
    assert "cited research" in context["borekSlide"]["lead"]
