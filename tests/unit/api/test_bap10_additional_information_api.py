"""BAP-10: a live user can add "Additional information" after a failed website analysis.

PUT /opportunities/{id}/additional-information saves only stage1_intake.about_company through the
existing store update: owner-only, audited, every other client field unchanged. The next
Discovery generation uses it; approved Discovery versions are never touched.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import journey_generation, stage1
from app.services.data.memory_store import get_memory_store, reset_memory_store
from services.framework.discovery_analysis.fixture import load_library
from services.research.website.provider import WebsiteResearchProvider
from tests.fixtures.website_fixtures import company_web
from tests.unit.api.test_master_presentation_v2_api import OTHER, headers

INTAKE = {
    "client_web_page": "https://restricted.test",
    "poc_name": "Dana Weber",
    "poc_position": "Head of Operations",
    "sales_topic_description": "Faster order handling",
    "about_company": None,
}
CONTEXT = "Family-owned distributor of industrial fasteners; quoting and invoice checks are done by hand by a team of six."


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    reset_memory_store()
    provider = WebsiteResearchProvider(fetcher_factory=company_web().fetcher)
    app = create_app()
    app.dependency_overrides[stage1.get_company_research_provider] = lambda: provider
    monkeypatch.setattr(journey_generation, "get_company_research_provider", lambda: provider)
    with TestClient(app) as test_client:
        yield test_client


def create(client: TestClient, name: str = "Schrauben Nord GmbH", intake: dict | None = None) -> str:
    body = {"client_name": name, "opportunity_name": "AI introduction", "department": "Operations", "stage1_intake": {**INTAKE, **(intake or {})}}
    response = client.post("/opportunities", headers=headers(), json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def save(client: TestClient, opportunity_id: str, value, user=None):
    return client.put(
        f"/opportunities/{opportunity_id}/additional-information",
        headers=headers(user) if user else headers(),
        json={"additional_information": value},
    )


def test_only_additional_information_changes(client: TestClient) -> None:
    opportunity_id = create(client)
    store = get_memory_store()
    store.opportunities[UUID(opportunity_id)]["voice_recording_artifact_id"] = "voice-1"
    before = client.get(f"/opportunities/{opportunity_id}", headers=headers()).json()
    response = save(client, opportunity_id, CONTEXT)
    assert response.status_code == 200, response.text
    after = client.get(f"/opportunities/{opportunity_id}", headers=headers()).json()
    assert after["stage1_intake"]["about_company"] == CONTEXT == response.json()["stage1_intake"]["about_company"]
    for key in ("client_web_page", "poc_name", "poc_position", "sales_topic_description"):
        assert after["stage1_intake"][key] == before["stage1_intake"][key], key
    for key in ("client_name", "opportunity_name", "department", "language", "status"):
        assert after[key] == before[key], key
    row = store.opportunities[UUID(opportunity_id)]
    assert row["voice_recording_artifact_id"] == "voice-1", "unlike a stage1_intake PATCH, nothing else is cleared"
    # Blank clears the field again.
    assert save(client, opportunity_id, "   ").json()["stage1_intake"]["about_company"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"additional_information": "x" * 20_001},
        {"additional_information": "contains \x00 NUL"},
        {"additional_information": 42},
        {"additional_information": "ok", "client_name": "Other"},
        {"about_company": "wrong key"},
    ],
    ids=["too-long", "nul", "not-text", "extra-field", "unknown-key"],
)
def test_invalid_updates_are_rejected_and_change_nothing(client: TestClient, body: dict) -> None:
    opportunity_id = create(client)
    response = client.put(f"/opportunities/{opportunity_id}/additional-information", headers=headers(), json=body)
    assert response.status_code == 422
    stored = client.get(f"/opportunities/{opportunity_id}", headers=headers()).json()
    assert stored["stage1_intake"]["about_company"] is None and stored["client_name"] == "Schrauben Nord GmbH"


def test_only_the_owner_can_update_and_clients_stay_isolated(client: TestClient) -> None:
    mine = create(client)
    other = create(client, "Nordlicht Logistik GmbH")
    path = f"/opportunities/{mine}/additional-information"
    assert client.put(path, json={"additional_information": CONTEXT}).status_code == 401, "no token"
    assert client.put(path, headers={"Authorization": "Bearer not-a-token"}, json={"additional_information": CONTEXT}).status_code == 401
    assert save(client, mine, CONTEXT, user=OTHER).status_code in (403, 404), "another user's client"
    assert save(client, str(uuid4()), CONTEXT).status_code == 404
    assert client.get(f"/opportunities/{mine}", headers=headers()).json()["stage1_intake"]["about_company"] is None
    assert save(client, mine, CONTEXT).status_code == 200
    assert client.get(f"/opportunities/{other}", headers=headers()).json()["stage1_intake"]["about_company"] is None, "the other client is unchanged"


def test_the_update_is_audited_and_persists_for_a_new_session(client: TestClient) -> None:
    opportunity_id = create(client)
    save(client, opportunity_id, CONTEXT)
    audit = [row for row in get_memory_store().audit_logs.values() if str(row["object_id"]) == opportunity_id]
    assert [str(row["action"]) for row in audit].count("opportunity.update") == 1
    # A new client and a new token for the same user (a re-login) read the saved value.
    with TestClient(create_app()) as again:
        assert again.get(f"/opportunities/{opportunity_id}", headers=headers()).json()["stage1_intake"]["about_company"] == CONTEXT


def test_recovery_after_a_failed_website_analysis_uses_the_saved_information(client: TestClient) -> None:
    library = load_library()
    opportunity_id = create(client)
    # 1. The website cannot be analysed: the document has no company profile.
    first = client.post(f"/opportunities/{opportunity_id}/discovery-paper/generate", headers=headers()).json()
    assert first["generation"]["website_research"]["status"] == "failed"
    assert first["analysis"]["framing"]["chapters"]["overview"]["paragraphs"][0] == library["chapters"]["overview"]["profile_unknown_named"].format(client="Schrauben Nord GmbH")
    approved = client.post(f"/opportunities/{opportunity_id}/discovery-paper/approve", headers=headers()).json()
    # 2. The user adds company information and saves it.
    assert save(client, opportunity_id, CONTEXT).status_code == 200
    # 3. Regenerate: the new document uses the saved context.
    second = client.post(f"/opportunities/{opportunity_id}/discovery-paper/generate", headers=headers()).json()
    assert second["status"] == "ready" and second["intake_context"]["additional_information"] == CONTEXT
    research = second["analysis"]["research"]
    assert research["company_profile"] == {"text": CONTEXT, "origin": "USER_INPUT"}
    assert {"label": "Additional information", "value": CONTEXT, "origin": "USER_INPUT", "source_refs": []} in research["known_facts"]
    overview = second["analysis"]["framing"]["chapters"]["overview"]["paragraphs"][0]
    assert overview == library["chapters"]["overview"]["profile_provided"].format(text=CONTEXT)
    priority = lambda paper: [area["id"] for area in paper["analysis"]["areas"] if area["priority"]]  # noqa: E731
    assert priority(first) == ["order_service"], "before: only the meeting purpose"
    assert priority(second) == ["sales_quoting", "order_service", "finance_controlling"], "the saved wording steers the analysis"
    # 4. The approved version is unchanged; the new document is a draft beside it.
    still = client.get(f"/opportunities/{opportunity_id}/discovery-paper/approved", headers=headers()).json()
    assert still["id"] == approved["id"] and still["paper_json"] == approved["paper_json"]
    assert second["latest_approved_version_id"] == approved["id"]


def test_the_supabase_store_sends_only_the_one_column_for_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production persistence: an owner-scoped read, then a PATCH of about_company alone."""
    import copy
    from datetime import UTC, datetime

    import httpx

    from app.services.data.supabase_store import SupabaseDataStore

    owner = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    VOICE = str(uuid4())  # noqa: N806
    row = {
        "id": str(uuid4()), "client_name": "Schrauben Nord GmbH", "opportunity_name": "AI", "department": "Operations",
        "language": "en", "status": "draft", "created_by": str(owner), "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(), **{key: value for key, value in INTAKE.items()}, "voice_recording_artifact_id": VOICE,
    }
    patches: list[dict] = []

    def request(self, method, table, *, json_body=None, params=None):
        assert table == "opportunities"
        if method == "PATCH":
            assert params == {"id": f"eq.{row['id']}"}
            patches.append(copy.deepcopy(json_body))
            row.update(json_body)
        return httpx.Response(200, json=[copy.deepcopy(row)])

    monkeypatch.setattr(SupabaseDataStore, "_request", request)
    store = SupabaseDataStore("test-token")
    updated = store.update_opportunity(opportunity_id=UUID(row["id"]), user_id=owner, updates={"about_company": CONTEXT})
    assert [set(patch) for patch in patches] == [{"about_company", "updated_at"}], "no other column is written"
    assert updated["stage1_intake"]["about_company"] == CONTEXT
    assert updated["stage1_intake"]["poc_name"] == "Dana Weber" and row["voice_recording_artifact_id"] == VOICE
