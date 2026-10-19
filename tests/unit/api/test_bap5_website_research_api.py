"""BAP-5 through the API: Client Information website -> research -> Discovery -> approval -> V1."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import create_app
from app.services import journey_generation, stage1
from app.services.data.memory_store import reset_memory_store
from services.research.website.provider import WebsiteResearchProvider
from tests.fixtures.website_fixtures import company_web
from tests.unit.api.test_master_presentation_api import approve, generate_deck, headers


def body(name: str, website: str) -> dict:
    return {
        "client_name": name,
        "opportunity_name": "AI introduction",
        "department": "Operations",
        "stage1_intake": {"client_web_page": website, "poc_name": "Dana Weber"},
    }


@pytest.fixture()
def researched(monkeypatch: pytest.MonkeyPatch):
    """The API with the website research provider reading the fixture websites."""
    reset_memory_store()
    web = company_web()
    provider = WebsiteResearchProvider(fetcher_factory=web.fetcher)
    app = create_app()
    app.dependency_overrides[stage1.get_company_research_provider] = lambda: provider
    monkeypatch.setattr(journey_generation, "get_company_research_provider", lambda: provider)
    with TestClient(app) as client:
        yield client, web


def create(client: TestClient, name: str, website: str) -> str:
    response = client.post("/opportunities", headers=headers(), json=body(name, website))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_the_discovery_of_each_client_is_built_from_its_own_website(researched) -> None:
    client, web = researched
    papers = {}
    for name, website in (("Hansa Präzisionsteile", "https://www.hansa-praezision.test"), ("Nordlicht Logistik", "https://nordlicht-logistik.test")):
        opportunity_id = create(client, name, website)
        approved = approve(client, opportunity_id)
        papers[name] = approved["paper_json"]
        paper = papers[name]
        assert paper["generation"]["research_mode"] == "provider"
        assert paper["generation"]["website_research"]["status"] == "complete"
        sources = {fact["source_refs"][0]["source_id"] for fact in paper["analysis"]["research"]["known_facts"] if fact["origin"] == "SOURCE_FACT"}
        assert sources and all(website.split("//", 1)[1] in source for source in sources)
        # The approved Discovery drives Master Presentation V1 as before.
        presentation = generate_deck(client, opportunity_id)
        assert presentation["status"] == "ready", presentation
    hansa, nordlicht = papers["Hansa Präzisionsteile"], papers["Nordlicht Logistik"]
    assert hansa["analysis"]["research"]["company_profile"] != nordlicht["analysis"]["research"]["company_profile"]
    assert hansa["presentation_brief"]["priority_opportunities"] != nordlicht["presentation_brief"]["priority_opportunities"]
    # Stage 1 research cites the same website evidence, read once per site thanks to the cache.
    before = len(web.requests)
    opportunity_id = create(client, "Hansa Präzisionsteile", "https://www.hansa-praezision.test")
    facts = client.post(f"/opportunities/{opportunity_id}/stage1-research", headers=headers()).json()["company_facts"]
    assert facts["description"]["status"] == "verified" and facts["headquarters"]["value"].startswith("Hafenstraße 12")
    assert len(web.requests) == before


def test_an_unreachable_website_does_not_block_the_workflow(researched) -> None:
    client, _web = researched
    opportunity_id = create(client, "Restricted AG", "https://restricted.test")
    approved = approve(client, opportunity_id)
    paper = approved["paper_json"]
    assert paper["generation"]["research_mode"] == "user_context_only"
    assert paper["generation"]["website_research"]["errors"][0]["code"] == "WEBSITE_ACCESS_RESTRICTED"
    assert "could not be analysed" in paper["analysis"]["research"]["web_research"]["note"]
    assert generate_deck(client, opportunity_id)["status"] == "ready"


def test_the_research_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings.WEBSITE_RESEARCH_ENABLED is False, "unit tests never read real websites"
    assert stage1.get_company_research_provider() is None
    monkeypatch.setattr(settings, "WEBSITE_RESEARCH_ENABLED", True)
    assert isinstance(stage1.get_company_research_provider(), WebsiteResearchProvider)
