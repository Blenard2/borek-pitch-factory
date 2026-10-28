"""Transcript speaker integrity and transcript selection through the API (audit 2026-10-27).

* A German meeting with umlaut speakers is uploaded, split, analysed and attributed correctly.
* A transcript stored with merged speaker turns (read by the earlier parser) is reported as such in
  the review, so the page no longer says the findings match it.
* Re-uploading, replacing and re-analysing: the analysis uses exactly the transcript asked for,
  findings from another transcript are visibly not the selected one, and a new analysis makes an
  earlier confirmation stale. Earlier uploads are kept.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app.services.data.memory_store import get_memory_store
from tests.unit.api.test_meeting_transcript_extraction import client, confirm, headers, ok, prepared, review  # noqa: F401 - fixture

OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "transcripts"
GERMAN = (FIXTURES / "de_erstgespraech_mueller_praezision.txt").read_bytes()
ENGLISH = (FIXTURES / "en_first_meeting_harborview.txt").read_bytes()
CONTRACT = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "post_meeting_review.schema.json"


def upload(client: TestClient, opportunity_id: str, name: str, content: bytes) -> str:
    response = client.post(f"/opportunities/{opportunity_id}/transcripts", headers=headers(), files={"file": (name, io.BytesIO(content), "text/plain")})
    return ok(response, 201)["transcript"]["id"]


def analyse(client: TestClient, opportunity_id: str, transcript_id: str) -> dict:
    return ok(client.post(f"/opportunities/{opportunity_id}/meeting-extraction/generate", headers=headers(), json={"transcript_id": transcript_id}))


def counts(view: dict) -> dict[str, int]:
    return {category: len(items) for category, items in view["extraction"]["categories"].items()}


def entry(view: dict, transcript_id: str) -> dict:
    return next(item for item in view["transcripts"] if item["id"] == transcript_id)


def test_a_german_meeting_with_umlaut_speakers_is_read_turn_by_turn_and_every_finding_is_found(client: TestClient) -> None:
    opportunity_id = prepared(client)
    transcript_id = upload(client, opportunity_id, "erstgespräch.txt", GERMAN)
    stored = get_memory_store().list_transcript_sources(opportunity_id=UUID(opportunity_id), user_id=OWNER)[-1]["sections"]
    assert [section["speaker_role"] for section in stored][2:5] == ["Lena Großmann", "Dr. Jürgen Weiß", "Dr. Jürgen Weiß"]
    assert [section["section_index"] for section in stored] == list(range(len(stored)))

    analyse(client, opportunity_id, transcript_id)
    view = review(client, opportunity_id)
    assert entry(view, transcript_id) | {"created_at": None, "revision": None} == {
        "id": transcript_id, "file_name": "erstgespräch.txt", "processing_status": "pending", "created_at": None, "revision": None,
        "turn_count": 13, "merged_speaker_changes": 0, "analysed": True,
    }
    assert counts(view) == {category: 1 for category in counts(view)}
    assert {item["source"] for items in view["extraction"]["categories"].values() for item in items} == {"transcript"}
    assert view["extraction"]["categories"]["decisions"][0]["text"] == "Wir starten im Dezember einen Pilot mit zehn echten Anfragen."


def test_a_transcript_stored_with_merged_speaker_turns_is_reported_in_the_review(client: TestClient) -> None:
    opportunity_id = prepared(client)
    # Exactly how the earlier parser stored the audit transcript: two turns for seven speaker lines.
    legacy = get_memory_store().create_transcript(
        opportunity_id=UUID(opportunity_id), user_id=OWNER, file_name="meeting_umlaut.txt", mime_type="text/plain",
        storage_path=f"{opportunity_id}/legacy.txt", conversation_id="C1", content=b"(stored before the fix)",
        sections=[
            {"section_index": 0, "speaker_role": "Dr. Sarah O'Neill-Hughes", "content": "Thanks for joining. [00:00:40] Dr. Jürgen Weiß: Requirement: Quotes must reach the customer within one business day. [00:01:10] Zoë Brandt: Challenge: Pricing data lives in three systems.", "metadata": {}},
            {"section_index": 1, "speaker_role": "Sarah O'Neill-Hughes", "content": "Discussed solution: An assistant that drafts the quote. [00:03:05] Zoë Brandt: Decision: We will run a pilot with ten real quotes.", "metadata": {}},
        ],
    )
    analyse(client, opportunity_id, str(legacy["id"]))
    view = review(client, opportunity_id)
    assert entry(view, str(legacy["id"]))["merged_speaker_changes"] == 3
    assert view["extraction"]["status"] == "current", "the stored analysis itself is unchanged; the review only reports the merged turns"

    # Uploading the same meeting again reads it with the current rules.
    again = upload(client, opportunity_id, "meeting_umlaut.txt", ENGLISH)
    assert entry(review(client, opportunity_id), again)["merged_speaker_changes"] == 0


def test_replacing_and_reanalysing_uses_exactly_the_requested_transcript_and_keeps_earlier_uploads(client: TestClient) -> None:
    opportunity_id = prepared(client)
    first = upload(client, opportunity_id, "first.txt", b"Anna: Requirement: Quotes in one day.\nBen: Decision: Pilot in May.\n")
    analyse(client, opportunity_id, first)
    confirmed = ok(confirm(client, opportunity_id, review(client, opportunity_id)))
    assert confirmed["confirmation"]["status"] == "current"

    second = upload(client, opportunity_id, "second.txt", ENGLISH)
    view = review(client, opportunity_id)
    assert [item["id"] for item in view["transcripts"]] == [first, second], "the earlier upload is kept"
    assert (entry(view, first)["analysed"], entry(view, second)["analysed"]) == (True, False)
    assert view["extraction"]["transcript_id"] == first, "uploading never re-analyses by itself"

    stored = analyse(client, opportunity_id, second)
    assert stored["transcript_id"] == second
    view = review(client, opportunity_id)
    assert (entry(view, first)["analysed"], entry(view, second)["analysed"]) == (False, True)
    assert view["extraction"]["transcript_file_name"] == "second.txt"
    assert sum(counts(view).values()) == 6
    assert view["confirmation"]["status"] == "stale", "findings confirmed for the first transcript do not carry over"

    # Analysing the first transcript again is possible, and again only what was asked for.
    assert analyse(client, opportunity_id, first)["transcript_id"] == first
    assert review(client, opportunity_id)["extraction"]["categories"]["decisions"][0]["text"] == "Pilot in May."


def test_the_review_contract_describes_merged_speaker_changes() -> None:
    transcript = json.loads(CONTRACT.read_text(encoding="utf-8"))["$defs"]["Transcript"]
    assert "merged_speaker_changes" in transcript["required"]
    assert transcript["properties"]["merged_speaker_changes"]["minimum"] == 0
