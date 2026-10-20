"""BAP-10 through the API: a client with only a name and a website gets a company-specific
Discovery document with its sources, which can be approved and feeds Master Presentation V1."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import journey_generation, stage1
from app.services.data.memory_store import reset_memory_store
from services.framework.discovery_analysis.fixture import load_library
from services.research.website.provider import WebsiteResearchProvider
from tests.fixtures.website_fixtures import company_web
from tests.unit.api.test_master_presentation_api import approve, generate_deck, headers

LIBRARY = load_library()


def name_and_website(name: str, website: str) -> dict:
    return {"client_name": name, "opportunity_name": "AI introduction", "department": "Operations", "stage1_intake": {"client_web_page": website}}


@pytest.fixture()
def researched(monkeypatch: pytest.MonkeyPatch):
    reset_memory_store()
    provider = WebsiteResearchProvider(fetcher_factory=company_web().fetcher)
    app = create_app()
    app.dependency_overrides[stage1.get_company_research_provider] = lambda: provider
    monkeypatch.setattr(journey_generation, "get_company_research_provider", lambda: provider)
    with TestClient(app) as client:
        yield client


def create(client: TestClient, name: str, website: str) -> str:
    response = client.post("/opportunities", headers=headers(), json=name_and_website(name, website))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_name_and_website_give_a_specific_document_with_sources_that_can_be_approved(researched) -> None:
    client = researched
    documents = {}
    for name, website in (("Hansa Präzisionsteile", "https://www.hansa-praezision.test"), ("Nordlicht Logistik", "https://nordlicht-logistik.test")):
        opportunity_id = create(client, name, website)
        paper = approve(client, opportunity_id)["paper_json"]
        documents[name] = paper
        background = [page for page in paper["page_manifest"] if page["id"].startswith("company-background-")]
        assert background, "the stored, approved document prints the company background"
        rows = [row for page in background for row in page["content"]["rows"]]
        assert all(row["source_url"].startswith(website.rstrip("/")) for row in rows)
        printed = json.dumps(paper["page_manifest"], ensure_ascii=False)
        for phrase in (LIBRARY["chapters"]["overview"]["profile_unknown"], LIBRARY["in_short"]["scope_context"], LIBRARY["research"]["web_research_note"]):
            assert phrase not in printed
        # The approved document reads back unchanged and still drives Master Presentation V1.
        stored = client.get(f"/opportunities/{opportunity_id}/discovery-paper/approved", headers=headers()).json()["paper_json"]
        assert stored["page_manifest"] == paper["page_manifest"]
        assert generate_deck(client, opportunity_id)["status"] == "ready"
    hansa, nordlicht = documents["Hansa Präzisionsteile"], documents["Nordlicht Logistik"]
    profile = lambda paper: paper["analysis"]["framing"]["chapters"]["overview"]["paragraphs"][0]  # noqa: E731
    assert profile(hansa) != profile(nordlicht) and "CNC" in profile(hansa) and "logistics" in profile(nordlicht)


def test_a_retry_never_changes_the_approved_document(researched) -> None:
    client = researched
    opportunity_id = create(client, "Hansa Präzisionsteile", "https://www.hansa-praezision.test")
    approved = approve(client, opportunity_id)
    retried = client.post(f"/opportunities/{opportunity_id}/discovery-paper/generate", headers=headers())
    assert retried.status_code == 200 and retried.json()["latest_approved_version_id"] == approved["id"]
    still = client.get(f"/opportunities/{opportunity_id}/discovery-paper/approved", headers=headers()).json()
    assert still["id"] == approved["id"] and still["paper_json"] == approved["paper_json"]


def test_an_inaccessible_website_is_reported_and_the_workflow_continues(researched) -> None:
    client = researched
    opportunity_id = create(client, "Restricted AG", "https://restricted.test")
    paper = approve(client, opportunity_id)["paper_json"]
    research = paper["generation"]["website_research"]
    assert research["status"] == "failed" and research["errors"][0]["code"] == "WEBSITE_ACCESS_RESTRICTED"
    assert not [page for page in paper["page_manifest"] if page["id"].startswith("company-background-")]
    assert generate_deck(client, opportunity_id)["status"] == "ready"
