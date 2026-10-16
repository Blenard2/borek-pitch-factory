"""Post Meeting: transcript, personal notes, extraction provenance, review, confirmation, V2 sources."""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from uuid import UUID, uuid4

import jsonschema
import pytest
from fastapi.testclient import TestClient

from app.auth import create_test_access_token
from app.config import settings
from app.main import create_app
from app.services import meeting_extraction as extraction_service
from app.services.data.memory_store import get_memory_store, reset_memory_store
from services.meeting.extraction import MeetingExtractionError, classify_item_sources, validate_meeting_extraction

OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CONTRACT = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "post_meeting_review.schema.json"
CATEGORIES = ("requirements", "challenges", "priorities", "opportunities", "discussed_solutions", "decisions", "follow_ups")
CLIENT = {
    "client_name": "Nordwind Maschinenbau",
    "opportunity_name": "AI introduction",
    "department": "Sales",
    "stage1_intake": {
        "poc_name": "Dana Weber",
        "sales_topic_description": "Reduce quote turnaround",
        "about_company": "Family-owned machine builder with 420 employees.",
    },
}
MEETING = (
    "Dana: Requirement: Quotes must go out within one day.\n"
    "Tom: Challenge: Pricing data sits in three systems.\n"
    "Dana: Priority: Start with the sales team.\n"
    "Tom: Decision: Run a pilot with ten quotes.\n"
    "Dana: Follow-up: Send the pricing export by Friday.\n"
    "Tom: We also talked about the trade fair.\n"
)
NOTES = "Priority: Start with the sales team.\nOpportunity: The service team could reuse the quoting assistant."


def headers(user: UUID = OWNER) -> dict[str, str]:
    token = create_test_access_token(user_id=user, email="sales@example.com", secret=settings.SUPABASE_JWT_SECRET)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client() -> TestClient:
    reset_memory_store()
    return TestClient(create_app())


def create(client: TestClient, user: UUID = OWNER) -> str:
    response = client.post("/opportunities", headers=headers(user), json=CLIENT)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def upload(client: TestClient, opportunity_id: str, body: str = MEETING, *, name: str = "meeting.txt", user: UUID = OWNER) -> str:
    response = client.post(
        f"/opportunities/{opportunity_id}/transcripts",
        headers=headers(user),
        files={"file": (name, io.BytesIO(body.encode()), "text/plain")},
    )
    return ok(response, 201)["transcript"]["id"]


def save_notes(client: TestClient, opportunity_id: str, text: str | None) -> dict:
    return ok(client.put(f"/opportunities/{opportunity_id}/personal-notes", headers=headers(), json={"text": text}))


def analyse(client: TestClient, opportunity_id: str, transcript_id: str) -> dict:
    return ok(client.post(f"/opportunities/{opportunity_id}/meeting-extraction/generate", headers=headers(), json={"transcript_id": transcript_id}))


def review(client: TestClient, opportunity_id: str, user: UUID = OWNER) -> dict:
    body = ok(client.get(f"/opportunities/{opportunity_id}/post-meeting-review", headers=headers(user)))
    jsonschema.Draft202012Validator(json.loads(CONTRACT.read_text(encoding="utf-8")), format_checker=jsonschema.FormatChecker()).validate(body)
    return body


def confirm(client: TestClient, opportunity_id: str, view: dict, excluded: dict | None = None):
    return client.post(
        f"/opportunities/{opportunity_id}/post-meeting-review/confirm",
        headers=headers(),
        json={
            "transcript_id": view["extraction"]["transcript_id"],
            "extraction_generated_at": view["extraction"]["generated_at"],
            "review_fingerprint": view["review_fingerprint"],
            "excluded": excluded or {},
        },
    )


def master_v1_and_meeting(client: TestClient, opportunity_id: str) -> dict:
    """Approved Discovery v2, ready Master Presentation V1, first meeting marked completed."""
    ok(client.post(f"/opportunities/{opportunity_id}/discovery-paper/generate", headers=headers()))
    approved = ok(client.post(f"/opportunities/{opportunity_id}/discovery-paper/approve", headers=headers()))
    deck = ok(client.post(f"/opportunities/{opportunity_id}/stage1-outputs/generate", headers=headers()))["outputs"]["presentation"]
    assert deck["status"] == "ready", deck
    ok(client.post(f"/opportunities/{opportunity_id}/workflow/first-meeting-completed", headers=headers()))
    return {"approved": approved, "presentation_id": deck["presentation_id"]}


def test_new_opportunity_shows_every_source_as_missing_and_nothing_as_confirmed(client: TestClient) -> None:
    opportunity_id = create(client)
    view = review(client, opportunity_id)
    assert view["transcripts"] == []
    assert view["personal_notes"] == {"status": "missing", "text": None, "updated_at": None}
    assert (view["extraction"]["status"], view["extraction"]["item_count"]) == ("missing", 0)
    assert view["extraction"]["categories"] == {category: [] for category in CATEGORIES}
    assert view["confirmation"]["status"] == "none" and view["confirmation"]["items"] is None
    assert view["selected_use_cases"]["status"] == "empty"
    assert view["readiness"] == {
        "ready_for_v2": False,
        "blockers": [
            "DISCOVERY_NOT_APPROVED",
            "MASTER_PRESENTATION_V1_NOT_READY",
            "FIRST_MEETING_NOT_COMPLETED",
            "TRANSCRIPT_MISSING",
            "MEETING_EXTRACTION_MISSING",
            "MEETING_REVIEW_NOT_CONFIRMED",
        ],
    }
    assert view["v2_sources"] is None
    assert view["execution_mode"] == "fixture"


def test_transcripts_are_validated_listed_and_bound_to_their_opportunity(client: TestClient) -> None:
    opportunity_id = create(client)
    other_id = create(client, OTHER)
    foreign_transcript = upload(client, other_id, "Cara: Requirement: Someone else's meeting.\n", user=OTHER)

    for name, body, code in (
        ("meeting.pdf", MEETING, "INVALID_TRANSCRIPT_FORMAT"),
        ("meeting.txt", "", "INVALID_TRANSCRIPT_CONTENT"),
    ):
        rejected = client.post(
            f"/opportunities/{opportunity_id}/transcripts",
            headers=headers(),
            files={"file": (name, io.BytesIO(body.encode()), "text/plain")},
        )
        assert rejected.status_code == 400 and code in rejected.text, rejected.text
    assert review(client, opportunity_id)["transcripts"] == []

    first = upload(client, opportunity_id, name="first-call.txt")
    second = upload(client, opportunity_id, "Dana: Requirement: A second conversation.\n", name="second-call.txt")
    listed = review(client, opportunity_id)["transcripts"]
    assert [(item["id"], item["file_name"], item["analysed"]) for item in listed] == [
        (first, "first-call.txt", False),
        (second, "second-call.txt", False),
    ]
    assert listed[0]["turn_count"] == 6 and listed[0]["revision"] != listed[1]["revision"]
    assert "storage_path" not in json.dumps(listed)

    # Another user's opportunity and transcript are out of reach, in both directions.
    assert client.get(f"/opportunities/{opportunity_id}/post-meeting-review", headers=headers(OTHER)).status_code in (403, 404)
    assert client.get(f"/opportunities/{opportunity_id}/post-meeting-review").status_code == 401
    assert client.post(f"/opportunities/{opportunity_id}/post-meeting-review/confirm", json={}).status_code == 401
    foreign = client.post(
        f"/opportunities/{opportunity_id}/meeting-extraction/generate", headers=headers(), json={"transcript_id": foreign_transcript}
    )
    assert foreign.status_code == 404 and "TRANSCRIPT_NOT_FOUND" in foreign.text
    assert foreign_transcript not in json.dumps(review(client, opportunity_id))
    assert [item["id"] for item in review(client, other_id, OTHER)["transcripts"]] == [foreign_transcript]


def test_notes_persist_separately_and_keep_their_revision(client: TestClient) -> None:
    opportunity_id = create(client)
    transcript_id = upload(client, opportunity_id)
    saved = save_notes(client, opportunity_id, NOTES)
    view = review(client, opportunity_id)
    assert view["personal_notes"] == {"status": "available", "text": NOTES, "updated_at": saved["updated_at"]}
    assert review(client, opportunity_id)["personal_notes"] == view["personal_notes"], "reload returns the same notes"
    # Notes never become transcript content.
    store = get_memory_store()
    sections = store.list_transcript_sources(opportunity_id=UUID(opportunity_id), user_id=OWNER)[0]["sections"]
    assert "service team" not in json.dumps(sections)
    assert view["transcripts"][0]["id"] == transcript_id and view["transcripts"][0]["turn_count"] == 6
    cleared = save_notes(client, opportunity_id, "   ")
    assert cleared["text"] is None
    assert review(client, opportunity_id)["personal_notes"]["status"] == "missing"


def test_extraction_records_revision_mode_and_the_source_of_every_item(client: TestClient) -> None:
    opportunity_id = create(client)
    transcript_id = upload(client, opportunity_id)
    notes = save_notes(client, opportunity_id, NOTES)
    stored = analyse(client, opportunity_id, transcript_id)
    validate_meeting_extraction(stored)
    assert stored["execution_mode"] == "fixture"
    assert stored["personal_notes_updated_at"] == notes["updated_at"]
    assert stored["item_sources"]["priorities"] == ["both"]
    assert stored["item_sources"]["opportunities"] == ["personal_notes"]
    assert stored["item_sources"]["requirements"] == ["transcript"]
    assert "personal_notes" not in stored, "the notes text is never copied into the extraction"

    view = review(client, opportunity_id)["extraction"]
    assert (view["status"], view["stale_reasons"], view["execution_mode"]) == ("current", [], "fixture")
    assert view["transcript_id"] == transcript_id and view["transcript_file_name"] == "meeting.txt"
    assert view["transcript_revision"] == stored["transcript_revision"]
    assert view["categories"]["requirements"] == [{"text": "Quotes must go out within one day.", "source": "transcript"}]
    assert view["categories"]["priorities"] == [{"text": "Start with the sales team.", "source": "both"}]
    assert view["categories"]["opportunities"] == [
        {"text": "The service team could reuse the quoting assistant.", "source": "personal_notes"}
    ]
    # Nothing is invented: unmarked talk yields nothing, and empty categories stay empty.
    assert view["categories"]["discussed_solutions"] == [] and "trade fair" not in json.dumps(view)
    assert view["item_count"] == 6
    assert [item["analysed"] for item in review(client, opportunity_id)["transcripts"]] == [True]


def test_an_item_without_a_source_is_rejected_not_stored() -> None:
    sections = [{"speaker_role": "Dana", "content": "Requirement: Quotes within one day."}]
    assert classify_item_sources({"requirements": ["Quotes within one day."]}, sections=sections, personal_notes=None)["requirements"] == ["transcript"]
    with pytest.raises(MeetingExtractionError, match="not supported"):
        classify_item_sources({"decisions": ["Budget of 2 million approved."]}, sections=sections, personal_notes="Priority: Sales first.")


def test_a_failed_extraction_keeps_the_previous_result_and_can_be_retried(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    opportunity_id = create(client)
    transcript_id = upload(client, opportunity_id)
    first = analyse(client, opportunity_id, transcript_id)

    def broken(**_kwargs):
        raise MeetingExtractionError("The model did not answer.")

    monkeypatch.setattr(extraction_service, "extract_meeting_findings", broken)
    failed = client.post(f"/opportunities/{opportunity_id}/meeting-extraction/generate", headers=headers(), json={"transcript_id": transcript_id})
    assert failed.status_code == 400 and "MEETING_EXTRACTION_FAILED" in failed.text and "did not answer" in failed.text
    assert ok(client.get(f"/opportunities/{opportunity_id}/meeting-extraction", headers=headers())) == first
    assert review(client, opportunity_id)["extraction"]["status"] == "current"

    monkeypatch.undo()
    retried = analyse(client, opportunity_id, transcript_id)
    assert {key: retried[key] for key in CATEGORIES} == {key: first[key] for key in CATEGORIES}
    assert retried["generated_at"] >= first["generated_at"]

    # A transcript without readable text is refused before anything is stored.
    store = get_memory_store()
    empty_id = upload(client, opportunity_id, "Dana: placeholder\n", name="empty.txt")
    store.transcripts[UUID(empty_id)]["sections"] = [{"section_index": 0, "speaker_role": "Dana", "content": "  "}]
    empty = client.post(f"/opportunities/{opportunity_id}/meeting-extraction/generate", headers=headers(), json={"transcript_id": empty_id})
    assert empty.status_code == 400 and "TRANSCRIPT_EMPTY" in empty.text
    assert review(client, opportunity_id)["extraction"]["transcript_id"] == transcript_id


def test_changed_notes_or_transcript_make_the_extraction_stale(client: TestClient) -> None:
    opportunity_id = create(client)
    transcript_id = upload(client, opportunity_id)
    save_notes(client, opportunity_id, NOTES)
    analyse(client, opportunity_id, transcript_id)
    assert review(client, opportunity_id)["extraction"]["status"] == "current"

    save_notes(client, opportunity_id, NOTES + "\nDecision: Owner will sponsor the pilot.")
    stale = review(client, opportunity_id)["extraction"]
    assert (stale["status"], stale["stale_reasons"]) == ("stale", ["NOTES_CHANGED"])
    assert "Owner will sponsor the pilot." not in json.dumps(stale["categories"]), "old results are shown as they were"
    assert analyse(client, opportunity_id, transcript_id)["decisions"] == ["Run a pilot with ten quotes.", "Owner will sponsor the pilot."]
    assert review(client, opportunity_id)["extraction"]["status"] == "current"

    save_notes(client, opportunity_id, None)
    assert review(client, opportunity_id)["extraction"]["stale_reasons"] == ["NOTES_CHANGED"]
    analyse(client, opportunity_id, transcript_id)

    store = get_memory_store()
    store.transcripts[UUID(transcript_id)]["sections"][0]["content"] = "Requirement: Quotes must go out within two days."
    assert review(client, opportunity_id)["extraction"]["stale_reasons"] == ["TRANSCRIPT_CHANGED"]
    analyse(client, opportunity_id, transcript_id)

    removed = client.delete(f"/opportunities/{opportunity_id}/transcripts/{transcript_id}", headers=headers())
    assert removed.status_code == 204
    gone = review(client, opportunity_id)
    assert (gone["extraction"]["status"], gone["extraction"]["stale_reasons"]) == ("stale", ["TRANSCRIPT_REMOVED"])
    assert gone["extraction"]["transcript_file_name"] is None
    assert "TRANSCRIPT_MISSING" in gone["readiness"]["blockers"] and "MEETING_EXTRACTION_STALE" in gone["readiness"]["blockers"]


def test_an_extraction_from_before_revision_tracking_still_loads_and_asks_for_a_new_analysis(client: TestClient) -> None:
    opportunity_id = create(client)
    transcript_id = upload(client, opportunity_id)
    legacy = analyse(client, opportunity_id, transcript_id)
    for key in ("transcript_revision", "execution_mode", "item_sources"):
        legacy.pop(key)
    validate_meeting_extraction(legacy)  # the previous shape is still a valid extraction
    get_memory_store().opportunities[UUID(opportunity_id)]["meeting_extraction"] = copy.deepcopy(legacy)

    assert ok(client.get(f"/opportunities/{opportunity_id}/meeting-extraction", headers=headers())) == legacy
    assert ok(client.get(f"/opportunities/{opportunity_id}/ppt2-context", headers=headers()))["sources"]["meeting_extraction"]["status"] == "available"
    view = review(client, opportunity_id)
    assert (view["extraction"]["status"], view["extraction"]["stale_reasons"]) == ("stale", ["TRANSCRIPT_REVISION_NOT_RECORDED"])
    assert view["extraction"]["execution_mode"] is None
    assert view["extraction"]["categories"]["requirements"] == [{"text": "Quotes must go out within one day.", "source": "transcript"}]
    refused = confirm(client, opportunity_id, view)
    assert refused.status_code == 409 and "MEETING_REVIEW_STALE" in refused.text
    analyse(client, opportunity_id, transcript_id)
    assert review(client, opportunity_id)["extraction"]["status"] == "current"


def test_confirmation_is_explicit_per_item_and_bound_to_the_reviewed_sources(client: TestClient) -> None:
    opportunity_id = create(client)
    transcript_id = upload(client, opportunity_id)
    save_notes(client, opportunity_id, NOTES)

    nothing = client.post(
        f"/opportunities/{opportunity_id}/post-meeting-review/confirm",
        headers=headers(),
        json={"transcript_id": transcript_id, "extraction_generated_at": "2026-01-01T00:00:00Z", "review_fingerprint": "0" * 64},
    )
    assert nothing.status_code == 400 and "MEETING_EXTRACTION_MISSING" in nothing.text

    analyse(client, opportunity_id, transcript_id)
    view = review(client, opportunity_id)
    assert view["confirmation"]["status"] == "none", "an extraction alone confirms nothing"

    reviewed = {
        "transcript_id": transcript_id,
        "extraction_generated_at": view["extraction"]["generated_at"],
        "review_fingerprint": view["review_fingerprint"],
    }
    for body, status, code in (
        ({**reviewed, "transcript_id": str(uuid4())}, 409, "MEETING_REVIEW_STALE"),
        ({**reviewed, "extraction_generated_at": "2026-01-01T00:00:00Z"}, 409, "MEETING_REVIEW_STALE"),
        ({**reviewed, "review_fingerprint": "0" * 64}, 409, "MEETING_REVIEW_STALE"),
        ({**reviewed, "excluded": {"budget": ["x"]}}, 400, "MEETING_REVIEW_INVALID"),
        ({**reviewed, "excluded": {"decisions": ["Never said."]}}, 400, "MEETING_REVIEW_INVALID"),
        ({**reviewed, "items": {}}, 422, "VALIDATION_ERROR"),
        ({key: value for key, value in reviewed.items() if key != "review_fingerprint"}, 422, "VALIDATION_ERROR"),
        ({**reviewed, "review_fingerprint": "not-a-fingerprint"}, 422, "VALIDATION_ERROR"),
    ):
        response = client.post(f"/opportunities/{opportunity_id}/post-meeting-review/confirm", headers=headers(), json=body)
        assert response.status_code == status and code in response.text, response.text
    assert review(client, opportunity_id)["confirmation"]["status"] == "none"
    assert confirm(client, opportunity_id, view).status_code == 200  # as the owner
    foreign = client.post(
        f"/opportunities/{opportunity_id}/post-meeting-review/confirm",
        headers=headers(OTHER),
        json=reviewed,
    )
    assert foreign.status_code in (403, 404)

    confirmed = ok(confirm(client, opportunity_id, view, {"follow_ups": ["Send the pricing export by Friday."]}))["confirmation"]
    assert (confirmed["status"], confirmed["confirmed_count"], confirmed["excluded_count"]) == ("current", 5, 1)
    assert confirmed["confirmed_by"] == str(OWNER)
    assert confirmed["items"]["follow_ups"] == [{"text": "Send the pricing export by Friday.", "source": "transcript", "status": "excluded"}]
    assert confirmed["items"]["opportunities"][0] == {
        "text": "The service team could reuse the quoting assistant.", "source": "personal_notes", "status": "confirmed",
    }
    assert confirmed["sources"]["transcript_id"] == transcript_id
    assert confirmed["sources"]["transcript_revision"] == view["extraction"]["transcript_revision"]
    assert review(client, opportunity_id)["confirmation"] == confirmed, "the confirmation is stored and reloaded"
    # The extraction itself is not rewritten by a confirmation, and the opportunity payload does not carry it.
    assert "Send the pricing export by Friday." in ok(client.get(f"/opportunities/{opportunity_id}/meeting-extraction", headers=headers()))["follow_ups"]
    assert "meeting_review" not in ok(client.get(f"/opportunities/{opportunity_id}", headers=headers()))
    audit = [entry for entry in get_memory_store().audit_logs.values() if entry["action"] == "meeting_review.confirm"]
    assert len(audit) == 2 and "pricing export" not in str(audit)

    # Any later change of a source makes the confirmation stale instead of carrying it over.
    save_notes(client, opportunity_id, NOTES + "\nDecision: Owner sponsors the pilot.")
    stale = review(client, opportunity_id)
    assert (stale["confirmation"]["status"], stale["confirmation"]["stale_reasons"]) == ("stale", ["NOTES_CHANGED"])
    assert stale["confirmation"]["items"] == confirmed["items"], "what was confirmed stays visible"
    assert confirm(client, opportunity_id, stale).status_code == 409
    analyse(client, opportunity_id, transcript_id)
    replaced = review(client, opportunity_id)
    assert (replaced["extraction"]["status"], replaced["confirmation"]["stale_reasons"]) == ("current", ["EXTRACTION_REPLACED"])
    assert "MEETING_REVIEW_STALE" in replaced["readiness"]["blockers"]
    again = ok(confirm(client, opportunity_id, replaced))
    assert (again["confirmation"]["status"], again["confirmation"]["excluded_count"]) == ("current", 0)


def test_ready_for_v2_needs_every_source_and_names_the_same_presentation(client: TestClient) -> None:
    opportunity_id = create(client)
    base = master_v1_and_meeting(client, opportunity_id)
    start = review(client, opportunity_id)
    assert start["master_presentation"]["status"] == "ready"
    assert start["master_presentation"]["presentation_id"] == base["presentation_id"]
    assert start["approved_discovery"]["version_id"] == base["approved"]["id"]
    assert start["approved_discovery"]["discovery_schema_version"] == "2.0"
    assert start["readiness"]["blockers"] == ["TRANSCRIPT_MISSING", "MEETING_EXTRACTION_MISSING", "MEETING_REVIEW_NOT_CONFIRMED"]

    transcript_id = upload(client, opportunity_id)
    notes = save_notes(client, opportunity_id, NOTES)
    analyse(client, opportunity_id, transcript_id)
    reviewed = review(client, opportunity_id)
    assert reviewed["readiness"]["blockers"] == ["MEETING_REVIEW_NOT_CONFIRMED"] and reviewed["v2_sources"] is None
    ready = ok(confirm(client, opportunity_id, reviewed, {"follow_ups": ["Send the pricing export by Friday."]}))
    assert ready["readiness"] == {"ready_for_v2": True, "blockers": []}

    sources = ready["v2_sources"]
    version_id = ready["master_presentation"]["version_id"]
    assert sources["presentation_id"] == base["presentation_id"], "V2 will be a version of the same presentation"
    assert sources["base_presentation_version_id"] == version_id
    assert sources["approved_discovery_version_id"] == base["approved"]["id"]
    assert sources["transcript_id"] == transcript_id
    assert sources["transcript_revision"] == ready["extraction"]["transcript_revision"]
    assert sources["personal_notes_updated_at"] == notes["updated_at"]
    assert sources["extraction_execution_mode"] == "fixture"
    assert sources["selected_use_case_ids"] == []
    # Identifiers and revisions only: no notes, transcript or finding text.
    assert "service team" not in json.dumps(sources) and "Quotes must" not in json.dumps(sources)
    assert review(client, opportunity_id)["v2_sources"] == sources, "the same sources give the same snapshot"

    # Reading and confirming generate nothing and create no second presentation.
    store = get_memory_store()
    assert len(store.presentations) == 1
    versions = [row for row in store.presentation_versions.values() if str(row["presentation_id"]) == base["presentation_id"]]
    assert [(str(row["id"]), row["journey_stage"]) for row in versions] == [(version_id, "first_contact")]
    workflow = ok(client.get(f"/opportunities/{opportunity_id}/workflow-status", headers=headers()))
    assert workflow["documents"]["ppt2"] is None
    assert {step["key"]: step["state"] for step in workflow["steps"]}["owner_review"] == "pending"

    # A different confirmation is a different snapshot.
    other = ok(confirm(client, opportunity_id, ready))
    assert other["v2_sources"]["source_hash"] != sources["source_hash"]


def test_a_draft_discovery_never_replaces_the_approved_source_and_a_new_approval_needs_a_new_confirmation(client: TestClient) -> None:
    opportunity_id = create(client)
    base = master_v1_and_meeting(client, opportunity_id)
    transcript_id = upload(client, opportunity_id)
    analyse(client, opportunity_id, transcript_id)
    ready = ok(confirm(client, opportunity_id, review(client, opportunity_id)))
    assert ready["readiness"]["ready_for_v2"]

    edited = client.patch(
        f"/opportunities/{opportunity_id}/discovery-paper",
        headers=headers(),
        json={"edits": [{"target": "thesis", "value": {"text": "Working hypothesis: a draft that was never approved"}}]},
    )
    assert edited.status_code == 200, edited.text
    drafted = review(client, opportunity_id)
    assert drafted["approved_discovery"]["version_id"] == base["approved"]["id"]
    assert drafted["v2_sources"] == ready["v2_sources"], "a draft changes nothing"

    second = ok(client.post(f"/opportunities/{opportunity_id}/discovery-paper/approve", headers=headers()))
    assert second["id"] != base["approved"]["id"]
    changed = review(client, opportunity_id)
    assert changed["approved_discovery"]["version_id"] == second["id"]
    assert (changed["confirmation"]["status"], changed["confirmation"]["stale_reasons"]) == ("stale", ["APPROVED_DISCOVERY_CHANGED"])
    assert changed["readiness"]["blockers"] == ["MEETING_REVIEW_STALE"] and changed["v2_sources"] is None
    assert changed["extraction"]["status"] == "current", "the meeting analysis itself is still valid"
    renewed = ok(confirm(client, opportunity_id, changed))
    assert renewed["v2_sources"]["approved_discovery_version_id"] == second["id"]
    assert renewed["v2_sources"]["presentation_id"] == base["presentation_id"]


def test_selected_use_cases_are_a_separate_source_and_changing_them_needs_a_new_confirmation(client: TestClient) -> None:
    opportunity_id = create(client)
    transcript_id = upload(client, opportunity_id)
    analyse(client, opportunity_id, transcript_id)
    available = ok(client.get(f"/opportunities/{opportunity_id}/available-use-cases", headers=headers()))["use_cases"]
    assert available, "the bundled corpus offers approved reference use cases"
    fact_id = available[0]["fact_id"]
    confirmed = ok(confirm(client, opportunity_id, review(client, opportunity_id)))
    assert confirmed["confirmation"]["sources"]["selected_use_case_ids"] == []

    ok(client.put(f"/opportunities/{opportunity_id}/selected-use-cases", headers=headers(), json={"use_case_ids": [fact_id]}))
    view = review(client, opportunity_id)
    assert view["selected_use_cases"]["use_case_ids"] == [fact_id]
    assert view["selected_use_cases"]["use_cases"][0]["status"] == "resolved"
    assert view["confirmation"]["stale_reasons"] == ["USE_CASES_CHANGED"]
    assert view["extraction"]["status"] == "current"
    assert fact_id not in json.dumps(view["extraction"]), "use cases are not mixed into the meeting findings"
    renewed = ok(confirm(client, opportunity_id, view))
    assert renewed["confirmation"]["sources"]["selected_use_case_ids"] == [fact_id]


def test_a_source_changed_between_loading_the_review_and_confirming_is_refused(client: TestClient) -> None:
    """The owner confirms what they saw. Anything that changed after the review was loaded is a 409."""
    opportunity_id = create(client)
    base = master_v1_and_meeting(client, opportunity_id)
    transcript_id = upload(client, opportunity_id)
    save_notes(client, opportunity_id, NOTES)
    analyse(client, opportunity_id, transcript_id)
    fact_id = ok(client.get(f"/opportunities/{opportunity_id}/available-use-cases", headers=headers()))["use_cases"][0]["fact_id"]
    store = get_memory_store()

    def refused_then_accepted(change, expected_difference: str) -> dict:
        loaded = review(client, opportunity_id)
        assert loaded["extraction"]["status"] == "current", loaded["extraction"]
        before = copy.deepcopy(store.opportunities[UUID(opportunity_id)].get("meeting_review"))
        change()
        refused = confirm(client, opportunity_id, loaded)
        assert refused.status_code == 409 and "MEETING_REVIEW_STALE" in refused.text, refused.text
        assert store.opportunities[UUID(opportunity_id)].get("meeting_review") == before, "a refused confirmation stores nothing"
        refreshed = review(client, opportunity_id)
        assert refreshed["review_fingerprint"] != loaded["review_fingerprint"], expected_difference
        return refreshed

    # 1. The selected Borek use cases change: the analysis is untouched, the request would have matched before.
    refreshed = refused_then_accepted(
        lambda: ok(client.put(f"/opportunities/{opportunity_id}/selected-use-cases", headers=headers(), json={"use_case_ids": [fact_id]})),
        "use cases",
    )
    assert refreshed["extraction"]["generated_at"] == review(client, opportunity_id)["extraction"]["generated_at"]
    confirmed = ok(confirm(client, opportunity_id, refreshed))
    assert confirmed["confirmation"]["sources"]["selected_use_case_ids"] == [fact_id]
    assert confirmed["readiness"]["ready_for_v2"]

    # 2. A different Discovery version is approved.
    def approve_new_discovery() -> None:
        edited = client.patch(
            f"/opportunities/{opportunity_id}/discovery-paper",
            headers=headers(),
            json={"edits": [{"target": "thesis", "value": {"text": "Working hypothesis: approved while the review was open"}}]},
        )
        assert edited.status_code == 200, edited.text
        ok(client.post(f"/opportunities/{opportunity_id}/discovery-paper/approve", headers=headers()))

    refreshed = refused_then_accepted(approve_new_discovery, "approved Discovery")
    assert refreshed["approved_discovery"]["version_id"] != base["approved"]["id"]
    assert refreshed["confirmation"]["stale_reasons"] == ["APPROVED_DISCOVERY_CHANGED"], "the earlier confirmation is shown as stale"
    confirmed = ok(confirm(client, opportunity_id, refreshed))
    assert confirmed["confirmation"]["sources"]["approved_discovery_version_id"] == refreshed["approved_discovery"]["version_id"]

    # 3. The notes change (the analysis becomes stale as well; the guard does not depend on that).
    refreshed = refused_then_accepted(lambda: save_notes(client, opportunity_id, NOTES + "\nDecision: Owner sponsors the pilot."), "notes")
    assert refreshed["extraction"]["stale_reasons"] == ["NOTES_CHANGED"]
    assert confirm(client, opportunity_id, refreshed).status_code == 409, "a stale analysis still cannot be confirmed"
    analyse(client, opportunity_id, transcript_id)
    ok(confirm(client, opportunity_id, review(client, opportunity_id)))

    # 4. The meeting is analysed again: same transcript, same notes, new extraction.
    refreshed = refused_then_accepted(lambda: analyse(client, opportunity_id, transcript_id), "extraction")
    ok(confirm(client, opportunity_id, refreshed))

    # 5. The transcript content changes.
    def change_transcript() -> None:
        store.transcripts[UUID(transcript_id)]["sections"][0]["content"] = "Requirement: Quotes must go out within two days."

    refreshed = refused_then_accepted(change_transcript, "transcript")
    assert refreshed["extraction"]["stale_reasons"] == ["TRANSCRIPT_CHANGED"]
    analyse(client, opportunity_id, transcript_id)
    ok(confirm(client, opportunity_id, review(client, opportunity_id)))

    # 6. Another transcript is uploaded while the review is open: the owner has not seen it.
    refreshed = refused_then_accepted(lambda: upload(client, opportunity_id, "Dana: Requirement: A later call.\n", name="second.txt"), "transcripts")
    final = ok(confirm(client, opportunity_id, refreshed))
    assert final["readiness"]["ready_for_v2"] and final["v2_sources"]["presentation_id"] == base["presentation_id"]


def test_the_fingerprint_is_stable_for_unchanged_sources_and_independent_of_the_confirmation(client: TestClient) -> None:
    opportunity_id = create(client)
    transcript_id = upload(client, opportunity_id)
    analyse(client, opportunity_id, transcript_id)
    first = review(client, opportunity_id)
    assert len(first["review_fingerprint"]) == 64
    assert review(client, opportunity_id)["review_fingerprint"] == first["review_fingerprint"], "reading twice gives the same value"
    confirmed = ok(confirm(client, opportunity_id, first, {"follow_ups": ["Send the pricing export by Friday."]}))
    assert confirmed["review_fingerprint"] == first["review_fingerprint"], "confirming changes no source"
    # So a changed selection can be confirmed again without reloading, and a repeated request is harmless.
    again = ok(confirm(client, opportunity_id, confirmed))
    assert (again["confirmation"]["status"], again["confirmation"]["excluded_count"]) == ("current", 0)
    # Owner review and finalization are untouched by any of this.
    workflow = ok(client.get(f"/opportunities/{opportunity_id}/workflow-status", headers=headers()))
    states = {step["key"]: step["state"] for step in workflow["steps"]}
    assert states["owner_review"] == "pending" and states["finalized"] == "pending" and workflow["finalization"] is None


def test_post_meeting_review_does_not_change_existing_contracts(client: TestClient) -> None:
    opportunity_id = create(client)
    transcript_id = upload(client, opportunity_id)
    save_notes(client, opportunity_id, NOTES)
    analyse(client, opportunity_id, transcript_id)
    before = {
        path: ok(client.get(f"/opportunities/{opportunity_id}/{path}", headers=headers()))
        for path in ("meeting-extraction", "personal-notes", "selected-use-cases", "workflow-status")
    }
    context_before = ok(client.get(f"/opportunities/{opportunity_id}/ppt2-context", headers=headers()))
    ok(confirm(client, opportunity_id, review(client, opportunity_id)))
    for path, body in before.items():
        assert ok(client.get(f"/opportunities/{opportunity_id}/{path}", headers=headers())) == body, path
    context_after = ok(client.get(f"/opportunities/{opportunity_id}/ppt2-context", headers=headers()))
    assert {key: value for key, value in context_after.items() if key != "assembled_at"} == {
        key: value for key, value in context_before.items() if key != "assembled_at"
    }
    assert context_after["sources"]["meeting_extraction"]["extraction"]["item_sources"]["priorities"] == ["both"]
    migration = Path(__file__).resolve().parents[3] / "apps/services/api/supabase/migrations/041_post_meeting_review.sql"
    sql = migration.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS meeting_review JSONB" in sql and "CREATE TABLE" not in sql and "POLICY" not in sql.upper().replace("NO NEW POLICY", "")
