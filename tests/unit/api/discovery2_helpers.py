"""BAP-7 test helper: generate and approve Discovery 2, the step between the confirmed meeting
findings and Master Presentation V2."""

from __future__ import annotations

from fastapi.testclient import TestClient


def generate_discovery2(client: TestClient, opportunity_id: str, headers: dict[str, str]) -> dict:
    response = client.post(f"/opportunities/{opportunity_id}/post-meeting-discovery/generate", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def approve_current_discovery2(client: TestClient, opportunity_id: str, headers: dict[str, str]) -> dict:
    current = client.get(f"/opportunities/{opportunity_id}/post-meeting-discovery", headers=headers).json()["current_version"]
    response = client.post(
        f"/opportunities/{opportunity_id}/post-meeting-discovery/approve",
        headers=headers,
        json={"expected_version_id": current["id"], "expected_content_hash": current["content_hash"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def approve_discovery2(client: TestClient, opportunity_id: str, headers: dict[str, str]) -> dict:
    """Generate Discovery 2 from the confirmed findings and approve it."""
    generate_discovery2(client, opportunity_id, headers)
    return approve_current_discovery2(client, opportunity_id, headers)


def ensure_discovery2_approved(client: TestClient, opportunity_id: str, headers: dict[str, str]) -> dict:
    """Approve Discovery 2 when the review needs it; the review as it is afterwards."""
    view = client.get(f"/opportunities/{opportunity_id}/post-meeting-review", headers=headers).json()
    blockers = view["readiness"]["blockers"]
    if blockers and set(blockers) <= {"DISCOVERY2_MISSING", "DISCOVERY2_NOT_APPROVED", "DISCOVERY2_STALE"}:
        if "DISCOVERY2_NOT_APPROVED" in blockers and view["discovery2"]["status"] == "draft":
            approve_current_discovery2(client, opportunity_id, headers)
        else:
            approve_discovery2(client, opportunity_id, headers)
        view = client.get(f"/opportunities/{opportunity_id}/post-meeting-review", headers=headers).json()
    return view
