"""Master Presentation V1 through the API: source, persistence, idempotency, artifacts, versioning."""

from __future__ import annotations

import copy
import io
import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pptx import Presentation

from app.auth import create_test_access_token
from app.config import settings
from app.main import create_app
from app.services import presentation_generation
from app.services.data.memory_store import get_memory_store, reset_memory_store
from services.presentation.master_deck import assembly, registry

OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CLIENT = {
    "client_name": "Nordwind Maschinenbau",
    "opportunity_name": "AI introduction",
    "department": "Sales",
    "stage1_intake": {
        "client_web_page": "https://nordwind.example",
        "poc_name": "Dana Weber",
        "sales_topic_description": "Reduce quote turnaround and invoice handling effort",
        "about_company": "Family-owned machine builder with 420 employees.",
    },
}


def headers(user: UUID = OWNER) -> dict[str, str]:
    token = create_test_access_token(user_id=user, email="sales@example.com", secret=settings.SUPABASE_JWT_SECRET)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client() -> TestClient:
    reset_memory_store()
    return TestClient(create_app())


def create(client: TestClient, body: dict = CLIENT) -> str:
    response = client.post("/opportunities", headers=headers(), json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def approve(client: TestClient, opportunity_id: str, *, generate: bool = True) -> dict:
    if generate:
        generated = client.post(f"/opportunities/{opportunity_id}/discovery-paper/generate", headers=headers())
        assert generated.status_code == 200, generated.text
    approved = client.post(f"/opportunities/{opportunity_id}/discovery-paper/approve", headers=headers())
    assert approved.status_code == 200, approved.text
    return approved.json()


def generate_deck(client: TestClient, opportunity_id: str, *, regenerate: bool = False) -> dict:
    path = "stage1-outputs/presentation/regenerate" if regenerate else "stage1-outputs/generate"
    response = client.post(f"/opportunities/{opportunity_id}/{path}", headers=headers())
    assert response.status_code == 200, response.text
    return response.json()["outputs"]["presentation"]


def versions_of(presentation_id: str) -> list[dict]:
    rows = [row for row in get_memory_store().presentation_versions.values() if str(row["presentation_id"]) == presentation_id]
    return sorted(rows, key=lambda row: row["version_number"])


def edit_thesis(client: TestClient, opportunity_id: str, text: str) -> None:
    response = client.patch(
        f"/opportunities/{opportunity_id}/discovery-paper",
        headers=headers(),
        json={"edits": [{"target": "thesis", "value": {"text": f"Working hypothesis: {text}"}}]},
    )
    assert response.status_code == 200, response.text


def test_v1_is_the_canonical_deck_plus_an_appendix_from_the_approved_analysis(client: TestClient) -> None:
    opportunity_id = create(client)
    approved = approve(client, opportunity_id)
    deck = generate_deck(client, opportunity_id)
    assert deck["status"] == "ready"
    presentation_id = deck["presentation_id"]
    (version,) = versions_of(presentation_id)
    master = registry.get_master()

    assert version["status"] == "ready" and version["journey_stage"] == "first_contact"
    assert version["generation_source_manifest"] == {
        "schema_version": "1.0",
        "kind": "master_presentation_v1",
        "product_version": "V1",
        "product_stage": "pre_meeting",
        "master_id": "borek_ai_tech_en_v1",
        "master_version": "1.0",
        "master_sha256": master.sha256,
        "master_slide_count": 26,
        "approved_discovery_version_id": approved["id"],
        "approved_discovery_document_id": approved["document_id"],
        "discovery_schema_version": "2.0",
        "appendix_source_hash": version["generation_source_manifest"]["appendix_source_hash"],
        "deck_layout": "closing-last:v1",
    }
    specs = version["slides_json"]
    assert len(specs) > 26 and len(specs) > 8
    canonical_specs = [*specs[:25], specs[-1]]
    assert [spec["layoutId"] for spec in canonical_specs] == ["CANONICAL"] * 26
    assert [spec["title"] for spec in canonical_specs] == list(registry.canonical_slide_titles())
    assert specs[-1]["title"] == "Let’s talk"
    assert all(spec["layoutId"].startswith("L") and spec["sourceChapterIds"] for spec in specs[25:-1])
    assert len(version["preview_image_paths"]) == len(specs)
    assert version["pptx_storage_path"] and version["pdf_storage_path"]

    # Real artifacts, served through the authenticated download endpoints.
    pptx = client.get(f"/presentations/{presentation_id}/download/pptx", headers=headers())
    pdf = client.get(f"/presentations/{presentation_id}/download/pdf", headers=headers())
    assert pptx.status_code == 200 and pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    downloaded = Presentation(io.BytesIO(pptx.content))
    assert len(downloaded.slides) == len(specs)
    target = io.BytesIO(pptx.content)
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "deck.pptx"
        path.write_bytes(target.getvalue())
        prints = assembly.canonical_fingerprints(path)
        assert [*prints[:25], prints[-1]] == assembly.canonical_fingerprints(master.pptx_path)
    center = client.get(f"/presentations/{presentation_id}/deck", headers=headers()).json()
    assert center["source"] == {
        "kind": "master_presentation_v1",
        "product_version": "V1",
        "product_stage": "pre_meeting",
        "revision": 1,
        "master_id": "borek_ai_tech_en_v1",
        "master_version": "1.0",
        "canonical_slide_count": 26,
        "appendix_slide_count": len(specs) - 26,
        "approved_discovery_version_id": approved["id"],
        "discovery_schema_version": "2.0",
    }
    assert [slide["slide_index"] for slide in center["slides"]] == list(range(len(specs)))
    first = client.get(f"/presentations/{presentation_id}/preview/slides/0.png", headers=headers())
    last = client.get(f"/presentations/{presentation_id}/preview/slides/{len(specs) - 1}.png", headers=headers())
    beyond = client.get(f"/presentations/{presentation_id}/preview/slides/{len(specs)}.png", headers=headers())
    assert first.status_code == 200 and last.status_code == 200 and beyond.status_code == 404
    assert first.content.startswith(bytes([0x89]) + b"PNG") and first.content != last.content
    assert client.get(f"/presentations/{presentation_id}/download/pptx").status_code == 401
    assert client.get(f"/presentations/{presentation_id}/download/pptx", headers=headers(OTHER)).status_code in {403, 404}
    assert "deck_assets" not in json.dumps(deck) and "pptx_storage_path" not in json.dumps(deck)


def test_generation_requires_an_approved_discovery_and_ignores_drafts_and_notes(client: TestClient) -> None:
    opportunity_id = create(client)
    generated = client.post(f"/opportunities/{opportunity_id}/discovery-paper/generate", headers=headers())
    assert generated.status_code == 200
    blocked = client.post(f"/opportunities/{opportunity_id}/stage1-outputs/generate", headers=headers())
    assert blocked.status_code == 400
    assert blocked.json()["error"]["code"] == "DISCOVERY_PAPER_APPROVAL_REQUIRED"
    assert get_memory_store().presentations == {}

    approved = approve(client, opportunity_id, generate=False)
    notes = client.put(
        f"/opportunities/{opportunity_id}/personal-notes", headers=headers(), json={"text": "NOTE-ONLY-FACT 777"}
    )
    assert notes.status_code in {200, 201}, notes.text
    edit_thesis(client, opportunity_id, "DRAFT-ONLY-THESIS")
    deck = generate_deck(client, opportunity_id)
    (version,) = versions_of(deck["presentation_id"])
    rendered = json.dumps(version["slides_json"], ensure_ascii=False)
    assert "DRAFT-ONLY-THESIS" not in rendered and "NOTE-ONLY-FACT" not in rendered
    assert "Reduce quote turnaround and invoice handling effort" in rendered
    assert version["generation_source_manifest"]["approved_discovery_version_id"] == approved["id"]
    plan = next(iter(get_memory_store().presentation_plans.values()))["plan_json"]
    assert "transcript" not in json.dumps(plan).lower() and "ppt2" not in json.dumps(version["generation_source_manifest"])


def test_same_source_is_idempotent_and_a_new_approval_adds_a_version_to_the_same_presentation(client: TestClient) -> None:
    opportunity_id = create(client)
    first_approval = approve(client, opportunity_id)
    deck = generate_deck(client, opportunity_id)
    presentation_id = deck["presentation_id"]
    (first,) = versions_of(presentation_id)
    snapshot = copy.deepcopy(first)

    # Same opportunity, master and approved version: the ready V1 is returned, nothing is generated.
    for regenerate in (False, True, True):
        again = generate_deck(client, opportunity_id, regenerate=regenerate)
        assert (again["presentation_id"], again["status"]) == (presentation_id, "ready")
    presentation, _plan, job, reused = presentation_generation.enqueue_first_contact_presentation_generate(
        get_memory_store(), opportunity_id=UUID(opportunity_id), user_id=OWNER
    )
    assert (str(presentation["id"]), job, reused) == (presentation_id, None, True)
    assert versions_of(presentation_id) == [snapshot]
    assert len(get_memory_store().presentations) == 1

    # A draft edit alone changes nothing; a new approval is a new source and a new version.
    edit_thesis(client, opportunity_id, "the second approved thesis")
    assert generate_deck(client, opportunity_id, regenerate=True)["presentation_id"] == presentation_id
    assert len(versions_of(presentation_id)) == 1
    second_approval = approve(client, opportunity_id, generate=False)
    assert second_approval["id"] != first_approval["id"]
    regenerated = generate_deck(client, opportunity_id, regenerate=True)
    assert (regenerated["presentation_id"], regenerated["status"]) == (presentation_id, "ready")
    old, new = versions_of(presentation_id)
    assert old == snapshot, "a ready version is never changed"
    assert (new["version_number"], new["journey_stage"]) == (2, "first_contact")
    # A second approved Discovery gives a new pre-meeting revision. It is still Master
    # Presentation V1 - the post-meeting stage (V2) is something else entirely.
    assert (new["generation_source_manifest"]["product_version"], new["generation_source_manifest"]["product_stage"]) == ("V1", "pre_meeting")
    assert new["generation_source_manifest"]["kind"] == "master_presentation_v1"
    center = client.get(f"/presentations/{presentation_id}/deck", headers=headers()).json()
    assert (center["source"]["product_version"], center["source"]["product_stage"], center["source"]["revision"]) == ("V1", "pre_meeting", 2)
    assert center["version_number"] == 2
    assert new["generation_source_manifest"]["approved_discovery_version_id"] == second_approval["id"]
    assert new["generation_source_manifest"]["appendix_source_hash"] != old["generation_source_manifest"]["appendix_source_hash"]
    assert "the second approved thesis" in json.dumps(new["slides_json"]).lower()
    assert "the second approved thesis" not in json.dumps(old["slides_json"]).lower()
    assert len(get_memory_store().presentations) == 1


def test_failed_generation_is_retried_on_the_same_presentation_with_the_frozen_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    opportunity_id = create(client)
    approved = approve(client, opportunity_id)
    original = presentation_generation.render_presentation_version
    calls = {"count": 0}

    def fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("renderer unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(presentation_generation, "render_presentation_version", fail_once)
    failed = generate_deck(client, opportunity_id)
    assert failed["status"] == "failed"
    presentation_id = failed["presentation_id"]
    assert not [row for row in versions_of(presentation_id) if row["status"] == "ready"], "no ready version without artifacts"

    # The source moves on, but the retry of this job keeps the source it was started with.
    edit_thesis(client, opportunity_id, "a later approved thesis")
    approve(client, opportunity_id, generate=False)
    store = get_memory_store()
    job = next(iter(store.generation_jobs.values()))
    retried = client.post(f"/jobs/{job['id']}/retry", headers=headers())
    assert retried.status_code == 202, retried.text
    ready = [row for row in versions_of(presentation_id) if row["status"] == "ready"]
    assert len(ready) == 1 and len(store.presentations) == 1
    assert ready[0]["generation_source_manifest"]["approved_discovery_version_id"] == approved["id"]
    assert "a later approved thesis" not in json.dumps(ready[0]["slides_json"]).lower()
    assert len(ready[0]["preview_image_paths"]) == len(ready[0]["slides_json"])


def test_a_new_request_after_a_failure_keeps_the_presentation_identity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    opportunity_id = create(client)
    approve(client, opportunity_id)
    from app.services import renderer_client

    original = renderer_client.render_borek_deck_assets
    calls = {"count": 0}

    def fail_once(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise renderer_client.RendererClientError("MASTER_PRESENTATION_RENDER_FAILED", "render failed", retryable=False)
        return original(**kwargs)

    monkeypatch.setattr(presentation_generation, "render_borek_deck_assets", fail_once)
    failed = generate_deck(client, opportunity_id)
    assert failed["status"] == "failed"
    again = generate_deck(client, opportunity_id)
    assert (again["status"], again["presentation_id"]) == ("ready", failed["presentation_id"])
    assert len(get_memory_store().presentations) == 1


def test_a_damaged_master_blocks_generation_explicitly(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import shutil

    master = registry.get_master()
    shutil.copytree(master.directory, tmp_path / master.master_id)
    (tmp_path / master.master_id / "deck.pptx").write_bytes(b"not the registered deck")
    monkeypatch.setattr(registry, "ASSETS", tmp_path)
    opportunity_id = create(client)
    approve(client, opportunity_id)
    with pytest.raises(Exception) as failure:
        presentation_generation.enqueue_first_contact_presentation_generate(
            get_memory_store(), opportunity_id=UUID(opportunity_id), user_id=OWNER
        )
    assert failure.value.detail["code"] == "MASTER_DECK_INVALID"
    assert get_memory_store().presentations == {}
    deck = generate_deck(client, opportunity_id)
    assert (deck["status"], deck["code"], deck["presentation_id"]) == ("failed", "MASTER_DECK_INVALID", None)


def test_a_future_v2_is_a_second_version_of_the_same_presentation(client: TestClient) -> None:
    """V2 is not implemented here; the model must already allow it without a second identity."""
    opportunity_id = create(client)
    approve(client, opportunity_id)
    deck = generate_deck(client, opportunity_id)
    presentation_id = UUID(deck["presentation_id"])
    store = get_memory_store()
    (first,) = versions_of(deck["presentation_id"])
    plan = next(iter(store.presentation_plans.values()))["plan_json"]
    second = store.create_presentation_version_with_slides(
        presentation_id=presentation_id,
        user_id=OWNER,
        plan_json=plan,
        journey_stage="post_meeting",
        prior_stage_presentation_version_id=first["id"],
        discovery_pages=None,
        generation_source_manifest={**first["generation_source_manifest"], "kind": "master_presentation_v2"},
        ppt2_generation_input=None,
    )
    v1, v2 = versions_of(deck["presentation_id"])
    assert (v1["id"], v1["journey_stage"], v1["version_number"]) == (first["id"], "first_contact", 1)
    assert (v2["id"], v2["journey_stage"], v2["version_number"]) == (second["id"], "post_meeting", 2)
    assert v2["presentation_id"] == v1["presentation_id"] == presentation_id
    assert v2["prior_stage_presentation_version_id"] == v1["id"]
    assert len(store.presentations) == 1
    assert v1["status"] == "ready" and v1["slides_json"] == first["slides_json"]


def test_earlier_ready_versions_stay_readable_after_a_new_revision(client: TestClient) -> None:
    opportunity_id = create(client)
    approve(client, opportunity_id)
    presentation_id = generate_deck(client, opportunity_id)["presentation_id"]
    base = f"/presentations/{presentation_id}"
    get = lambda path, user=OWNER: client.get(base + path, headers=headers(user))  # noqa: E731
    latest_before = get("/deck").json()
    first_files = {kind: get(f"/download/{kind}").content for kind in ("pptx", "pdf")}
    first_preview = get("/preview/slides/26.png").content

    edit_thesis(client, opportunity_id, "the second approved thesis")
    approve(client, opportunity_id, generate=False)
    assert generate_deck(client, opportunity_id, regenerate=True)["presentation_id"] == presentation_id
    old, new = versions_of(presentation_id)
    snapshot = copy.deepcopy(old)

    listed = get("/versions")
    assert listed.status_code == 200, listed.text
    newest, oldest = listed.json()
    assert [(item["presentation_version_id"], item["version_number"], item["is_latest"]) for item in (newest, oldest)] == [
        (str(new["id"]), 2, True),
        (str(old["id"]), 1, False),
    ]
    assert (oldest["source"]["product_version"], oldest["source"]["revision"]) == ("V1", 1)
    assert oldest["source"]["approved_discovery_version_id"] != newest["source"]["approved_discovery_version_id"]
    assert "storage_path" not in listed.text and "slides_json" not in listed.text

    # The first version is read through its own URLs and is exactly what it was.
    prefix = f"/versions/{old['id']}"
    assert oldest["deck_url"] == f"{base}{prefix}/deck"
    first = get(f"{prefix}/deck")
    assert first.status_code == 200, first.text
    deck = first.json()
    assert (deck["presentation_id"], deck["presentation_version_id"], deck["version_number"]) == (presentation_id, str(old["id"]), 1)
    assert deck["source"]["revision"] == 1
    assert [slide["slide_id"] for slide in deck["slides"]] == [slide["slide_id"] for slide in latest_before["slides"]]
    assert all(slide["preview_url"] == f"{base}{prefix}/preview/slides/{slide['slide_index']}.png" for slide in deck["slides"])
    assert (deck["pptx_download_url"], deck["pdf_download_url"]) == (oldest["pptx_download_url"], oldest["pdf_download_url"])
    for kind in ("pptx", "pdf"):
        response = client.get(deck[f"{kind}_download_url"], headers=headers())
        assert response.status_code == 200 and response.content == first_files[kind]
        assert str(old["id"]) in response.headers["content-disposition"]
    assert client.get(deck["slides"][26]["preview_url"], headers=headers()).content == first_preview

    # The existing endpoints still serve the latest version, with their payload unchanged.
    latest = get("/deck").json()
    assert latest["version_number"] == 2 and set(latest) == set(latest_before)
    assert all("/versions/" not in slide["preview_url"] for slide in latest["slides"])
    second_pptx = get("/download/pptx").content
    assert second_pptx != first_files["pptx"]
    assert get(f"/versions/{new['id']}/download/pptx").content == second_pptx
    assert versions_of(presentation_id)[0] == snapshot, "reading a version never changes it"
    assert len(get_memory_store().presentations) == 1


def test_version_access_enforces_authentication_ownership_and_identity(client: TestClient) -> None:
    opportunity_id = create(client)
    approve(client, opportunity_id)
    presentation_id = generate_deck(client, opportunity_id)["presentation_id"]
    (version,) = versions_of(presentation_id)
    other_opportunity = create(client, {**CLIENT, "client_name": "Südhafen Logistik"})
    approve(client, other_opportunity)
    other_presentation = generate_deck(client, other_opportunity)["presentation_id"]
    (other_version,) = versions_of(other_presentation)
    assert other_presentation != presentation_id

    paths = ["/versions"] + [
        f"/versions/{version['id']}/{tail}" for tail in ("deck", "preview/slides/0.png", "download/pptx", "download/pdf")
    ]
    for path in paths:
        url = f"/presentations/{presentation_id}{path}"
        assert client.get(url, headers=headers()).status_code == 200, path
        assert client.get(url).status_code == 401, path
        # Another user gets exactly what the latest-version routes give them: nothing.
        foreign = client.get(url, headers=headers(OTHER))
        assert foreign.status_code == client.get(f"/presentations/{presentation_id}/deck", headers=headers(OTHER)).status_code
        assert foreign.status_code in (403, 404), path

    # A version is only reachable through the presentation it belongs to.
    for wrong in (other_version["id"], "cccccccc-cccc-4ccc-8ccc-cccccccccccc"):
        for tail in ("deck", "preview/slides/0.png", "download/pptx", "download/pdf"):
            response = client.get(f"/presentations/{presentation_id}/versions/{wrong}/{tail}", headers=headers())
            assert response.status_code == 404 and "PRESENTATION_VERSION_NOT_FOUND" in response.text, response.text
    assert client.get(f"/presentations/{presentation_id}/versions/not-a-uuid/deck", headers=headers()).status_code == 422

    # Only ready versions are listed or served.
    get_memory_store().presentation_versions[version["id"]]["status"] = "failed"
    assert client.get(f"/presentations/{presentation_id}/versions", headers=headers()).json() == []
    for tail in ("deck", "download/pptx"):
        response = client.get(f"/presentations/{presentation_id}/versions/{version['id']}/{tail}", headers=headers())
        assert response.status_code in (400, 409) and "PRESENTATION_NOT_READY" in response.text
