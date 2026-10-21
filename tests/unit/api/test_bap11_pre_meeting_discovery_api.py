"""BAP-11: the Pre-Meeting Discovery Document stays available after the client moves to post-meeting.

GET /opportunities/{id}/pre-meeting-discovery resolves - read-only - the approved Discovery version
Presentation V1 was built from, and separately the approved Post-Meeting Discovery (Discovery 2).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.data.memory_store import get_memory_store
from tests.unit.api.test_master_presentation_v2_api import (  # noqa: F401  (client is a fixture)
    CLIENT,
    OTHER,
    client,
    confirm,
    generate_v2,
    get,
    headers,
    ok,
    post,
    prepared,
    stored_versions,
)


def documents(client: TestClient, opportunity_id: str, user=None) -> dict:
    return ok(get(client, f"/opportunities/{opportunity_id}/pre-meeting-discovery", **({"user": user} if user else {})))


def counts() -> tuple[int, int, int, int]:
    store = get_memory_store()
    return (len(store.discovery_paper_versions), len(store.presentation_versions), len(store.post_meeting_discovery_versions), len(store.generation_jobs))


def test_the_original_document_is_resolved_through_the_whole_post_meeting_journey(client: TestClient) -> None:
    base = prepared(client)
    opportunity_id, presentation_id, original = base["opportunity"], base["presentation"], base["approved"]
    approved_paper = ok(get(client, f"/opportunities/{opportunity_id}/discovery-paper/versions/{original}"))["paper_json"]
    v1 = stored_versions(presentation_id)[0]

    # After the meeting, before Discovery 2: the pre-meeting document, no post-meeting one.
    first = documents(client, opportunity_id)
    pre = first["pre_meeting"]
    assert (pre["state"], pre["label"], pre["resolution"]) == ("available", "Pre-Meeting Discovery Document", "presentation_v1")
    assert pre["version_id"] == original and pre["presentation_version_id"] == str(v1["id"])
    assert (pre["format"], pre["docx_available"], pre["version_number"]) == ("analysis", True, 1) and pre["approved_at"]
    assert first["post_meeting"] == {
        "state": "unavailable", "label": "Post-Meeting Discovery Document", "reason": "NO_APPROVED_DISCOVERY2",
        "message": "No approved Post-Meeting Discovery Document exists yet.",
    }

    # Discovery 2 approved, V2 generated, reviewed and finalized: two separate documents.
    confirm(client, opportunity_id)
    v2 = ok(generate_v2(client, opportunity_id))
    after_v2 = documents(client, opportunity_id)
    assert after_v2["pre_meeting"]["version_id"] == original and after_v2["pre_meeting"]["resolution"] == "presentation_v2_base"
    post_meeting = after_v2["post_meeting"]
    assert (post_meeting["state"], post_meeting["label"]) == ("available", "Post-Meeting Discovery Document")
    assert post_meeting["version_id"] != original
    assert post_meeting["docx_download_path"] == f"/opportunities/{opportunity_id}/post-meeting-discovery/versions/{post_meeting['version_id']}/download/docx"
    ok(post(client, f"/opportunities/{opportunity_id}/workflow/owner-reviewed"))
    ok(post(client, f"/opportunities/{opportunity_id}/workflow/finalize"))
    assert documents(client, opportunity_id)["pre_meeting"]["version_id"] == original

    # A later approval and a new pre-meeting revision do not replace the original document.
    edited = client.patch(
        f"/opportunities/{opportunity_id}/discovery-paper", headers=headers(),
        json={"edits": [{"target": "thesis", "value": {"text": "Working hypothesis: approved after the meeting"}}]},
    )
    assert edited.status_code == 200, edited.text
    later = ok(post(client, f"/opportunities/{opportunity_id}/discovery-paper/approve"))
    assert later["id"] != original
    ok(post(client, f"/opportunities/{opportunity_id}/stage1-outputs/presentation/regenerate"))
    final = documents(client, opportunity_id)
    assert final["pre_meeting"]["version_id"] == original, "the V1 the post-meeting deck was built on decides"
    assert final["pre_meeting"]["presentation_version_id"] == str(v1["id"]), "anchored on the V1 that V2 was built on"
    assert v2["presentation_version_id"] != final["pre_meeting"]["presentation_version_id"]

    # The saved document reads back unchanged and downloads as Word - nothing is generated.
    before = counts()
    saved = ok(get(client, final["pre_meeting"]["version_path"]))
    assert saved["paper_json"] == approved_paper and saved["status"] == "approved"
    docx = get(client, final["pre_meeting"]["docx_download_path"])
    assert docx.status_code == 200 and docx.content.startswith(b"PK")
    assert "wordprocessingml" in docx.headers["content-type"]
    two = get(client, final["post_meeting"]["docx_download_path"])
    assert two.status_code == 200 and two.content.startswith(b"PK")
    documents(client, opportunity_id)
    assert counts() == before, "reading and downloading never generate, version or approve anything"


def test_a_later_approval_before_v2_does_not_replace_the_presented_version(client: TestClient) -> None:
    base = prepared(client)
    opportunity_id = base["opportunity"]
    client.patch(
        f"/opportunities/{opportunity_id}/discovery-paper", headers=headers(),
        json={"edits": [{"target": "thesis", "value": {"text": "Working hypothesis: changed after the meeting"}}]},
    )
    later = ok(post(client, f"/opportunities/{opportunity_id}/discovery-paper/approve"))
    workflow = ok(get(client, f"/opportunities/{opportunity_id}/workflow-status"))
    assert workflow["documents"]["approved_discovery"]["version_id"] == later["id"], "the latest approval is newer"
    assert documents(client, opportunity_id)["pre_meeting"]["version_id"] == base["approved"], "but the V1 reference decides"


def _v1_row(presentation_id: str) -> dict:
    return get_memory_store().presentation_versions[stored_versions(presentation_id)[0]["id"]]


def test_legacy_ppt1_and_the_earlier_discovery_format(client: TestClient) -> None:
    base = prepared(client)
    opportunity_id, original = base["opportunity"], base["approved"]
    store = get_memory_store()
    # An earlier PPT #1 records its Discovery the same way; an earlier Discovery has seven pages.
    _v1_row(base["presentation"])["generation_source_manifest"] = {"schema_version": "1.0", "kind": "ppt1", "approved_discovery_version_id": original}
    row = store.discovery_paper_versions[UUID(original)]
    row["paper_json"] = {**{key: value for key, value in row["paper_json"].items() if key != "analysis"}, "schema_version": "1.0", "pages": []}
    pre = documents(client, opportunity_id)["pre_meeting"]
    assert (pre["state"], pre["version_id"], pre["resolution"]) == ("available", original, "presentation_v1")
    assert (pre["format"], pre["docx_available"], pre["docx_download_path"]) == ("legacy", False, None), "Word is not advertised"
    assert get(client, f"/opportunities/{opportunity_id}/discovery-paper/versions/{original}/download/docx").status_code == 409


def test_without_a_v1_reference_only_an_unambiguous_version_is_used(client: TestClient) -> None:
    base = prepared(client)
    opportunity_id, original = base["opportunity"], base["approved"]
    v1 = _v1_row(base["presentation"])
    v1["generation_source_manifest"] = {key: value for key, value in v1["generation_source_manifest"].items() if key != "approved_discovery_version_id"}
    only = documents(client, opportunity_id)["pre_meeting"]
    assert (only["state"], only["version_id"], only["resolution"]) == ("available", original, "only_approved_version")
    client.patch(
        f"/opportunities/{opportunity_id}/discovery-paper", headers=headers(),
        json={"edits": [{"target": "thesis", "value": {"text": "Working hypothesis: a second approved version"}}]},
    )
    ok(post(client, f"/opportunities/{opportunity_id}/discovery-paper/approve"))
    ambiguous = documents(client, opportunity_id)["pre_meeting"]
    assert (ambiguous["state"], ambiguous["reason"]) == ("unavailable", "DISCOVERY_ORIGIN_AMBIGUOUS"), "never the latest by default"


def test_broken_references_are_reported_not_substituted(client: TestClient) -> None:
    base = prepared(client)
    other = prepared(client, body={**CLIENT, "client_name": "Nordlicht Logistik GmbH"})
    v1 = _v1_row(base["presentation"])
    manifest = copy.deepcopy(v1["generation_source_manifest"])
    for reference, reason in (
        (str(uuid4()), "DISCOVERY_VERSION_MISSING"),
        (other["approved"], "DISCOVERY_VERSION_MISMATCH"),
    ):
        v1["generation_source_manifest"] = {**manifest, "approved_discovery_version_id": reference}
        pre = documents(client, base["opportunity"])["pre_meeting"]
        assert (pre["state"], pre["reason"]) == ("unavailable", reason)
        assert other["approved"] not in str(pre), "another client's document is never offered"


def test_no_approved_discovery_yet(client: TestClient) -> None:
    opportunity_id = ok(client.post("/opportunities", headers=headers(), json=CLIENT), 201)["id"]
    result = documents(client, opportunity_id)
    assert (result["pre_meeting"]["state"], result["pre_meeting"]["reason"]) == ("unavailable", "NO_APPROVED_DISCOVERY")
    assert result["post_meeting"]["state"] == "unavailable"


def test_only_the_owner_reads_the_documents_and_a_new_session_sees_the_same(client: TestClient) -> None:
    base = prepared(client)
    path = f"/opportunities/{base['opportunity']}/pre-meeting-discovery"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer not-a-token"}).status_code == 401
    assert get(client, path, OTHER).status_code == 404, "another user's client"
    assert get(client, f"/opportunities/{uuid4()}/pre-meeting-discovery").status_code == 404
    first = documents(client, base["opportunity"])
    # Another user cannot read the version or download it either.
    assert get(client, first["pre_meeting"]["version_path"], OTHER).status_code == 404
    assert get(client, first["pre_meeting"]["docx_download_path"], OTHER).status_code == 404
    with TestClient(create_app()) as again:  # refresh / log in again: server-side state only
        assert again.get(path, headers=headers()).json() == first


# ------------------------------------------------------------------ PR #11 review: V1 rebuilt after the meeting


def _approve_later_discovery(client: TestClient, opportunity_id: str) -> str:
    edited = client.patch(
        f"/opportunities/{opportunity_id}/discovery-paper", headers=headers(),
        json={"edits": [{"target": "thesis", "value": {"text": "Working hypothesis: approved after the first meeting"}}]},
    )
    assert edited.status_code == 200, edited.text
    return ok(post(client, f"/opportunities/{opportunity_id}/discovery-paper/approve"))["id"]


def test_presentation_v1_rebuilt_after_the_meeting_keeps_the_discovery_used_in_the_meeting(client: TestClient) -> None:
    # 1-3: Discovery A approved, Presentation V1-A ready, first meeting completed (inside prepared()).
    base = prepared(client)
    opportunity_id, presentation_id, discovery_a = base["opportunity"], base["presentation"], base["approved"]
    v1_a = stored_versions(presentation_id)[0]
    # 4-5: the workflow allows approving Discovery B and rebuilding V1 after the meeting.
    discovery_b = _approve_later_discovery(client, opportunity_id)
    ok(post(client, f"/opportunities/{opportunity_id}/stage1-outputs/presentation/regenerate"))
    rebuilt = [row for row in stored_versions(presentation_id) if row["id"] != v1_a["id"]]
    assert rebuilt and rebuilt[-1]["status"] == "ready", "V1 was rebuilt after the meeting"
    assert rebuilt[-1]["generation_source_manifest"]["approved_discovery_version_id"] == discovery_b
    # 6: the Pre-Meeting Discovery Document is still Discovery A, anchored on V1-A.
    pre = documents(client, opportunity_id)["pre_meeting"]
    assert pre["state"] == "available", pre
    assert pre["version_id"] == discovery_a, "the Discovery used in the first meeting, not the later approval"
    assert pre["presentation_version_id"] == str(v1_a["id"])
    assert pre["resolution"] == "first_meeting_presentation_v1"

    # V2 and the final package then build on the rebuilt V1; the meeting's V1 still decides.
    confirm(client, opportunity_id)
    v2 = ok(generate_v2(client, opportunity_id))
    v2_row = get_memory_store().presentation_versions[UUID(v2["presentation_version_id"])]
    assert v2_row["generation_source_manifest"]["base_presentation_version_id"] == str(rebuilt[-1]["id"])
    ok(post(client, f"/opportunities/{opportunity_id}/workflow/owner-reviewed"))
    ok(post(client, f"/opportunities/{opportunity_id}/workflow/finalize"))
    final = documents(client, opportunity_id)["pre_meeting"]
    assert (final["version_id"], final["presentation_version_id"]) == (discovery_a, str(v1_a["id"]))
    before = counts()
    assert get(client, final["docx_download_path"]).content.startswith(b"PK")
    assert counts() == before, "still read-only"


def _forget_meeting_audit(opportunity_id: str) -> None:
    store = get_memory_store()
    for key, row in list(store.audit_logs.items()):
        if row["action"] == "workflow.first_meeting_completed" and str(row["object_id"]) == opportunity_id:
            del store.audit_logs[key]


def test_without_the_meeting_record_the_meeting_time_decides(client: TestClient) -> None:
    base = prepared(client)
    opportunity_id, presentation_id, discovery_a = base["opportunity"], base["presentation"], base["approved"]
    v1_a = stored_versions(presentation_id)[0]
    _approve_later_discovery(client, opportunity_id)
    ok(post(client, f"/opportunities/{opportunity_id}/stage1-outputs/presentation/regenerate"))
    _forget_meeting_audit(opportunity_id)  # audit writes are best-effort
    pre = documents(client, opportunity_id)["pre_meeting"]
    assert (pre["version_id"], pre["presentation_version_id"], pre["resolution"]) == (discovery_a, str(v1_a["id"]), "first_meeting_presentation_v1")
    # The same holds for the earlier PPT #1 records.
    for row in stored_versions(presentation_id):
        get_memory_store().presentation_versions[row["id"]]["generation_source_manifest"]["kind"] = "ppt1"
    assert documents(client, opportunity_id)["pre_meeting"]["version_id"] == discovery_a


def test_an_unclear_meeting_version_is_reported_not_guessed(client: TestClient) -> None:
    base = prepared(client)
    opportunity_id, presentation_id = base["opportunity"], base["presentation"]
    store = get_memory_store()
    v1_a = store.presentation_versions[stored_versions(presentation_id)[0]["id"]]
    discovery_b = _approve_later_discovery(client, opportunity_id)
    ok(post(client, f"/opportunities/{opportunity_id}/stage1-outputs/presentation/regenerate"))
    _forget_meeting_audit(opportunity_id)
    # Two V1 versions from different Discovery versions before the meeting, one rebuilt after it.
    earlier = copy.deepcopy(v1_a)
    earlier.update(id=uuid4(), version_number=0, generation_source_manifest={**v1_a["generation_source_manifest"], "approved_discovery_version_id": discovery_b})
    store.presentation_versions[earlier["id"]] = earlier
    pre = documents(client, opportunity_id)["pre_meeting"]
    assert (pre["state"], pre["reason"]) == ("unavailable", "FIRST_MEETING_ORIGIN_AMBIGUOUS"), pre
    assert pre["label"] == "Pre-Meeting Discovery Document"


def test_a_rebuild_from_the_same_discovery_changes_nothing(client: TestClient) -> None:
    base = prepared(client)
    opportunity_id, presentation_id, discovery_a = base["opportunity"], base["presentation"], base["approved"]
    store = get_memory_store()
    v1_a = store.presentation_versions[stored_versions(presentation_id)[0]["id"]]
    # The API reuses the version for an unchanged Discovery; a re-render after the meeting is stored directly.
    ok(post(client, f"/opportunities/{opportunity_id}/stage1-outputs/presentation/regenerate"))
    assert len(stored_versions(presentation_id)) == 1
    rerender = copy.deepcopy(v1_a)
    rerender.update(id=uuid4(), version_number=2, created_at=datetime.now(UTC) + timedelta(minutes=5))
    store.presentation_versions[rerender["id"]] = rerender
    _forget_meeting_audit(opportunity_id)
    pre = documents(client, opportunity_id)["pre_meeting"]
    assert (pre["state"], pre["version_id"]) == ("available", discovery_a)
    assert pre["presentation_version_id"] == str(v1_a["id"]), "the V1 ready at the meeting"
