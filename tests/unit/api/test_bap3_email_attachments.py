"""BAP-3: the follow-up email offers the four generated documents and files the owner uploads.

Uploaded files are validated on their bytes, stored per opportunity under a stable id, selected
and removed under the draft's revision and confirmation rules, and exported as real files.
"""

from __future__ import annotations

import copy
import email
import email.policy
import hashlib
import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from docx import Document
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.services.data.memory_store import get_memory_store
from app.services.data.supabase_store import SupabaseDataStore
from services.followup.attachments import (
    MAX_EMAIL_BYTES,
    MAX_EXTERNAL_ATTACHMENT_BYTES,
    MAX_EXTERNAL_ATTACHMENTS,
    attachments_fit,
    encoded_size,
    safe_file_name,
    unique_names,
)
from tests.unit.api.test_followup_email import confirm_email, export, finalized, generate, load, patch, root
from tests.unit.api.test_master_presentation_v2_api import OTHER, OWNER, client, get, headers, ok  # noqa: F401  (client is a fixture)

GENERATED = ["pptx", "pdf", "v1_pptx", "v1_pdf", "discovery1_docx", "discovery2_docx"]
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def pdf(text: str = "hello") -> bytes:
    return b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n% " + text.encode() + b"\ntrailer << >>\n%%EOF\n"


def docx(text: str = "Agenda") -> bytes:
    document = Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def office(main_part: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(main_part, "<x/>")
    return buffer.getvalue()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def upload(client: TestClient, opportunity_id: str, draft: dict, name: str, content: bytes, mime: str | None = "application/pdf", user: UUID = OWNER, revision: int | None = None):
    file = (name, io.BytesIO(content), mime) if mime is not None else (name, io.BytesIO(content))
    return client.post(
        f"{root(opportunity_id)}/{draft['id']}/attachments",
        headers=headers(user),
        files={"file": file},
        data={"expected_revision": str(revision if revision is not None else draft["revision"])},
    )


def remove(client: TestClient, opportunity_id: str, draft: dict, attachment_id: str, user: UUID = OWNER, revision: int | None = None):
    return client.delete(
        f"{root(opportunity_id)}/{draft['id']}/attachments/{attachment_id}?expected_revision={revision if revision is not None else draft['revision']}",
        headers=headers(user),
    )


def uploaded(draft: dict) -> list[dict]:
    return [item for item in draft["attachments"] if item["origin"] == "external"]


def drafted(client: TestClient, user: UUID = OWNER) -> tuple[str, dict]:
    base = finalized(client, user)
    return base["opportunity"], ok(generate(client, base["opportunity"], user))["draft"]


# ------------------------------------------------------------------ generated documents


def test_the_four_generated_documents_stay_first_and_pinned(client: TestClient) -> None:
    opportunity_id, draft = drafted(client)
    generated = [item for item in draft["attachments"] if item["origin"] == "generated"]
    assert [item["key"] for item in generated] == GENERATED
    assert {item["artifact"] for item in generated} == {"master_presentation_v2", "master_presentation_v1", "discovery_1", "discovery_2"}
    assert all(item["available"] and item["download_path"] and item["uploaded_at"] is None for item in generated)
    assert uploaded(draft) == []
    with_file = ok(upload(client, opportunity_id, draft, "agenda.pdf", pdf()))["draft"]
    assert [item["key"] for item in with_file["attachments"]][:6] == GENERATED, "uploads come after the generated documents"
    assert [item["selected"] for item in with_file["attachments"]][:6] == [item["selected"] for item in generated], "generated selections are kept"


# ------------------------------------------------------------------ upload, list, download, remove


def test_upload_list_download_and_remove(client: TestClient) -> None:
    opportunity_id, draft = drafted(client)
    body = draft["lengths"]["medium"]["body"]
    content = pdf("pricing")
    saved = ok(upload(client, opportunity_id, draft, "Pricing sheet.pdf", content))["draft"]
    (item,) = uploaded(saved)
    assert item["key"] == f"external:{item['artifact_version_id']}" and item["artifact"] == "external"
    assert (item["file_name"], item["format"], item["size_bytes"], item["selected"], item["available"], item["product_version"]) == (
        "Pricing sheet.pdf", "pdf", len(content), True, True, "Uploaded",
    ), "an uploaded file is selected; the owner uploaded it to send it"
    assert item["uploaded_at"] and item["presentation_id"] is None
    assert saved["revision"] == draft["revision"] + 1 and saved["lengths"]["medium"]["body"] == body, "the email text is untouched"

    listed = ok(get(client, f"{root(opportunity_id)}/{draft['id']}/attachments"))["attachments"]
    assert [entry["key"] for entry in listed] == [*GENERATED, item["key"]]
    downloaded = get(client, item["download_path"])
    assert downloaded.status_code == 200 and downloaded.content == content
    assert downloaded.headers["content-type"] == "application/pdf" and downloaded.headers["x-content-type-options"] == "nosniff"

    reloaded = load(client, opportunity_id)
    assert uploaded(reloaded) == uploaded(saved), "files and their selection survive a reload"

    gone = ok(remove(client, opportunity_id, saved, item["artifact_version_id"]))["draft"]
    assert uploaded(gone) == [] and item["key"] not in (gone.get("attachment_selection") or {})
    assert get(client, item["download_path"]).status_code == 404
    assert get_memory_store().email_attachments == {}, "the stored bytes are removed with the attachment"
    actions = [entry["action"] for entry in get_memory_store().audit_logs.values()]
    assert actions.count("email_attachment.upload") == 1 and actions.count("email_attachment.remove") == 1


def test_several_files_duplicate_names_and_exact_bytes_in_the_eml(client: TestClient) -> None:
    opportunity_id, draft = drafted(client)
    first, second, notes, sheet = pdf("first"), pdf("second"), "Talking points\nPilot with ten quotes\n".encode(), office("xl/workbook.xml")
    draft = ok(upload(client, opportunity_id, draft, "notes.pdf", first))["draft"]
    draft = ok(upload(client, opportunity_id, draft, "notes.pdf", second))["draft"]
    draft = ok(upload(client, opportunity_id, draft, "talking points.txt", notes, "text/plain"))["draft"]
    draft = ok(upload(client, opportunity_id, draft, "volumes.xlsx", sheet, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))["draft"]
    files = uploaded(draft)
    assert [item["file_name"] for item in files] == ["notes.pdf", "notes.pdf", "talking points.txt", "volumes.xlsx"]
    assert len({item["key"] for item in files}) == 4, "the identity is the id, not the name"
    # The talking points stay out; the Discovery 2 document goes in.
    choice = {files[2]["key"]: False, "discovery2_docx": True}
    draft = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": draft["revision"], "attachments": choice}))["draft"]
    assert load(client, opportunity_id)["attachments"] == draft["attachments"], "the selection survives a reload"
    done = ok(confirm_email(client, opportunity_id, draft))["draft"]
    response = export(client, opportunity_id, done)
    assert response.status_code == 200, response.text
    message = email.message_from_bytes(response.content, policy=email.policy.default)
    parts = {part.get_filename(): part for part in message.iter_attachments()}
    assert "notes.pdf" in parts and "notes (2).pdf" in parts, "two files of the same name stay two files"
    assert parts["notes.pdf"].get_content() == first and parts["notes (2).pdf"].get_content() == second
    assert parts["volumes.xlsx"].get_content() == sheet
    assert parts["volumes.xlsx"].get_content_type() == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "talking points.txt" not in parts, "an unselected upload is not attached"
    assert any(name.endswith("Discovery 2 - v1.docx") or "Discovery 2" in name for name in parts)
    assert load(client, opportunity_id)["send_status"] == "not_sent", "exporting sends nothing"


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "name,content,mime,code",
    [
        ("setup.exe", b"MZ\x90\x00", "application/octet-stream", "EMAIL_ATTACHMENT_UNSUPPORTED_TYPE"),
        ("archive.zip", b"PK\x03\x04", "application/zip", "EMAIL_ATTACHMENT_UNSUPPORTED_TYPE"),
        ("noextension", b"%PDF-1.4\n%%EOF", "application/pdf", "EMAIL_ATTACHMENT_UNSUPPORTED_TYPE"),
        ("agenda.pdf", pdf(), "image/png", "EMAIL_ATTACHMENT_UNSUPPORTED_TYPE"),
        ("empty.pdf", b"", "application/pdf", "EMAIL_ATTACHMENT_EMPTY"),
        ("fake.pdf", b"MZ this is a program", "application/pdf", "EMAIL_ATTACHMENT_CORRUPT"),
        ("fake.docx", office("xl/workbook.xml"), DOCX_MIME, "EMAIL_ATTACHMENT_CORRUPT"),
        ("broken.docx", b"PK\x03\x04 broken", DOCX_MIME, "EMAIL_ATTACHMENT_CORRUPT"),
        ("photo.png", b"\xff\xd8\xff not a png", "image/png", "EMAIL_ATTACHMENT_CORRUPT"),
        ("binary.txt", b"\x00\x01\x02 binary", "text/plain", "EMAIL_ATTACHMENT_CORRUPT"),
        ("big.pdf", b"%PDF-1.4\n" + b"0" * MAX_EXTERNAL_ATTACHMENT_BYTES + b"\n%%EOF", "application/pdf", "EMAIL_ATTACHMENT_TOO_LARGE"),
    ],
    ids=["exe", "zip", "no-extension", "mime-mismatch", "empty", "renamed-binary", "excel-as-word", "broken-zip", "jpeg-as-png", "binary-text", "too-large"],
)
def test_invalid_files_are_refused_and_nothing_is_stored(client: TestClient, name: str, content: bytes, mime: str, code: str) -> None:
    opportunity_id, draft = drafted(client)
    response = upload(client, opportunity_id, draft, name, content, mime)
    assert response.status_code == 400 and response.json()["error"]["code"] == code, response.text
    assert get_memory_store().email_attachments == {}
    assert load(client, opportunity_id)["revision"] == draft["revision"], "a refused upload changes nothing"


def test_accepted_types_names_and_size_boundaries(client: TestClient) -> None:
    opportunity_id, draft = drafted(client)
    for name, content, mime in [
        ("letter.docx", docx(), DOCX_MIME),
        ("deck.pptx", office("ppt/presentation.xml"), None),
        ("chart.png", PNG, "image/png"),
        ("photo.JPG", b"\xff\xd8\xff\xe0 jpeg", "image/jpeg"),
        ("figures.csv", b"month;quotes\nJan;12\n", "text/csv"),
    ]:
        draft = ok(upload(client, opportunity_id, draft, name, content, mime))["draft"]
    assert [item["format"] for item in uploaded(draft)] == ["docx", "pptx", "png", "jpg", "csv"]
    # A path in the name is reduced to the name; the file is stored under its id.
    draft = ok(upload(client, opportunity_id, draft, "..\\..\\secret/../etc/passwd.txt", b"plain text", "text/plain"))["draft"]
    item = uploaded(draft)[-1]
    assert item["file_name"] == "passwd.txt"
    stored = get_memory_store().email_attachments[UUID(item["artifact_version_id"])]
    assert stored["storage_path"] == f"{opportunity_id}/{item['artifact_version_id']}.txt"
    # Exactly the limit is accepted (text: a plain-text file of exactly 10 MB).
    exact = b"a" * MAX_EXTERNAL_ATTACHMENT_BYTES
    for item in uploaded(draft):
        draft = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": draft["revision"], "attachments": {item["key"]: False}}))["draft"]
    draft = ok(upload(client, opportunity_id, draft, "exact.txt", exact, "text/plain"))["draft"]
    assert uploaded(draft)[-1]["size_bytes"] == MAX_EXTERNAL_ATTACHMENT_BYTES


def test_safe_names_and_unique_names() -> None:
    assert safe_file_name("../../etc/passwd") == "passwd"
    assert safe_file_name("C:\\Users\\a\\Report Q3.PDF") == "Report Q3.pdf"
    assert safe_file_name('quote "final"; v2<1>.pdf') == "quote _final__ v2_1.pdf"
    assert safe_file_name("\x00\x1f.pdf") == "attachment.pdf"
    assert len(safe_file_name("x" * 400 + ".pdf")) == 120
    assert unique_names(["a.pdf", "A.pdf", "a.pdf", "b"]) == ["a.pdf", "A (2).pdf", "a (3).pdf", "b"]


def test_the_25_mb_policy_applies_to_the_encoded_email_and_the_count_is_limited(client: TestClient) -> None:
    opportunity_id, draft = drafted(client)
    big = b"b" * (6 * 1024 * 1024)
    selected = [item["size_bytes"] for item in draft["attachments"] if item["selected"]]
    # Raw bytes alone would allow four 6 MB files next to the generated PDF; encoded, they do not fit.
    fitting = 0
    while attachments_fit(selected + [len(big)] * (fitting + 1)):
        fitting += 1
    assert sum(selected) + len(big) * (fitting + 1) <= MAX_EMAIL_BYTES, "the refusal is caused by the encoding, not by the raw bytes"
    for index in range(fitting):
        draft = ok(upload(client, opportunity_id, draft, f"part {index}.txt", big, "text/plain"))["draft"]
    refused = upload(client, opportunity_id, draft, "one too many.txt", big, "text/plain")
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "EMAIL_ATTACHMENTS_TOO_LARGE", refused.text
    assert "once encoded" in refused.json()["error"]["message"]

    # The estimate is conservative: the exported email of the fullest selection stays within 25 MB.
    done = ok(confirm_email(client, opportunity_id, draft))["draft"]
    exported = export(client, opportunity_id, done)
    assert exported.status_code == 200 and len(exported.content) <= MAX_EMAIL_BYTES
    attached = sum(int(item["size_bytes"]) for item in done["attachments"] if item["selected"])
    assert len(exported.content) > attached * 4 // 3, "the email is larger than its raw attachments: Base64"
    assert sum(encoded_size(item["size_bytes"]) for item in done["attachments"] if item["selected"]) >= len(exported.content) - 200_000

    # Deselected files do not count; selecting again past the limit is refused and changes nothing.
    first = uploaded(done)[0]["key"]
    draft = ok(patch(client, opportunity_id, done["id"], {"expected_revision": done["revision"], "attachments": {first: False}}))["draft"]
    draft = ok(upload(client, opportunity_id, draft, "one too many.txt", big, "text/plain"))["draft"]
    again = patch(client, opportunity_id, draft["id"], {"expected_revision": draft["revision"], "attachments": {first: True}})
    assert again.status_code == 400 and again.json()["error"]["code"] == "EMAIL_ATTACHMENTS_TOO_LARGE"
    assert load(client, opportunity_id)["attachments"] == draft["attachments"]

    # At most MAX_EXTERNAL_ATTACHMENTS uploads per email.
    for item in uploaded(draft):
        draft = ok(remove(client, opportunity_id, draft, item["artifact_version_id"]))["draft"]
    for index in range(MAX_EXTERNAL_ATTACHMENTS):
        draft = ok(upload(client, opportunity_id, draft, f"file {index}.txt", b"text", "text/plain"))["draft"]
    eleventh = upload(client, opportunity_id, draft, "one more.txt", b"text", "text/plain")
    assert eleventh.status_code == 400 and eleventh.json()["error"]["code"] == "EMAIL_ATTACHMENT_LIMIT"


def test_uploads_survive_regeneration_and_stay_in_their_opportunity(client: TestClient) -> None:
    first, draft = drafted(client)
    second, other = drafted(client)
    draft = ok(upload(client, first, draft, "agenda.pdf", pdf("agenda")))["draft"]
    item = uploaded(draft)[0]
    regenerated = ok(generate(client, first, overwrite_edits=True))["draft"]
    assert regenerated["id"] == draft["id"] and uploaded(regenerated) == [item], "the upload and its selection stay through a regeneration"
    assert uploaded(load(client, second)) == [] and uploaded(other) == [], "another opportunity of the same owner does not see it"
    assert get(client, f"{root(second)}/{other['id']}/attachments/{item['artifact_version_id']}/download").status_code == 404
    foreign = patch(client, second, other["id"], {"expected_revision": other["revision"], "attachments": {item["key"]: True}})
    assert foreign.status_code == 400


def test_uploaded_files_are_never_used_to_write_the_email(client: TestClient) -> None:
    opportunity_id, draft = drafted(client)
    claim = "The client accepted a fifty percent discount on every quote."
    before = {name: draft["lengths"][name]["body"] for name in ("short", "medium", "extensive")}
    draft = ok(upload(client, opportunity_id, draft, "claims.txt", claim.encode(), "text/plain"))["draft"]
    regenerated = ok(generate(client, opportunity_id, overwrite_edits=True))["draft"]
    for name in ("short", "medium", "extensive"):
        assert "discount" not in regenerated["lengths"][name]["body"]
        assert regenerated["lengths"][name]["body"] == before[name], "the text comes from the finalized package only"


def test_a_failed_draft_save_or_removal_leaves_no_orphaned_or_hidden_file(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    opportunity_id, draft = drafted(client)
    store = get_memory_store()
    original_save = type(store).save_email_draft

    def conflicting(self, **kwargs):
        raise HTTPException(status_code=409, detail={"code": "EMAIL_DRAFT_CONFLICT", "message": "changed elsewhere"})

    monkeypatch.setattr(type(store), "save_email_draft", conflicting)
    refused = upload(client, opportunity_id, draft, "agenda.pdf", pdf())
    assert refused.status_code == 409 and store.email_attachments == {}, "the stored file is removed when the draft cannot name it"
    monkeypatch.setattr(type(store), "save_email_draft", original_save)

    draft = ok(upload(client, opportunity_id, draft, "agenda.pdf", pdf()))["draft"]
    item = uploaded(draft)[0]
    original_delete = type(store).delete_email_attachment

    def failing(self, **kwargs):
        raise HTTPException(status_code=503, detail={"code": "EMAIL_ATTACHMENT_DELETE_FAILED", "message": "storage down"})

    monkeypatch.setattr(type(store), "delete_email_attachment", failing)
    failed = remove(client, opportunity_id, draft, item["artifact_version_id"])
    assert failed.status_code == 503
    monkeypatch.setattr(type(store), "delete_email_attachment", original_delete)
    current = load(client, opportunity_id)
    (still,) = uploaded(current)
    assert still["selected"] is False, "the email no longer carries it"
    assert get(client, still["download_path"]).status_code == 200, "the file is still visible and reachable"
    ok(remove(client, opportunity_id, current, still["artifact_version_id"]))
    assert store.email_attachments == {}, "removing it again succeeds"



def test_uploads_need_the_current_revision_and_withdraw_a_confirmation(client: TestClient) -> None:
    opportunity_id, draft = drafted(client)
    stale = upload(client, opportunity_id, draft, "agenda.pdf", pdf(), revision=draft["revision"] + 5)
    assert stale.status_code == 409 and get_memory_store().email_attachments == {}, "a stale upload stores nothing"

    confirmed = ok(confirm_email(client, opportunity_id, draft))["draft"]
    assert confirmed["status"] == "confirmed"
    added = ok(upload(client, opportunity_id, confirmed, "agenda.pdf", pdf()))["draft"]
    assert added["status"] == "draft" and added["confirmed_revision"] is None, "a new file needs a new review"
    item = uploaded(added)[0]

    confirmed = ok(confirm_email(client, opportunity_id, added))["draft"]
    toggled = ok(patch(client, opportunity_id, confirmed["id"], {"expected_revision": confirmed["revision"], "attachments": {item["key"]: False}}))["draft"]
    assert toggled["status"] == "draft", "deselecting a file needs a new review"

    confirmed = ok(confirm_email(client, opportunity_id, toggled))["draft"]
    stale_remove = remove(client, opportunity_id, confirmed, item["artifact_version_id"], revision=confirmed["revision"] - 1)
    assert stale_remove.status_code == 409 and len(get_memory_store().email_attachments) == 1
    removed = ok(remove(client, opportunity_id, confirmed, item["artifact_version_id"]))["draft"]
    assert removed["status"] == "draft"
    unconfirmed_export = export(client, opportunity_id, removed)
    assert unconfirmed_export.status_code == 409 and "EMAIL_NOT_CONFIRMED" in unconfirmed_export.text


def test_a_missing_selected_file_blocks_confirmation_and_export(client: TestClient) -> None:
    opportunity_id, draft = drafted(client)
    draft = ok(upload(client, opportunity_id, draft, "agenda.pdf", pdf()))["draft"]
    confirmed = ok(confirm_email(client, opportunity_id, draft))["draft"]
    store = get_memory_store()
    (row,) = store.email_attachments.values()
    kept = row["content"]
    row["content"] = b"%PDF-1.4 tampered %%EOF"  # the stored bytes are no longer the upload
    try:
        blocked = export(client, opportunity_id, confirmed)
        assert blocked.status_code == 409 and "EMAIL_ATTACHMENT_UNAVAILABLE" in blocked.text
        reopened = ok(patch(client, opportunity_id, confirmed["id"], {"expected_revision": confirmed["revision"], "selected_length": "short"}))["draft"]
        refused = confirm_email(client, opportunity_id, reopened)
        assert refused.status_code == 409 and "EMAIL_ATTACHMENT_UNAVAILABLE" in refused.text
    finally:
        row["content"] = kept


def test_selection_keys_are_checked(client: TestClient) -> None:
    opportunity_id, draft = drafted(client)
    unknown = patch(client, opportunity_id, draft["id"], {"expected_revision": draft["revision"], "attachments": {f"external:{uuid4()}": True}})
    assert unknown.status_code == 400 and unknown.json()["error"]["code"] == "INVALID_EMAIL_ATTACHMENT"
    malformed = patch(client, opportunity_id, draft["id"], {"expected_revision": draft["revision"], "attachments": {"external:../../x": True}})
    assert malformed.status_code == 422


# ------------------------------------------------------------------ ownership


def test_uploaded_files_stay_with_their_owner_and_opportunity(client: TestClient) -> None:
    mine, my_draft = drafted(client)
    theirs, their_draft = drafted(client, OTHER)
    my_draft = ok(upload(client, mine, my_draft, "mine.pdf", pdf("mine")))["draft"]
    their_draft = ok(upload(client, theirs, their_draft, "theirs.pdf", pdf("theirs"), user=OTHER))["draft"]
    mine_item, their_item = uploaded(my_draft)[0], uploaded(their_draft)[0]

    assert upload(client, mine, my_draft, "intruder.pdf", pdf(), user=OTHER).status_code == 404
    assert get(client, f"{root(mine)}/{my_draft['id']}/attachments", OTHER).status_code == 404
    assert get(client, mine_item["download_path"], OTHER).status_code == 404
    assert remove(client, mine, my_draft, mine_item["artifact_version_id"], user=OTHER).status_code == 404
    # Their attachment id under my opportunity and draft is not found either.
    assert get(client, f"{root(mine)}/{my_draft['id']}/attachments/{their_item['artifact_version_id']}/download").status_code == 404
    assert remove(client, mine, my_draft, their_item["artifact_version_id"]).status_code == 404
    selection = patch(client, mine, my_draft["id"], {"expected_revision": my_draft["revision"], "attachments": {their_item["key"]: True}})
    assert selection.status_code == 400
    assert [item["file_name"] for item in uploaded(load(client, mine))] == ["mine.pdf"]
    assert client.post(f"{root(mine)}/{my_draft['id']}/attachments", files={"file": ("x.pdf", io.BytesIO(pdf()), "application/pdf")}, data={"expected_revision": "1"}).status_code == 401


# ------------------------------------------------------------------ Supabase store and migration


class FakeSupabase:
    """PostgREST tables and Storage objects of the BAP-3 lifecycle, with the database trigger that
    queues the storage path of every deleted email_attachments row. ``fail`` names operations
    that fail once, e.g. {"DELETE email_attachments", "DELETE object"}."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.queue: list[dict] = []
        self.objects: dict[str, bytes] = {}
        self.fail: set[str] = set()
        self.log: list[str] = []

    def _failing(self, name: str) -> bool:
        self.log.append(name)
        if name in self.fail:
            self.fail.discard(name)
            return True
        return False

    @staticmethod
    def _matches(row: dict, params: dict) -> bool:
        for key, value in params.items():
            if not isinstance(value, str) or key in {"select", "order", "limit"}:
                continue
            if value.startswith("eq.") and str(row.get(key)) != value[3:]:
                return False
            if value.startswith("lt.") and not str(row.get(key)) < value[3:]:
                return False
            if value.startswith("in.(") and str(row.get(key)) not in [item.strip('"') for item in value[4:-1].split(",")]:
                return False
        return True

    def request(self, store, method, table, *, json_body=None, params=None, headers=None):
        rows = self.rows if table == "email_attachments" else self.queue
        params = params or {}
        if self._failing(f"{method} {table}"):
            return httpx.Response(500, json={"message": "database error"})
        matched = [row for row in rows if self._matches(row, params)]
        if method == "POST":
            created = {**copy.deepcopy(json_body), "created_at": datetime.now(UTC).isoformat()}
            rows.append(created)
            return httpx.Response(201, json=[copy.deepcopy(created)])
        if method == "GET":
            limit = int(params.get("limit", len(matched) or 1))
            return httpx.Response(200, json=[copy.deepcopy(row) for row in matched[:limit]])
        if method == "PATCH":
            for row in matched:
                row.update(copy.deepcopy(json_body))
            return httpx.Response(200, json=[copy.deepcopy(row) for row in matched])
        if method == "DELETE":
            for row in matched:
                rows.remove(row)
                if table == "email_attachments":  # the AFTER DELETE trigger of migration 045
                    if not any(entry["storage_path"] == row["storage_path"] for entry in self.queue):
                        self.queue.append({"storage_path": row["storage_path"], "opportunity_id": row["opportunity_id"], "queued_at": "2026-10-19T09:01:00+00:00", "attempts": 0, "last_error": None})
            return httpx.Response(204)
        raise AssertionError(method)

    def storage(self, method, url, *, headers=None, content=None, **_kwargs):
        path = url.split("/storage/v1/object/email_attachments/", 1)[1]
        if self._failing(f"{method} object"):
            return httpx.Response(500)
        if method == "POST":
            self.objects[path] = content
            return httpx.Response(200, json={"Key": path})
        if method == "GET":
            return httpx.Response(200, content=self.objects[path]) if path in self.objects else httpx.Response(404)
        if method == "DELETE":
            return httpx.Response(200) if self.objects.pop(path, None) is not None else httpx.Response(404)
        raise AssertionError(method)


@pytest.fixture()
def supabase(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeSupabase, SupabaseDataStore]:
    import app.services.data.supabase_store as supabase_module

    fake = FakeSupabase()
    monkeypatch.setattr(SupabaseDataStore, "_request", lambda self, method, table, **kwargs: fake.request(self, method, table, **kwargs))
    monkeypatch.setattr(supabase_module, "_request_with_retry", fake.storage)
    monkeypatch.setattr(SupabaseDataStore, "get_opportunity", lambda self, *, opportunity_id, user_id: {"id": opportunity_id})
    return fake, SupabaseDataStore("test-token")


def stored_attachment(store: SupabaseDataStore, opportunity_id: UUID, name: str = "agenda.pdf") -> tuple[UUID, str, bytes]:
    attachment_id, content = uuid4(), pdf(name)
    path = f"{opportunity_id}/{attachment_id}.pdf"
    store.create_email_attachment(
        opportunity_id=opportunity_id, user_id=OWNER, journey_stage="deepening", attachment_id=attachment_id, file_name=name, extension=".pdf",
        mime_type="application/pdf", size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(), storage_path=path, content=content,
    )
    return attachment_id, path, content


def listed(store: SupabaseDataStore, opportunity_id: UUID) -> list[str]:
    return [row["file_name"] for row in store.list_email_attachments(opportunity_id=opportunity_id, user_id=OWNER, journey_stage="deepening")]


def test_supabase_upload_creates_a_pending_row_then_bytes_then_ready(supabase) -> None:
    fake, store = supabase
    opportunity_id = uuid4()
    attachment_id, path, content = stored_attachment(store, opportunity_id)
    assert fake.log[:3] == ["POST email_attachments", "POST object", "PATCH email_attachments"], "the row exists before any byte"
    assert fake.rows[0]["status"] == "ready" and fake.objects[path] == content
    assert listed(store, opportunity_id) == ["agenda.pdf"]
    assert store.read_email_attachment_content(opportunity_id=opportunity_id, user_id=OWNER, attachment_id=attachment_id) == content
    with pytest.raises(HTTPException) as foreign:
        store.get_email_attachment(opportunity_id=uuid4(), user_id=OWNER, attachment_id=attachment_id)
    assert foreign.value.status_code == 404


@pytest.mark.parametrize("failing", ["POST object", "PATCH email_attachments"], ids=["bytes-upload-fails", "ready-mark-fails"])
def test_supabase_failed_uploads_leave_nothing_behind(supabase, failing: str) -> None:
    fake, store = supabase
    fake.fail = {failing}
    with pytest.raises(HTTPException) as failed:
        stored_attachment(store, uuid4())
    assert failed.value.status_code == 503
    assert fake.rows == [] and fake.objects == {} and fake.queue == [], "row discarded, its path queued and swept"


def test_supabase_upload_rollback_whose_cleanup_fails_is_recovered_later(supabase) -> None:
    fake, store = supabase
    opportunity_id = uuid4()
    # The bytes are stored, marking the row ready fails, and discarding the row fails as well.
    fake.fail = {"PATCH email_attachments", "DELETE email_attachments"}
    with pytest.raises(HTTPException):
        stored_attachment(store, opportunity_id)
    (row,) = fake.rows
    assert row["status"] == "pending" and len(fake.objects) == 1
    assert listed(store, opportunity_id) == [], "an unfinished upload is never offered"
    # The sweep discards the stale pending row; the trigger queues its path; the bytes are removed.
    counts = store.process_email_attachment_cleanup(stale_after_seconds=-60)
    assert counts["discarded_pending"] == 1 and counts["removed"] == 1
    assert fake.rows == [] and fake.objects == {} and fake.queue == []


def test_supabase_delete_db_failure_changes_nothing_and_can_be_retried(supabase) -> None:
    fake, store = supabase
    opportunity_id = uuid4()
    attachment_id, path, content = stored_attachment(store, opportunity_id)
    fake.fail = {"DELETE email_attachments"}
    with pytest.raises(HTTPException) as failed:
        store.delete_email_attachment(opportunity_id=opportunity_id, user_id=OWNER, attachment_id=attachment_id)
    assert failed.value.status_code == 503
    assert listed(store, opportunity_id) == ["agenda.pdf"] and fake.objects[path] == content, "still listed, still readable"
    assert store.delete_email_attachment(opportunity_id=opportunity_id, user_id=OWNER, attachment_id=attachment_id) == {"bytes_removed": True}
    assert fake.rows == [] and fake.objects == {} and fake.queue == []


def test_supabase_storage_failure_after_the_row_is_gone_is_queued_and_swept(supabase) -> None:
    fake, store = supabase
    opportunity_id = uuid4()
    attachment_id, path, _content = stored_attachment(store, opportunity_id)
    fake.fail = {"DELETE object"}
    assert store.delete_email_attachment(opportunity_id=opportunity_id, user_id=OWNER, attachment_id=attachment_id) == {"bytes_removed": False}
    # The attachment is gone and nothing points at the bytes; the queue keeps them, with the error.
    assert fake.rows == [] and listed(store, opportunity_id) == []
    (entry,) = fake.queue
    assert entry["storage_path"] == path and entry["attempts"] == 1 and "storage HTTP 500" in entry["last_error"]
    assert path in fake.objects
    # Retrying is safe: a second sweep removes the bytes and the entry.
    assert store.process_email_attachment_cleanup()["removed"] == 1
    assert fake.objects == {} and fake.queue == []
    assert store.process_email_attachment_cleanup()["removed"] == 0, "nothing left to do"


def test_supabase_queue_entry_that_survives_a_removed_object_is_dropped_on_the_next_sweep(supabase) -> None:
    fake, store = supabase
    opportunity_id = uuid4()
    attachment_id, path, _content = stored_attachment(store, opportunity_id)
    fake.fail = {"DELETE email_attachment_storage_cleanup"}
    store.delete_email_attachment(opportunity_id=opportunity_id, user_id=OWNER, attachment_id=attachment_id)
    assert fake.objects == {} and [entry["storage_path"] for entry in fake.queue] == [path]
    assert store.process_email_attachment_cleanup()["removed"] == 1, "the object is already gone (404): the entry is dropped"
    assert fake.queue == []


def test_supabase_deleting_an_opportunity_removes_only_its_attachment_bytes(supabase) -> None:
    fake, store = supabase
    deleted, kept = uuid4(), uuid4()
    first = stored_attachment(store, deleted, "a.pdf")
    second = stored_attachment(store, deleted, "b.pdf")
    other = stored_attachment(store, kept, "c.pdf")
    # The opportunity is deleted in the database: ON DELETE CASCADE removes its rows, the trigger
    # queues their paths. The API is not involved.
    for row in [row for row in fake.rows if row["opportunity_id"] == str(deleted)]:
        fake.request(store, "DELETE", "email_attachments", params={"id": f"eq.{row['id']}"})
    assert sorted(entry["storage_path"] for entry in fake.queue) == sorted([first[1], second[1]])
    # The first sweep fails for one object; the failure stays queued and the next sweep recovers.
    fake.fail = {"DELETE object"}
    first_sweep = store.process_email_attachment_cleanup()
    assert (first_sweep["removed"], first_sweep["failed"]) == (1, 1) and len(fake.queue) == 1
    assert store.process_email_attachment_cleanup()["removed"] == 1
    assert fake.queue == [] and set(fake.objects) == {other[1]}, "the other opportunity's bytes are untouched"
    assert listed(store, kept) == ["c.pdf"]


def test_supabase_cleanup_never_deletes_a_foreign_or_malformed_path(supabase) -> None:
    fake, store = supabase
    opportunity_id, foreign = uuid4(), uuid4()
    _attachment_id, path, _content = stored_attachment(store, foreign)
    # A queue entry naming another opportunity's object, and one that is no attachment path at all.
    fake.queue.append({"storage_path": path, "opportunity_id": str(opportunity_id), "attempts": 0, "last_error": None})
    fake.queue.append({"storage_path": "../client_documents/secret.pdf", "opportunity_id": str(opportunity_id), "attempts": 0, "last_error": None})
    counts = store.process_email_attachment_cleanup()
    assert counts["skipped"] == 2 and counts["removed"] == 0
    assert path in fake.objects and all(entry["attempts"] == 1 for entry in fake.queue)


def test_the_migration_creates_an_owned_table_and_a_private_bucket() -> None:
    sql = (Path(__file__).resolve().parents[3] / "apps/services/api/supabase/migrations/045_bap3_email_attachments.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS public.email_attachments" in sql and "ENABLE ROW LEVEL SECURITY" in sql
    assert "WHERE created_by = auth.uid()" in sql and "storage_path TEXT NOT NULL UNIQUE" in sql
    assert "VALUES ('email_attachments', 'email_attachments', false)" in sql and "bucket_id = 'email_attachments'" in sql
    assert "size_bytes <= 10485760" in sql
    # The lifecycle: pending/ready rows and a durable cleanup queue filled by a trigger.
    assert "status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready'))" in sql
    assert "CREATE TABLE IF NOT EXISTS public.email_attachment_storage_cleanup" in sql
    assert "ALTER TABLE public.email_attachment_storage_cleanup ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY" not in sql.split("email_attachment_storage_cleanup (", 1)[1].split("CREATE OR REPLACE FUNCTION", 1)[0], "service role only"
    assert "SECURITY DEFINER" in sql and "AFTER DELETE ON public.email_attachments" in sql
    assert "ON CONFLICT (storage_path) DO NOTHING" in sql
