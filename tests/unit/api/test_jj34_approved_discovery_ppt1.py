"""JJ-34: PPT #1 is planned and written from the latest approved Discovery."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.auth import create_test_access_token
from app.config import settings
from app.main import create_app
from app.services.data.memory_store import get_memory_store, reset_memory_store
from app.services.presentation_generation import enqueue_first_contact_presentation_generate

OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + create_test_access_token(
            user_id=OWNER,
            email="sales@example.com",
            secret=settings.SUPABASE_JWT_SECRET,
        )
    }


def create_opportunity(client: TestClient, client_name: str = "Northwind") -> str:
    response = client.post(
        "/opportunities",
        headers=headers(),
        json={
            "client_name": client_name,
            "opportunity_name": "Warehouse review",
            "department": "Operations",
            "stage1_intake": {
                "sales_topic_description": "Warehouse slotting review",
                "about_company": "Family-owned distributor in Hamburg.",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload_document(client: TestClient, opportunity_id: str) -> None:
    upload = client.post(
        f"/opportunities/{opportunity_id}/client-documents",
        headers=headers(),
        files={"file": ("brief.txt", b"Client background material.", "text/plain")},
    )
    assert upload.status_code == 201, upload.text


def approve_discovery(client: TestClient, opportunity_id: str) -> dict:
    generated = client.post(
        f"/opportunities/{opportunity_id}/discovery-paper/generate",
        headers=headers(),
    )
    assert generated.status_code == 200, generated.text
    approved = client.post(
        f"/opportunities/{opportunity_id}/discovery-paper/approve",
        headers=headers(),
    )
    assert approved.status_code == 200, approved.text
    return approved.json()


def edit_cover(client: TestClient, opportunity_id: str, client_name: str) -> None:
    paper = client.get(
        f"/opportunities/{opportunity_id}/discovery-paper",
        headers=headers(),
    ).json()
    assert paper["schema_version"] == "2.0"
    edited = client.patch(
        f"/opportunities/{opportunity_id}/discovery-paper",
        headers=headers(),
        json={"edits": [{"target": "thesis", "value": {"text": f"Working hypothesis: {client_name}"}}]},
    )
    assert edited.status_code == 200, edited.text


def latest_version(store, presentation_id):
    rows = [
        row
        for row in store.presentation_versions.values()
        if row["presentation_id"] == presentation_id
    ]
    return max(rows, key=lambda row: row["version_number"])


def test_draft_only_discovery_is_rejected_before_side_effects() -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        generated = client.post(
            f"/opportunities/{opportunity_id}/discovery-paper/generate",
            headers=headers(),
        )
        assert generated.status_code == 200, generated.text
        store = get_memory_store()
        before = (
            len(store.presentation_plans),
            len(store.presentations),
            len(store.generation_jobs),
            len(store.presentation_versions),
        )
        rejected = client.post(
            f"/opportunities/{opportunity_id}/stage1-outputs/generate",
            headers=headers(),
        )
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "DISCOVERY_PAPER_APPROVAL_REQUIRED"
        assert (
            len(store.presentation_plans),
            len(store.presentations),
            len(store.generation_jobs),
            len(store.presentation_versions),
        ) == before


def test_approved_v1_grounds_plan_and_slide_content() -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approved = approve_discovery(client, opportunity_id)
        generated = client.post(
            f"/opportunities/{opportunity_id}/stage1-outputs/generate",
            headers=headers(),
        )
        assert generated.status_code == 200, generated.text
        presentation = generated.json()["outputs"]["presentation"]
        assert presentation["status"] == "ready"
        store = get_memory_store()
        presentation_id = UUID(presentation["presentation_id"])
        version = latest_version(store, presentation_id)
        assert version["status"] == "ready"
        assert version["journey_stage"] == "first_contact"
        assert version["pptx_storage_path"]
        assert version["pdf_storage_path"]
        assert version["preview_image_paths"]
        manifest = version["generation_source_manifest"]
        # An approved Discovery analysis (schema 2.0) is the source of the Master Presentation.
        assert manifest["kind"] == "master_presentation_v1"
        assert manifest["approved_discovery_version_id"] == approved["id"]
        assert manifest["discovery_schema_version"] == "2.0"
        assert "client_name" not in json.dumps(manifest)
        assert "pages" not in manifest
        plan = next(iter(store.presentation_plans.values()))["plan_json"]
        assert len(plan["slides"]) > 26
        assert plan["slides"][0]["frameworkReferences"] == []
        specs = version["slides_json"]
        assert specs[0]["layoutId"] == "CANONICAL"
        assert specs[25]["sourceChapterIds"]
        assert plan["engine"] == "borek_deck"
        assert plan["deck_kind"] == "master_v1"
        assert specs[-1]["title"] == "Let’s talk" and specs[-1]["layoutId"] == "CANONICAL"
        assert specs[-2]["layoutId"].startswith("L")
        assert "Northwind" in json.dumps(specs)
        framework = next(iter(store.framework_versions.values()))
        framework_blob = json.dumps(framework["framework_json"])
        assert approved["id"] not in framework_blob
        assert "approved_discovery" not in framework_blob
        pptx = client.get(
            f"/presentations/{presentation['presentation_id']}/download/pptx",
            headers=headers(),
        )
        pdf = client.get(
            f"/presentations/{presentation['presentation_id']}/download/pdf",
            headers=headers(),
        )
        preview = client.get(
            f"/presentations/{presentation['presentation_id']}/preview/slides/0.png",
            headers=headers(),
        )
        assert pptx.status_code == 200, pptx.text
        assert pdf.status_code == 200, pdf.text
        assert preview.status_code == 200, preview.text


def test_draft_after_approval_does_not_change_the_planner_source() -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approved = approve_discovery(client, opportunity_id)
        edit_cover(client, opportunity_id, "Edited after approval")
        generated = client.post(
            f"/opportunities/{opportunity_id}/stage1-outputs/generate",
            headers=headers(),
        )
        assert generated.status_code == 200, generated.text
        store = get_memory_store()
        presentation_id = UUID(generated.json()["outputs"]["presentation"]["presentation_id"])
        specs = latest_version(store, presentation_id)["slides_json"]
        assert "Northwind" in json.dumps(specs)
        assert "Edited after approval" not in json.dumps(specs)
        manifest = latest_version(store, presentation_id)["generation_source_manifest"]
        assert manifest["approved_discovery_version_id"] == approved["id"]


def test_explicit_regenerate_uses_v2_and_keeps_the_stage1_presentation() -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approve_discovery(client, opportunity_id)
        first = client.post(
            f"/opportunities/{opportunity_id}/stage1-outputs/generate",
            headers=headers(),
        )
        assert first.status_code == 200, first.text
        presentation_id = first.json()["outputs"]["presentation"]["presentation_id"]
        edit_cover(client, opportunity_id, "Second Approved Client")
        second = client.post(
            f"/opportunities/{opportunity_id}/discovery-paper/approve",
            headers=headers(),
        )
        assert second.status_code == 200, second.text
        regenerated = client.post(
            f"/opportunities/{opportunity_id}/stage1-outputs/presentation/regenerate",
            headers=headers(),
        )
        assert regenerated.status_code == 200, regenerated.text
        assert regenerated.json()["outputs"]["presentation"]["presentation_id"] == presentation_id
        assert regenerated.json()["outputs"]["presentation"]["status"] == "ready"
        store = get_memory_store()
        assert len(store.presentations) == 1
        version = latest_version(store, UUID(presentation_id))
        assert version["version_number"] == 2
        assert "Second Approved Client" in str(version["slides_json"])
        assert version["generation_source_manifest"]["approved_discovery_version_id"] == second.json()["id"]
        edit_cover(client, opportunity_id, "Draft Only Client")
        again = client.post(
            f"/opportunities/{opportunity_id}/stage1-outputs/presentation/regenerate",
            headers=headers(),
        )
        assert again.status_code == 200, again.text
        # Same master and same approved version: the ready deck is returned, not generated again.
        latest = latest_version(store, UUID(presentation_id))
        assert latest["version_number"] == 2
        assert "Second Approved Client" in str(latest["slides_json"])
        assert "Draft Only Client" not in json.dumps(latest["slides_json"])
        assert again.json()["outputs"]["presentation"]["presentation_id"] == presentation_id


def test_retry_of_the_same_job_keeps_the_original_approved_version(monkeypatch) -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approved = approve_discovery(client, opportunity_id)
        from app.services import presentation_generation

        original = presentation_generation.execute_presentation_generation
        calls = {"count": 0}

        def fail_once(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("renderer unavailable")
            return original(*args, **kwargs)

        monkeypatch.setattr(
            presentation_generation,
            "execute_presentation_generation",
            fail_once,
        )
        failed = client.post(
            f"/opportunities/{opportunity_id}/stage1-outputs/generate",
            headers=headers(),
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["outputs"]["presentation"]["status"] == "failed"
        presentation_id = failed.json()["outputs"]["presentation"]["presentation_id"]
        edit_cover(client, opportunity_id, "Later Approved Client")
        moved = client.post(
            f"/opportunities/{opportunity_id}/discovery-paper/approve",
            headers=headers(),
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["id"] != approved["id"]
        store = get_memory_store()

        def forbid_latest(*_args, **_kwargs):
            raise AssertionError("retry must not resolve the latest approved Discovery")

        monkeypatch.setattr(store, "get_latest_approved_discovery_paper", forbid_latest)
        job = next(iter(store.generation_jobs.values()))
        retried = client.post(f"/jobs/{job['id']}/retry", headers=headers())
        assert retried.status_code == 202, retried.text
        version = latest_version(store, UUID(presentation_id))
        assert "Northwind" in str(version["slides_json"])
        assert "Later Approved Client" not in json.dumps(version["slides_json"])
        assert (
            version["generation_source_manifest"]["approved_discovery_version_id"]
            == approved["id"]
        )
        assert store.opportunities[UUID(opportunity_id)]["stage1_outputs"]["outputs"][
            "presentation"
        ]["presentation_id"] == presentation_id


def test_generic_first_contact_deck_is_not_ppt1() -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client, client_name="Acme Corp")
        client.post(f"/opportunities/{opportunity_id}/framework/generate", headers=headers())
        client.post(
            f"/opportunities/{opportunity_id}/framework/confirm",
            headers=headers(),
            json={},
        )
        plan = client.post(
            f"/opportunities/{opportunity_id}/presentation-plan/generate",
            headers=headers(),
            json={},
        )
        assert plan.status_code == 202, plan.text
        generic = client.post(
            f"/opportunities/{opportunity_id}/presentation/generate",
            headers=headers(),
            json={"presentation_plan_id": plan.json()["presentation_plan_id"]},
        )
        assert generic.status_code == 202, generic.text
        generic_id = generic.json()["presentation_id"]
        upload_document(client, opportunity_id)
        approve_discovery(client, opportunity_id)
        stage1 = client.post(
            f"/opportunities/{opportunity_id}/stage1-outputs/generate",
            headers=headers(),
        )
        assert stage1.status_code == 200, stage1.text
        ppt1_id = stage1.json()["outputs"]["presentation"]["presentation_id"]
        assert ppt1_id != generic_id
        store = get_memory_store()
        generic_versions = [
            row
            for row in store.presentation_versions.values()
            if str(row["presentation_id"]) == generic_id
        ]
        assert len(generic_versions) == 1
        assert len(store.presentations) == 2


def test_direct_enqueue_without_approval_creates_nothing() -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        store = get_memory_store()
        before = len(store.presentation_plans)
        with pytest.raises(Exception) as raised:
            enqueue_first_contact_presentation_generate(
                store,
                opportunity_id=UUID(opportunity_id),
                user_id=OWNER,
            )
        assert getattr(raised.value, "status_code", None) == 400
        assert len(store.presentation_plans) == before


def test_single_slide_edits_are_not_offered_for_the_borek_deck() -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approve_discovery(client, opportunity_id)
        generated = client.post(
            f"/opportunities/{opportunity_id}/stage1-outputs/generate",
            headers=headers(),
        )
        assert generated.status_code == 200, generated.text
        presentation_id = generated.json()["outputs"]["presentation"]["presentation_id"]
        slides = client.get(f"/presentations/{presentation_id}/slides", headers=headers())
        assert slides.status_code == 200, slides.text
        slide = slides.json()[0]
        regenerate = client.post(
            f"/presentations/{presentation_id}/slides/{slide['id']}/regenerate",
            headers=headers(),
        )
        assert regenerate.status_code == 400
        assert regenerate.json()["error"]["code"] == "BOREK_DECK_SLIDE_EDIT_UNSUPPORTED"
        relayout = client.post(
            f"/presentations/{presentation_id}/slides/{slide['id']}/change-layout",
            headers=headers(),
            json={"layout_id": slide["layout_id"]},
        )
        assert relayout.status_code == 400
        assert relayout.json()["error"]["code"] == "BOREK_DECK_SLIDE_EDIT_UNSUPPORTED"
