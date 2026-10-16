"""BAP-6 — transcript upload accepts real meeting transcripts and the Post-Meeting flow continues.

The API accepts the MIME types browsers actually send, refuses files only for format, content or
size, keeps transcripts per owner, and live analysis reads natural conversation while every
finding stays grounded in a quote from the transcript or the notes.
"""

from __future__ import annotations

import io
import re
from uuid import UUID

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.auth import create_test_access_token
from app.config import settings
from app.main import create_app
from app.services.data.memory_store import get_memory_store, reset_memory_store
from services.meeting import extraction as meeting_extraction
from services.transcript.ingestion import MAX_TRANSCRIPT_BYTES
from tests.unit.api.test_meeting_transcript_extraction import confirm, headers, ok, prepared, review

OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CATEGORIES = ("requirements", "challenges", "priorities", "opportunities", "discussed_solutions", "decisions", "follow_ups")
NATURAL = (
    "Thanks for making the time today.\n"
    "Honestly our biggest pain is that we reconcile invoices by hand every Friday, it takes the whole team.\n"
    "If the ERP could match invoices to orders automatically that would change a lot for us.\n"
    "We would want to start with the finance team before anyone else.\n"
    "Fine, then let's meet again next Tuesday with finance to look at sample data.\n"
)
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture()
def client() -> TestClient:
    reset_memory_store()
    return TestClient(create_app())


def other_headers() -> dict[str, str]:
    token = create_test_access_token(user_id=OTHER, email="other@example.com", secret=settings.SUPABASE_JWT_SECRET)
    return {"Authorization": f"Bearer {token}"}


def upload(client: TestClient, opportunity_id: str, name: str, content: bytes, mime: str | None, auth: dict | None = None):
    file = (name, io.BytesIO(content), mime) if mime is not None else (name, io.BytesIO(content))
    return client.post(f"/opportunities/{opportunity_id}/transcripts", headers=auth or headers(), files={"file": file})


def new_opportunity(client: TestClient) -> str:
    return ok(client.post("/opportunities", headers=headers(), json={"client_name": "Acme", "opportunity_name": "Invoices", "department": "Finance"}), 201)["id"]


def docx_bytes(*paragraphs: str) -> bytes:
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def stored_sections(opportunity_id: str) -> list[dict]:
    return get_memory_store().list_transcript_sources(opportunity_id=UUID(opportunity_id), user_id=OWNER)[-1]["sections"]


# ------------------------------------------------------------------ accepted uploads


@pytest.mark.parametrize(
    "name,content,mime",
    [
        ("meeting.txt", NATURAL.encode(), "text/plain; charset=utf-8"),
        ("MEETING.TXT", NATURAL.encode(), "text/plain"),
        ("call.vtt", b"WEBVTT\n\n00:01.000 --> 00:04.000\n<v Anna>We need single sign-on.\n", "text/vtt; charset=utf-8"),
        ("call.vtt", b"WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nWe need single sign-on.\n", "text/x-vtt"),
        ("call.srt", b"1\n00:00:01,000 --> 00:00:02,000\nWe need single sign-on.\n", "text/srt"),
        ("call.srt", b"1\n00:00:01,000 --> 00:00:02,000\nWe need single sign-on.\n", "application/x-srt"),
        ("call.SRT", b"1\n00:00:01,000 --> 00:00:02,000\nWe need single sign-on.\n", "application/octet-stream"),
        ("notes.docx", docx_bytes("We need single sign-on."), DOCX_MIME),
        ("notes.DOCX", docx_bytes("We need single sign-on."), "application/zip"),
        ("notes.docx", docx_bytes("We need single sign-on."), "application/x-zip-compressed"),
    ],
    ids=["txt-charset", "txt-upper", "vtt-short-times", "vtt-x-vtt", "srt-text-srt", "srt-x-srt", "srt-upper-octet", "docx", "docx-zip", "docx-x-zip"],
)
def test_real_browser_uploads_are_accepted(client: TestClient, name: str, content: bytes, mime: str) -> None:
    opportunity_id = new_opportunity(client)
    body = ok(upload(client, opportunity_id, name, content, mime), 201)
    assert body["transcript"]["file_name"] == name
    assert " ".join(section["content"] for section in stored_sections(opportunity_id)).strip()


def test_an_unstructured_transcript_is_stored_whole_without_names_times_or_labels(client: TestClient) -> None:
    opportunity_id = new_opportunity(client)
    ok(upload(client, opportunity_id, "meeting.txt", NATURAL.encode(), "text/plain"), 201)
    sections = stored_sections(opportunity_id)
    assert [section["speaker_role"] for section in sections] == ["unknown"]
    assert sections[0]["content"] == " ".join(NATURAL.split("\n")).strip()


# ------------------------------------------------------------------ refused uploads


@pytest.mark.parametrize(
    "name,content,mime,code,message",
    [
        ("slides.pdf", b"%PDF-1.7", "application/pdf", "INVALID_TRANSCRIPT_FORMAT", "Unsupported transcript extension"),
        ("meeting.txt", b"hello", "image/png", "INVALID_TRANSCRIPT_FORMAT", "does not match a .txt transcript"),
        ("meeting.txt", b"", "text/plain", "INVALID_TRANSCRIPT_CONTENT", "empty"),
        ("meeting.txt", b"\n \n", "text/plain", "INVALID_TRANSCRIPT_CONTENT", "no transcript text"),
        ("meeting.txt", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "text/plain", "INVALID_TRANSCRIPT_CONTENT", "plain text|not valid text"),
        ("meeting.docx", b"PK\x03\x04 not really a document", DOCX_MIME, "INVALID_TRANSCRIPT_CONTENT", "could not be read"),
        ("big.txt", b"a" * (MAX_TRANSCRIPT_BYTES + 1), "text/plain", "TRANSCRIPT_TOO_LARGE", "larger than 10 MB"),
    ],
    ids=["pdf", "mime-mismatch", "empty", "blank", "binary", "corrupt-docx", "too-large"],
)
def test_unsupported_empty_corrupt_and_oversized_files_get_a_clear_error(
    client: TestClient, name: str, content: bytes, mime: str, code: str, message: str
) -> None:
    opportunity_id = new_opportunity(client)
    response = upload(client, opportunity_id, name, content, mime)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == code
    assert re.search(message, response.json()["error"]["message"])
    assert get_memory_store().list_transcript_sources(opportunity_id=UUID(opportunity_id), user_id=OWNER) == []


# ------------------------------------------------------------------ ownership


def test_transcripts_stay_with_the_owner_of_the_opportunity(client: TestClient) -> None:
    opportunity_id = new_opportunity(client)
    ok(upload(client, opportunity_id, "meeting.txt", NATURAL.encode(), "text/plain"), 201)
    assert upload(client, opportunity_id, "meeting.txt", b"intruder text", "text/plain", other_headers()).status_code == 404
    assert client.get(f"/opportunities/{opportunity_id}/transcripts", headers=other_headers()).status_code == 404
    assert len(ok(client.get(f"/opportunities/{opportunity_id}/transcripts", headers=headers()))) == 1


# ------------------------------------------------------------------ live analysis of natural conversation


def model_answer(**items: list[dict[str, str]]) -> dict:
    return {category: items.get(category, []) for category in CATEGORIES}


def test_live_analysis_reads_natural_conversation_and_the_flow_continues(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    opportunity_id = prepared(client)
    other_opportunity = prepared(client)
    ok(upload(client, other_opportunity, "other.txt", b"Anna: Other client wants a mobile app with offline mode.", "text/plain"), 201)
    transcript_id = ok(upload(client, opportunity_id, "meeting.txt", NATURAL.encode(), "text/plain"), 201)["transcript"]["id"]
    ok(client.put(f"/opportunities/{opportunity_id}/personal-notes", headers=headers(), json={"text": "Budget owner is the CFO."}))
    seen: dict[str, str] = {}

    def fake_model(system: str, user: str, schema: dict) -> dict:
        seen.update(system=system, user=user)
        item = schema["properties"]["requirements"]["items"]
        assert item["required"] == ["text", "evidence"]
        return model_answer(
            challenges=[{"text": "Invoices are reconciled manually every week", "evidence": "we reconcile invoices by hand every Friday"}],
            requirements=[
                {"text": "Automatic matching of invoices to orders in the ERP", "evidence": "the ERP could match invoices to orders automatically"},
                # Fabricated: the quote is not in either source.
                {"text": "Integration with SAP", "evidence": "we must integrate with SAP"},
                # Another client's transcript: never part of this opportunity's sources.
                {"text": "Mobile app with offline mode", "evidence": "wants a mobile app with offline mode"},
            ],
            priorities=[
                {"text": "Finance team first", "evidence": "start with the finance team before anyone else"},
                # Too short to show what the finding is about.
                {"text": "Everything is a priority", "evidence": "Fine"},
            ],
            follow_ups=[{"text": "Meeting with finance next Tuesday to review sample data", "evidence": "let’s meet again next Tuesday with finance"}],
            opportunities=[{"text": "The CFO owns the budget", "evidence": "Budget owner is the CFO."}],
        )

    monkeypatch.setattr(settings, "AI_EXECUTION_MODE", "live")
    monkeypatch.setattr(meeting_extraction, "_anthropic_complete", fake_model)
    ok(client.post(f"/opportunities/{opportunity_id}/meeting-extraction/generate", headers=headers(), json={"transcript_id": transcript_id}))

    assert "Do not rely on labels being present" in seen["system"] and "Do not invent" in seen["system"]
    assert "unknown: Thanks for making the time today." in seen["user"]
    assert "offline mode" not in seen["user"], "another client's transcript is never sent"

    view = review(client, opportunity_id)
    categories = {category: [(item["text"], item["source"]) for item in items] for category, items in view["extraction"]["categories"].items()}
    assert categories["challenges"] == [("Invoices are reconciled manually every week", "transcript")]
    assert categories["requirements"] == [("Automatic matching of invoices to orders in the ERP", "transcript")]
    assert categories["priorities"] == [("Finance team first", "transcript")]
    assert categories["follow_ups"] == [("Meeting with finance next Tuesday to review sample data", "transcript")]
    assert categories["opportunities"] == [("The CFO owns the budget", "personal_notes")]
    assert categories["decisions"] == categories["discussed_solutions"] == []
    assert view["extraction"]["execution_mode"] == "live" and view["extraction"]["item_count"] == 5

    # The owner still reviews and confirms; afterwards Discovery 2 (BAP-7) and then V2 follow.
    assert view["readiness"]["blockers"] == ["MEETING_REVIEW_NOT_CONFIRMED"]
    ok(confirm(client, opportunity_id, view))
    after = review(client, opportunity_id)
    assert after["readiness"]["blockers"] == ["DISCOVERY2_MISSING"] and after["discovery2"]["can_generate"] is True


def test_live_analysis_that_finds_nothing_stays_empty_and_cannot_be_confirmed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    opportunity_id = prepared(client)
    transcript_id = ok(upload(client, opportunity_id, "chat.txt", b"Nice weather today.\nIndeed, very sunny.", "text/plain"), 201)["transcript"]["id"]
    monkeypatch.setattr(settings, "AI_EXECUTION_MODE", "live")
    monkeypatch.setattr(meeting_extraction, "_anthropic_complete", lambda system, user, schema: model_answer(
        requirements=[{"text": "Customer needs a weather dashboard", "evidence": "we need a weather dashboard"}],
    ))
    ok(client.post(f"/opportunities/{opportunity_id}/meeting-extraction/generate", headers=headers(), json={"transcript_id": transcript_id}))
    view = review(client, opportunity_id)
    assert (view["extraction"]["item_count"], view["extraction"]["execution_mode"]) == (0, "live")
    assert "MEETING_FINDINGS_EMPTY" in view["readiness"]["blockers"]
    refused = confirm(client, opportunity_id, view)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "MEETING_REVIEW_NO_FINDINGS"


def test_fixture_mode_is_reported_as_fixture_and_does_not_interpret_conversation(client: TestClient) -> None:
    opportunity_id = prepared(client)
    transcript_id = ok(upload(client, opportunity_id, "meeting.txt", NATURAL.encode(), "text/plain"), 201)["transcript"]["id"]
    ok(client.post(f"/opportunities/{opportunity_id}/meeting-extraction/generate", headers=headers(), json={"transcript_id": transcript_id}))
    view = review(client, opportunity_id)
    assert (view["extraction"]["execution_mode"], view["extraction"]["item_count"]) == ("fixture", 0)
