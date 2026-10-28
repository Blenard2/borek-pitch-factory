"""JJ-35: PPT #2 is generated from a frozen BT-46 context."""

from __future__ import annotations

import io
import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.auth import create_test_access_token
from app.config import settings
from app.main import create_app
from app.schemas.journey_stage import JOURNEY_STAGES
from app.services.data.memory_store import get_memory_store, reset_memory_store
OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NOTE_A = "Revision A owner observation"
NOTE_B = "Revision B after the job was frozen"
REQUIREMENT = "Keep slotting manual"
INVOICE_ID = "reference.invoice-3way.delivery-pattern"


def headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + create_test_access_token(
            user_id=OWNER,
            email="sales@example.com",
            secret=settings.SUPABASE_JWT_SECRET,
        )
    }


def create_opportunity(client: TestClient) -> str:
    response = client.post(
        "/opportunities",
        headers=headers(),
        json={
            "client_name": "Northwind",
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
    paper = client.get(f"/opportunities/{opportunity_id}/discovery-paper", headers=headers()).json()
    assert paper["schema_version"] == "2.0"
    edited = client.patch(
        f"/opportunities/{opportunity_id}/discovery-paper",
        headers=headers(),
        json={"edits": [{"target": "thesis", "value": {"text": f"Working hypothesis: {client_name}"}}]},
    )
    assert edited.status_code == 200, edited.text


def put_notes(client: TestClient, opportunity_id: str, text: str) -> dict:
    response = client.put(
        f"/opportunities/{opportunity_id}/personal-notes",
        headers=headers(),
        json={"text": text},
    )
    assert response.status_code == 200, response.text
    return response.json()


def upload_transcript(client: TestClient, opportunity_id: str, body: bytes) -> str:
    response = client.post(
        f"/opportunities/{opportunity_id}/transcripts",
        headers=headers(),
        files={"file": ("meeting.txt", io.BytesIO(body), "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()["transcript"]["id"]


def extract(client: TestClient, opportunity_id: str, transcript_id: str) -> dict:
    response = client.post(
        f"/opportunities/{opportunity_id}/meeting-extraction/generate",
        headers=headers(),
        json={"transcript_id": transcript_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def latest_version(store, presentation_id):
    rows = [
        row
        for row in store.presentation_versions.values()
        if row["presentation_id"] == presentation_id
    ]
    return max(rows, key=lambda row: row["version_number"])


def side_effect_counts(store) -> tuple:
    return (
        len(store.presentation_plans),
        len(store.presentations),
        len(store.generation_jobs),
        len(store.presentation_versions),
    )


def test_missing_approval_is_rejected_before_side_effects() -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        client.post(
            f"/opportunities/{opportunity_id}/discovery-paper/generate",
            headers=headers(),
        )
        store = get_memory_store()
        before = side_effect_counts(store)
        rejected = client.post(
            f"/opportunities/{opportunity_id}/ppt2/generate",
            headers=headers(),
        )
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "DISCOVERY_PAPER_APPROVAL_REQUIRED"
        assert side_effect_counts(store) == before


def test_approved_v1_is_used_while_a_draft_exists_and_sources_stay_separate(monkeypatch) -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approved = approve_discovery(client, opportunity_id)
        stage1 = client.post(
            f"/opportunities/{opportunity_id}/stage1-outputs/generate",
            headers=headers(),
        )
        assert stage1.status_code == 200, stage1.text
        stage1_id = stage1.json()["outputs"]["presentation"]["presentation_id"]
        store = get_memory_store()
        stage1_version = latest_version(store, UUID(stage1_id))
        stage1_manifest = dict(stage1_version["generation_source_manifest"])
        first_contact_ids = {
            row["id"]
            for row in store.presentation_versions.values()
            if row.get("journey_stage") == "first_contact"
        }
        edit_cover(client, opportunity_id, "Draft Only Client")
        notes = put_notes(client, opportunity_id, NOTE_A)
        first_transcript = upload_transcript(
            client,
            opportunity_id,
            b"Ada: Requirement: Keep slotting manual.\n",
        )
        upload_transcript(client, opportunity_id, b"Ben: A later transcript that must not be chosen.\n")
        extraction = extract(client, opportunity_id, first_transcript)
        selected = client.put(
            f"/opportunities/{opportunity_id}/selected-use-cases",
            headers=headers(),
            json={"use_case_ids": [INVOICE_ID]},
        )
        assert selected.status_code == 200, selected.text

        import app.services.data.memory_store as memory_store_module

        framework_slide_writer = memory_store_module.build_slide_spec_for_planned_slide

        def only_borek_slides(*, planned, framework_json):
            # PPT #2 slides come from the Borek master deck, not the framework slide writer.
            assert planned.get("borekSlide") is not None, (
                "PPT #2 must not use the framework slide writer"
            )
            return framework_slide_writer(planned=planned, framework_json=framework_json)

        monkeypatch.setattr(
            memory_store_module,
            "build_slide_spec_for_planned_slide",
            only_borek_slides,
        )
        generated = client.post(
            f"/opportunities/{opportunity_id}/ppt2/generate",
            headers=headers(),
        )
        assert generated.status_code == 200, generated.text
        body = generated.json()
        assert body["presentation_id"] != stage1_id
        assert body["journey_stage"] == "post_meeting"
        presentation_id = UUID(body["presentation_id"])
        version = latest_version(store, presentation_id)
        assert version["status"] == "ready"
        assert version["journey_stage"] == "post_meeting"
        assert version["version_number"] == 1
        assert version["pptx_storage_path"]
        assert version["pdf_storage_path"]
        assert version["preview_image_paths"]
        manifest = version["generation_source_manifest"]
        assert manifest["schema_version"] == "1.0"
        assert manifest["kind"] == "ppt2"
        assert manifest["approved_discovery_version_id"] == approved["id"]
        assert manifest["transcript_id"] == first_transcript
        assert manifest["meeting_extraction_generated_at"] == extraction["generated_at"]
        assert manifest["extraction_notes_revision"] == notes["updated_at"]
        assert manifest["current_personal_notes_updated_at"] == notes["updated_at"]
        assert manifest["selected_use_case_ids"] == [INVOICE_ID]
        manifest_blob = json.dumps(manifest)
        assert NOTE_A not in manifest_blob
        assert REQUIREMENT not in manifest_blob
        assert "paper_json" not in manifest
        assert "Draft Only Client" not in json.dumps(version["slides_json"])
        assert "Northwind" in str(version["slides_json"])
        assert version["slides_json"][0]["sourceChapterIds"] == ["discovery.cover"]
        rendered = json.dumps(version["slides_json"])
        # Audit 2026-10-27 (BUG-02): the owner's notes are internal; no slide shows them. They
        # stay in the frozen job input (checked below) for provenance and retries.
        assert NOTE_A not in rendered
        assert REQUIREMENT in rendered
        assert not [spec for spec in version["slides_json"] if spec["sourceChapterIds"] == ["notes"]]
        extraction_slide = next(
            spec for spec in version["slides_json"] if "meeting.requirements" in spec["sourceChapterIds"]
        )
        assert REQUIREMENT in json.dumps(extraction_slide)
        assert NOTE_A not in json.dumps(extraction_slide)
        use_case_slide = next(
            spec
            for spec in version["slides_json"]
            if spec["sourceChapterIds"] and spec["sourceChapterIds"][0].startswith("use_case.")
        )
        assert use_case_slide["sourceChapterIds"] == [f"use_case.{INVOICE_ID}"]
        job = next(
            row
            for row in store.generation_jobs.values()
            if str(row.get("presentation_id")) == str(presentation_id)
        )
        frozen = row_enqueue(job)["ppt2_generation_input"]
        assert frozen["source_priority"] == [
            "approved_discovery",
            "personal_notes",
            "meeting_extraction",
            "selected_use_cases",
        ]
        assert frozen["personal_notes"]["text"] == NOTE_A
        assert frozen["meeting_extraction"]["transcript_id"] == first_transcript
        assert frozen["approved_discovery"]["version_id"] == approved["id"]
        framework_blob = json.dumps(
            next(iter(store.framework_versions.values()))["framework_json"]
        )
        assert NOTE_A not in framework_blob
        assert "ppt2_context" not in framework_blob
        post_plan = next(
            plan["plan_json"]
            for plan in store.presentation_plans.values()
            if str(plan["plan_json"].get("title") or "").startswith("Post-meeting")
        )
        assert len(post_plan["slides"]) > 8
        pointer = store.opportunities[UUID(opportunity_id)]["stage1_outputs"]["outputs"]["presentation"]
        assert pointer["presentation_id"] == stage1_id
        assert latest_version(store, UUID(stage1_id))["id"] == stage1_version["id"]
        assert latest_version(store, UUID(stage1_id))["generation_source_manifest"] == stage1_manifest
        assert {
            row["id"]
            for row in store.presentation_versions.values()
            if row.get("journey_stage") == "first_contact"
        } == first_contact_ids
        assert "post_meeting" not in JOURNEY_STAGES
        eligibility = client.get(
            f"/opportunities/{opportunity_id}/journey-stage-eligibility",
            headers=headers(),
        )
        assert eligibility.status_code == 200, eligibility.text
        stages = {row["journey_stage"]: row for row in eligibility.json()["stages"]}
        assert list(stages) == list(JOURNEY_STAGES)
        assert "post_meeting" not in stages
        assert stages["concretisation"]["startable"] is False
        pptx = client.get(
            f"/presentations/{presentation_id}/download/pptx",
            headers=headers(),
        )
        pdf = client.get(
            f"/presentations/{presentation_id}/download/pdf",
            headers=headers(),
        )
        preview = client.get(
            f"/presentations/{presentation_id}/preview/slides/0.png",
            headers=headers(),
        )
        assert pptx.status_code == 200
        assert pdf.status_code == 200
        assert preview.status_code == 200


def row_enqueue(job: dict) -> dict:
    return dict((job.get("result_json") or {}).get("_enqueue") or {})


def test_optional_sources_and_stale_extraction_do_not_block() -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approve_discovery(client, opportunity_id)
        upload_transcript(client, opportunity_id, b"Ada: Hello.\n")
        bare = client.post(f"/opportunities/{opportunity_id}/ppt2/generate", headers=headers())
        assert bare.status_code == 200, bare.text
        store = get_memory_store()
        presentation_id = UUID(bare.json()["presentation_id"])
        version = latest_version(store, presentation_id)
        assert version["generation_source_manifest"]["transcript_id"] is None
        assert version["generation_source_manifest"]["selected_use_case_ids"] == []
        frozen = row_enqueue(next(iter(store.generation_jobs.values())))["ppt2_generation_input"]
        assert frozen["personal_notes"] is None
        assert frozen["meeting_extraction"] is None
        assert "personal_notes" in frozen["missing_sources"]
        assert "meeting_extraction" in frozen["missing_sources"]
        assert any(item["code"] == "MEETING_EXTRACTION_MISSING" for item in frozen["warnings"])

        put_notes(client, opportunity_id, NOTE_A)
        transcript_id = upload_transcript(
            client,
            opportunity_id,
            b"Ada: Requirement: Keep slotting manual.\n",
        )
        extract(client, opportunity_id, transcript_id)
        put_notes(client, opportunity_id, NOTE_B)
        stale = client.post(
            f"/opportunities/{opportunity_id}/ppt2/{presentation_id}/regenerate",
            headers=headers(),
        )
        assert stale.status_code == 200, stale.text
        regenerated = latest_version(store, presentation_id)
        assert regenerated["version_number"] == 2
        assert regenerated["presentation_id"] == presentation_id
        stale_input = row_enqueue(
            max(store.generation_jobs.values(), key=lambda row: str(row["created_at"]))
        )["ppt2_generation_input"]
        assert any(
            item["code"] == "MEETING_EXTRACTION_NOTES_STALE" for item in stale_input["warnings"]
        )
        assert stale_input["notes_revision_matches_extraction"] is False
        assert stale_input["personal_notes"]["text"] == NOTE_B, "the regeneration froze the current notes"
        assert NOTE_B not in json.dumps(regenerated["slides_json"]), "internal notes are never on a slide"


def test_worker_and_retry_use_the_frozen_input(monkeypatch) -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approved = approve_discovery(client, opportunity_id)
        put_notes(client, opportunity_id, NOTE_A)
        from app.services import presentation_generation
        import app.services.ppt2_context as ppt2_context

        original_builder = ppt2_context.build_ppt2_context
        held: list[tuple] = []

        def capture(task, *args):
            held.append((task, args))

        def forbid(*_args, **_kwargs):
            raise AssertionError("worker must not rebuild BT-46 context")

        monkeypatch.setattr(presentation_generation, "_dispatch_task", capture)
        queued = client.post(
            f"/opportunities/{opportunity_id}/ppt2/generate",
            headers=headers(),
        )
        assert queued.status_code == 200, queued.text
        put_notes(client, opportunity_id, NOTE_B)
        monkeypatch.setattr(ppt2_context, "build_ppt2_context", forbid)
        task, args = held[0]
        task.run(*args)
        store = get_memory_store()
        presentation_id = UUID(queued.json()["presentation_id"])
        version = latest_version(store, presentation_id)
        # The worker used the input frozen at the request (notes A), not the later notes B. The
        # notes are internal: frozen with the job, never on a slide (audit 2026-10-27, BUG-02).
        first_job = min(store.generation_jobs.values(), key=lambda row: str(row["created_at"]))
        assert row_enqueue(first_job)["ppt2_generation_input"]["personal_notes"]["text"] == NOTE_A
        assert NOTE_A not in json.dumps(version["slides_json"])
        assert NOTE_B not in json.dumps(version["slides_json"])
        assert version["generation_source_manifest"]["approved_discovery_version_id"] == approved["id"]

        original_execute = presentation_generation.execute_presentation_generation
        calls = {"count": 0}

        def fail_once(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("renderer unavailable")
            return original_execute(*args, **kwargs)

        def run_and_keep_the_failed_job(task, *a):
            try:
                task.run(*a)
            except Exception:
                return None

        monkeypatch.setattr(presentation_generation, "_dispatch_task", run_and_keep_the_failed_job)
        monkeypatch.setattr(presentation_generation, "execute_presentation_generation", fail_once)
        monkeypatch.setattr(ppt2_context, "build_ppt2_context", original_builder)
        failed = client.post(
            f"/opportunities/{opportunity_id}/ppt2/{presentation_id}/regenerate",
            headers=headers(),
        )
        assert failed.status_code == 200, failed.text
        put_notes(client, opportunity_id, "Revision C must not be picked up by retry")
        monkeypatch.setattr(ppt2_context, "build_ppt2_context", forbid)
        job = max(store.generation_jobs.values(), key=lambda row: str(row["created_at"]))
        retried = client.post(f"/jobs/{job['id']}/retry", headers=headers())
        assert retried.status_code == 202, retried.text
        retried_version = latest_version(store, presentation_id)
        assert row_enqueue(job)["ppt2_generation_input"]["personal_notes"]["text"] == NOTE_B, "the retry keeps the frozen input"
        assert NOTE_B not in json.dumps(retried_version["slides_json"])
        assert "Revision C" not in json.dumps(retried_version["slides_json"])
        assert retried_version["generation_source_manifest"]["approved_discovery_version_id"] == approved["id"]


def test_explicit_regenerate_reuses_the_ppt2_id_and_refreshes_context() -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approve_discovery(client, opportunity_id)
        put_notes(client, opportunity_id, NOTE_A)
        stage1 = client.post(
            f"/opportunities/{opportunity_id}/stage1-outputs/generate",
            headers=headers(),
        )
        stage1_id = stage1.json()["outputs"]["presentation"]["presentation_id"]
        first = client.post(f"/opportunities/{opportunity_id}/ppt2/generate", headers=headers())
        assert first.status_code == 200, first.text
        presentation_id = first.json()["presentation_id"]
        store = get_memory_store()
        first_version = latest_version(store, UUID(presentation_id))
        assert NOTE_A not in json.dumps(first_version["slides_json"]), "internal notes are never on a slide"
        put_notes(client, opportunity_id, NOTE_B)
        edit_cover(client, opportunity_id, "Second Approved Client")
        second_paper = client.post(
            f"/opportunities/{opportunity_id}/discovery-paper/approve",
            headers=headers(),
        )
        assert second_paper.status_code == 200, second_paper.text
        regenerated = client.post(
            f"/opportunities/{opportunity_id}/ppt2/{presentation_id}/regenerate",
            headers=headers(),
        )
        assert regenerated.status_code == 200, regenerated.text
        assert regenerated.json()["presentation_id"] == presentation_id
        second = latest_version(store, UUID(presentation_id))
        assert second["version_number"] == 2
        assert second["journey_stage"] == "post_meeting"
        latest_job = max(store.generation_jobs.values(), key=lambda row: str(row["created_at"]))
        assert row_enqueue(latest_job)["ppt2_generation_input"]["personal_notes"]["text"] == NOTE_B, "regeneration refreshed the context"
        assert NOTE_B not in json.dumps(second["slides_json"])
        assert "Second Approved Client" in str(second["slides_json"])
        assert (
            second["generation_source_manifest"]["approved_discovery_version_id"]
            == second_paper.json()["id"]
        )
        assert "Northwind" in str(first_version["slides_json"])
        edit_cover(client, opportunity_id, "Draft Only Client")
        still = client.post(
            f"/opportunities/{opportunity_id}/ppt2/{presentation_id}/regenerate",
            headers=headers(),
        )
        assert still.status_code == 200, still.text
        third = latest_version(store, UUID(presentation_id))
        assert third["version_number"] == 3
        assert "Second Approved Client" in str(third["slides_json"])
        assert "Draft Only Client" not in json.dumps(third["slides_json"])
        assert len(store.presentations) == 2
        stolen = client.post(
            f"/opportunities/{opportunity_id}/ppt2/{stage1_id}/regenerate",
            headers=headers(),
        )
        assert stolen.status_code == 400
        assert stolen.json()["error"]["code"] == "PPT2_MUST_NOT_USE_STAGE1_PRESENTATION"
        assert latest_version(store, UUID(stage1_id))["journey_stage"] == "first_contact"
        pointer = store.opportunities[UUID(opportunity_id)]["stage1_outputs"]["outputs"]["presentation"]
        assert pointer["presentation_id"] == stage1_id


def test_second_initial_generate_reuses_the_in_progress_ppt2(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import presentation_generation

    reset_memory_store()
    monkeypatch.setattr(presentation_generation, "_dispatch_task", lambda *_args, **_kwargs: None)
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approve_discovery(client, opportunity_id)
        first = client.post(f"/opportunities/{opportunity_id}/ppt2/generate", headers=headers())
        assert first.status_code == 200, first.text
        presentation_id = first.json()["presentation_id"]
        store = get_memory_store()
        presentation_count = len(store.presentations)
        second = client.post(f"/opportunities/{opportunity_id}/ppt2/generate", headers=headers())
        assert second.status_code == 200, second.text
        assert second.json()["presentation_id"] == presentation_id
        assert second.json()["job_id"] == first.json()["job_id"]
        assert second.json()["is_existing_job"] is True
        assert len(store.presentations) == presentation_count
        assert "ppt2_generation_input" not in second.text


def test_completed_ppt2_rejects_a_second_initial_generate_and_regenerate_keeps_the_id() -> None:
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approve_discovery(client, opportunity_id)
        put_notes(client, opportunity_id, NOTE_A)
        first = client.post(f"/opportunities/{opportunity_id}/ppt2/generate", headers=headers())
        assert first.status_code == 200, first.text
        presentation_id = first.json()["presentation_id"]
        store = get_memory_store()
        before = {
            str(row["presentation_id"])
            for row in store.presentation_versions.values()
            if row.get("journey_stage") == "post_meeting"
        }
        second = client.post(f"/opportunities/{opportunity_id}/ppt2/generate", headers=headers())
        assert second.status_code == 400, second.text
        error = second.json()["error"]
        assert error["code"] == "PPT2_ALREADY_EXISTS"
        assert error["detail"]["presentation_id"] == presentation_id
        assert f"/ppt2/{presentation_id}/regenerate" in error["message"]
        after = {
            str(row["presentation_id"])
            for row in store.presentation_versions.values()
            if row.get("journey_stage") == "post_meeting"
        }
        assert after == before == {presentation_id}
        put_notes(client, opportunity_id, NOTE_B)
        regenerated = client.post(
            f"/opportunities/{opportunity_id}/ppt2/{presentation_id}/regenerate",
            headers=headers(),
        )
        assert regenerated.status_code == 200, regenerated.text
        assert regenerated.json()["presentation_id"] == presentation_id
        assert latest_version(store, UUID(presentation_id))["version_number"] == 2
        assert after == {
            str(row["presentation_id"])
            for row in store.presentation_versions.values()
            if row.get("journey_stage") == "post_meeting"
        }


def test_frozen_ppt2_input_is_not_exposed_and_retry_can_still_read_it() -> None:
    from app.services.job_retry import enqueue_payload
    from app.services.job_service import get_job

    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approve_discovery(client, opportunity_id)
        put_notes(client, opportunity_id, NOTE_A)
        stage1 = client.post(
            f"/opportunities/{opportunity_id}/stage1-outputs/generate",
            headers=headers(),
        )
        assert stage1.status_code == 200, stage1.text
        generated = client.post(f"/opportunities/{opportunity_id}/ppt2/generate", headers=headers())
        assert generated.status_code == 200, generated.text
        presentation_id = generated.json()["presentation_id"]
        job_id = generated.json()["job_id"]
        public_job = client.get(f"/jobs/{job_id}", headers=headers())
        assert public_job.status_code == 200, public_job.text
        assert "ppt2_generation_input" not in public_job.text
        assert NOTE_A not in public_job.text
        store = get_memory_store()
        stored = get_job(UUID(job_id), repository=store)
        assert stored is not None
        frozen = enqueue_payload(stored)["ppt2_generation_input"]
        assert NOTE_A in json.dumps(frozen)
        workflow = client.get(f"/opportunities/{opportunity_id}/workflow-status", headers=headers())
        presentation = client.get(f"/presentations/{presentation_id}", headers=headers())
        activity = client.get("/employees/activity", headers=headers())
        assert workflow.status_code == 200, workflow.text
        assert presentation.status_code == 200, presentation.text
        assert activity.status_code == 200, activity.text
        for body in (workflow.text, presentation.text, activity.text):
            assert "ppt2_generation_input" not in body
            assert NOTE_A not in body
        completed = client.post(
            f"/opportunities/{opportunity_id}/workflow/first-meeting-completed",
            headers=headers(),
        )
        reviewed = client.post(
            f"/opportunities/{opportunity_id}/workflow/owner-reviewed",
            headers=headers(),
        )
        finalized = client.post(
            f"/opportunities/{opportunity_id}/workflow/finalize",
            headers=headers(),
        )
        assert completed.status_code == 200, completed.text
        assert reviewed.status_code == 200, reviewed.text
        assert finalized.status_code == 200, finalized.text
        assert "ppt2_generation_input" not in finalized.text
        assert NOTE_A not in finalized.text
        snapshot = store.opportunities[UUID(opportunity_id)]["finalization_snapshot"]
        assert snapshot["ppt2_presentation_id"] == presentation_id
        assert snapshot["ppt2_version_id"] == str(latest_version(store, UUID(presentation_id))["id"])
        assert "ppt2_generation_input" not in json.dumps(snapshot)
        assert NOTE_A not in json.dumps(snapshot)
        assert enqueue_payload(get_job(UUID(job_id), repository=store)).get("ppt2_generation_input")


def test_workflow_and_finalization_do_not_guess_among_ppt2_identities() -> None:
    import copy
    import uuid

    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approve_discovery(client, opportunity_id)
        first = client.post(f"/opportunities/{opportunity_id}/ppt2/generate", headers=headers())
        assert first.status_code == 200, first.text
        presentation_id = UUID(first.json()["presentation_id"])
        store = get_memory_store()
        clone_id = uuid.uuid4()
        cloned = copy.deepcopy(store.presentations[presentation_id])
        cloned["id"] = clone_id
        store.presentations[clone_id] = cloned
        cloned_version = copy.deepcopy(latest_version(store, presentation_id))
        cloned_version["id"] = uuid.uuid4()
        cloned_version["presentation_id"] = clone_id
        store.presentation_versions[cloned_version["id"]] = cloned_version
        workflow = client.get(f"/opportunities/{opportunity_id}/workflow-status", headers=headers())
        assert workflow.status_code == 400, workflow.text
        assert workflow.json()["error"]["code"] == "PPT2_IDENTITY_AMBIGUOUS"
        duplicate = client.post(f"/opportunities/{opportunity_id}/ppt2/generate", headers=headers())
        assert duplicate.status_code == 400, duplicate.text
        assert duplicate.json()["error"]["code"] == "PPT2_IDENTITY_AMBIGUOUS"
        store.opportunities[UUID(opportunity_id)]["owner_reviewed_at"] = "2026-06-01T00:00:00+00:00"
        finalized = client.post(f"/opportunities/{opportunity_id}/workflow/finalize", headers=headers())
        assert finalized.status_code == 400, finalized.text
        assert finalized.json()["error"]["code"] == "PPT2_IDENTITY_AMBIGUOUS"
        assert store.opportunities[UUID(opportunity_id)].get("finalization_snapshot") is None
