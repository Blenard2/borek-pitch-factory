"""BAP-2: what the Client Presentation heading reads - the generated title and the main opportunity
of the approved Discovery - is there for each client, and only for that client."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.unit.api.test_master_presentation_v2_api import CLIENT, OTHER, OWNER, client, get, headers, ok, post  # noqa: F401  (client is a fixture)


def approved_discovery(client: TestClient, body: dict, user=OWNER) -> tuple[str, dict]:
    opportunity_id = ok(client.post("/opportunities", headers=headers(user), json=body), 201)["id"]
    ok(post(client, f"/opportunities/{opportunity_id}/discovery-paper/generate", user=user))
    approved = ok(post(client, f"/opportunities/{opportunity_id}/discovery-paper/approve", user=user))
    return opportunity_id, approved


def test_each_client_gets_its_own_generated_title_and_main_opportunity(client: TestClient) -> None:
    first, first_version = approved_discovery(client, CLIENT)
    second, second_version = approved_discovery(client, {**CLIENT, "client_name": "Südhafen Logistik"}, OTHER)

    for opportunity_id, version, name, user in ((first, first_version, CLIENT["client_name"], OWNER), (second, second_version, "Südhafen Logistik", OTHER)):
        latest = ok(get(client, f"/opportunities/{opportunity_id}/discovery-paper/approved", user))
        pinned = ok(get(client, f"/opportunities/{opportunity_id}/discovery-paper/versions/{version['id']}", user))
        assert latest["id"] == pinned["id"] == version["id"] and latest["opportunity_id"] == opportunity_id
        paper = pinned["paper_json"]
        assert paper["schema_version"] == "2.0" and paper["opportunity_id"] == opportunity_id
        title = paper["analysis"]["framing"]["document"]["title"]
        assert name in title, "the generated title names this client"
        main = paper["presentation_brief"]["priority_opportunities"][0]
        assert main["title"] and main["result"] and main["area"]
        assert len(title) <= 70, "the Discovery contract keeps the title short enough for the heading"

    # Another owner never reads the title of this client, neither the latest nor a pinned version.
    assert get(client, f"/opportunities/{first}/discovery-paper/approved", OTHER).status_code in (403, 404)
    assert get(client, f"/opportunities/{first}/discovery-paper/versions/{first_version['id']}", OTHER).status_code in (403, 404)
    # A version of one opportunity is not served under another one.
    assert get(client, f"/opportunities/{second}/discovery-paper/versions/{first_version['id']}", OTHER).status_code == 404


def test_without_an_approved_discovery_there_is_no_title_to_show(client: TestClient) -> None:
    opportunity_id = ok(client.post("/opportunities", headers=headers(), json=CLIENT), 201)["id"]
    missing = get(client, f"/opportunities/{opportunity_id}/discovery-paper/approved")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "DISCOVERY_PAPER_NOT_APPROVED"
    ok(post(client, f"/opportunities/{opportunity_id}/discovery-paper/generate"))
    assert get(client, f"/opportunities/{opportunity_id}/discovery-paper/approved").status_code == 404, "a draft is not shown as the title"
