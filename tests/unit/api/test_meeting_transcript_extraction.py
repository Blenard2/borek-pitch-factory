"""Meeting transcripts with time stamps and speaker names yield their marked findings, and an
analysis without findings can neither be confirmed nor become Master Presentation V2."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from uuid import UUID

import jsonschema
import pytest
from fastapi.testclient import TestClient

from tests.unit.api.discovery2_helpers import ensure_discovery2_approved

from app.auth import create_test_access_token
from app.config import settings
from app.main import create_app
from app.services.data.memory_store import reset_memory_store
from app.services.post_meeting_review import _blockers
from services.meeting.extraction import classify_item_sources, extract_meeting_categories
from services.transcript.speaker_turns import split_speaker_turns

OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "packages" / "contracts" / "post_meeting_review.schema.json"
FACEBOOK = ROOT / "tests" / "fixtures" / "transcripts" / "facebook_first_meeting_synthetic.txt"
CATEGORIES = ("requirements", "challenges", "priorities", "opportunities", "discussed_solutions", "decisions", "follow_ups")
LABELS = {
    "Requirement": "requirements", "Challenge": "challenges", "Priority": "priorities", "Opportunity": "opportunities",
    "Discussed solution": "discussed_solutions", "Decision": "decisions", "Follow-up": "follow_ups",
}
# Read independently of the code under test: "[mm:ss] Speaker: Label: statement".
MARKED = re.compile(r"^\[(\d{2}:\d{2})\] ([^:]+): (" + "|".join(LABELS) + r"): (.+)$")
CLIENT = {"client_name": "Facebook", "opportunity_name": "Faster campaign reporting", "department": "Sales"}
SEVEN = [
    ("Requirement", "Campaign reports must be ready within one working day."),
    ("Challenge", "Campaign data sits in three tools."),
    ("Priority", "Start with the sales team."),
    ("Opportunity", "The service team could reuse the reports."),
    ("Discussed solution", "A shared reporting assistant."),
    ("Decision", "Run a pilot with five reports."),
    ("Follow-up", "Send two sample reports by 16.10.2026."),
]


def sections(name: str, text: str) -> list[dict]:
    return [{"speaker_role": turn.speaker, "content": turn.text} for turn in split_speaker_turns(name, text.encode())]


def fixture_findings(text: str, notes: str | None = None, name: str = "meeting.txt") -> dict[str, list[str]]:
    return extract_meeting_categories(sections=sections(name, text), personal_notes=notes, live=False)


def headers() -> dict[str, str]:
    token = create_test_access_token(user_id=OWNER, email="sales@example.com", secret=settings.SUPABASE_JWT_SECRET)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client() -> TestClient:
    reset_memory_store()
    return TestClient(create_app())


def ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def prepared(client: TestClient) -> str:
    """Approved Discovery, ready Master Presentation V1, first meeting completed."""
    opportunity_id = ok(client.post("/opportunities", headers=headers(), json=CLIENT), 201)["id"]
    ok(client.post(f"/opportunities/{opportunity_id}/discovery-paper/generate", headers=headers()))
    ok(client.post(f"/opportunities/{opportunity_id}/discovery-paper/approve", headers=headers()))
    ok(client.post(f"/opportunities/{opportunity_id}/stage1-outputs/generate", headers=headers()))
    ok(client.post(f"/opportunities/{opportunity_id}/workflow/first-meeting-completed", headers=headers()))
    return opportunity_id


def upload_and_analyse(client: TestClient, opportunity_id: str, text: str) -> None:
    uploaded = client.post(
        f"/opportunities/{opportunity_id}/transcripts", headers=headers(), files={"file": ("meeting.txt", io.BytesIO(text.encode()), "text/plain")}
    )
    transcript_id = ok(uploaded, 201)["transcript"]["id"]
    ok(client.post(f"/opportunities/{opportunity_id}/meeting-extraction/generate", headers=headers(), json={"transcript_id": transcript_id}))


def review(client: TestClient, opportunity_id: str) -> dict:
    body = ok(client.get(f"/opportunities/{opportunity_id}/post-meeting-review", headers=headers()))
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


# ------------------------------------------------------------------ the original transcript


def test_the_original_facebook_transcript_yields_every_marked_finding_with_its_speaker() -> None:
    text = FACEBOOK.read_text(encoding="utf-8")
    marked = [match.groups() for match in map(MARKED.match, text.splitlines()) if match]
    assert len(marked) == 26, "the transcript marks 26 findings"

    turns = split_speaker_turns(FACEBOOK.name, text.encode())
    assert len(turns) > 40, "one turn per time-stamped line, not one block"
    assert {speaker for _, speaker, _, _ in marked} <= {turn.speaker for turn in turns}
    assert not any(re.match(r"^[\[(]?\d{1,2}:\d{2}", turn.speaker) for turn in turns), "a time stamp is never a speaker"

    parsed = [{"speaker_role": turn.speaker, "content": turn.text} for turn in turns]
    findings = extract_meeting_categories(sections=parsed, personal_notes=None, live=False)
    assert {category: len(findings[category]) for category in CATEGORIES} == {
        "requirements": 5, "challenges": 3, "priorities": 4, "opportunities": 3, "discussed_solutions": 3, "decisions": 4, "follow_ups": 4,
    }
    # Exactly the marked statements, in order, in their category - nothing added, nothing lost.
    expected: dict[str, list[str]] = {category: [] for category in CATEGORIES}
    for _, _, label, statement in marked:
        expected[LABELS[label]].append(statement.strip())
    assert findings == expected

    # Every finding is traceable: to the transcript as its source, and to the turn of the person who said it.
    sources = classify_item_sources(findings, sections=parsed, personal_notes=None)
    assert all(label == "transcript" for labels in sources.values() for label in labels)
    for _, speaker, label, statement in marked:
        said = [turn for turn in turns if turn.text.startswith(f"{label}: {statement.strip()}")]
        assert [turn.speaker for turn in said] == [speaker], (speaker, statement)


def test_the_original_transcript_can_be_analysed_confirmed_and_is_ready_for_v2(client: TestClient) -> None:
    opportunity_id = prepared(client)
    upload_and_analyse(client, opportunity_id, FACEBOOK.read_text(encoding="utf-8"))
    view = review(client, opportunity_id)
    assert view["extraction"]["item_count"] == 26 and view["readiness"]["blockers"] == ["MEETING_REVIEW_NOT_CONFIRMED"]
    assert all(item["source"] == "transcript" for items in view["extraction"]["categories"].values() for item in items)
    assert all(view["extraction"]["categories"][category] for category in CATEGORIES)
    ok(confirm(client, opportunity_id, view))
    after = review(client, opportunity_id)
    assert after["confirmation"]["confirmed_count"] == 26 and after["readiness"] == {"ready_for_v2": False, "blockers": ["DISCOVERY2_MISSING"]}
    # BAP-7: Discovery 2 from the confirmed findings is the step that makes V2 possible.
    assert ensure_discovery2_approved(client, opportunity_id, headers())["readiness"] == {"ready_for_v2": True, "blockers": []}


# ------------------------------------------------------------------ line formats


@pytest.mark.parametrize(
    "prefix",
    ["Daniel: ", "[02:15] Daniel: ", "00:02:15 Daniel: ", "(02:15) Daniel: ", "02:15 - Daniel: ", "", "- ", "[02:15] "],
    ids=["speaker", "bracket-time", "plain-time", "paren-time", "dash-time", "label-only", "bullet", "time-only"],
)
def test_all_seven_categories_are_found_in_every_supported_line_format(prefix: str) -> None:
    text = "\n".join(f"{prefix}{label}: {statement}" for label, statement in SEVEN)
    findings = fixture_findings(text)
    assert findings == {LABELS[label]: [statement] for label, statement in SEVEN}
    # Notes are read line by line as well.
    assert extract_meeting_categories(sections=[], personal_notes=text, live=False) == findings


def test_label_variations_are_recognised() -> None:
    text = (
        "Daniel: requirements: One login.\nDaniel: CHALLENGES : Three tools.\nDaniel: Priorities: Sales first.\n"
        "Daniel: Opportunities: Service reuse.\nDaniel: Discussed solutions: An assistant.\nDaniel: Decisions: Pilot.\n"
        "Daniel: Follow up: Send samples.\nMira: Follow-ups: Book the review.\nMira: Followup: Share the plan.\n"
    )
    findings = fixture_findings(text)
    assert findings["requirements"] == ["One login."] and findings["challenges"] == ["Three tools."]
    assert findings["priorities"] == ["Sales first."] and findings["opportunities"] == ["Service reuse."]
    assert findings["discussed_solutions"] == ["An assistant."] and findings["decisions"] == ["Pilot."]
    assert findings["follow_ups"] == ["Send samples.", "Book the review.", "Share the plan."]


def test_a_multiline_finding_keeps_its_full_text_and_is_traceable() -> None:
    text = "[02:15] Daniel: Requirement: Reports must be ready\nwithin one working day.\n[02:40] Mira: Noted.\n"
    parsed = sections("meeting.txt", text)
    findings = extract_meeting_categories(sections=parsed, personal_notes=None, live=False)
    assert findings["requirements"] == ["Reports must be ready within one working day."]
    assert classify_item_sources(findings, sections=parsed, personal_notes=None)["requirements"] == ["transcript"]


def test_nothing_is_invented_from_unmarked_talk() -> None:
    text = (
        "[00:10] Daniel: We really require faster reports, that is the challenge.\n"
        "[00:30] Mira: My priority is the team. The decision is yours.\n"
        "[00:50] Daniel: Requirement:\n"
        "[01:10] Mira: Meeting at 10:30: fine.\n"
    )
    assert fixture_findings(text) == {category: [] for category in CATEGORIES}


def test_personal_notes_stay_a_separate_source() -> None:
    text = "[00:10] Daniel: Requirement: One login.\n[00:20] Daniel: Priority: Sales first.\n"
    notes = "- Priority: Sales first.\nOpportunity: Service reuse."
    parsed = sections("meeting.txt", text)
    findings = extract_meeting_categories(sections=parsed, personal_notes=notes, live=False)
    assert findings["priorities"] == ["Sales first."], "said in the meeting and noted: one finding"
    sources = classify_item_sources(findings, sections=parsed, personal_notes=notes)
    assert (sources["requirements"], sources["priorities"], sources["opportunities"]) == (["transcript"], ["both"], ["personal_notes"])


def test_repeated_statements_are_one_finding() -> None:
    text = (
        "[00:10] Daniel: Decision: Run a pilot.\n[05:00] Mira: Decision: run a  pilot\n[09:00] Daniel: Decision: Run a pilot.\n"
        "[09:30] Daniel: Decision: Start in May.\n[09:40] Daniel: Requirement: Run a pilot.\n"
    )
    parsed = sections("meeting.txt", text)
    findings = extract_meeting_categories(sections=parsed, personal_notes=None, live=False)
    assert findings["decisions"] == ["Run a pilot.", "Start in May."], "first wording kept, repeats dropped"
    assert findings["requirements"] == ["Run a pilot."], "the same words under another label are another finding"
    classify_item_sources(findings, sections=parsed, personal_notes=None)


def test_the_live_path_is_unchanged() -> None:
    parsed = sections("meeting.txt", "[00:10] Ada: Requirement: Flag duplicate invoices.\n")
    seen: dict[str, str] = {}

    def complete(system: str, user: str, schema: dict) -> dict:
        seen["user"] = user
        return {**{category: [] for category in CATEGORIES}, "requirements": ["Flag duplicate invoices.", "Invented requirement"]}

    findings = extract_meeting_categories(sections=parsed, personal_notes=None, live=True, complete=complete)
    assert findings["requirements"] == ["Flag duplicate invoices."], "unsupported items are still dropped"
    assert "Ada: Requirement: Flag duplicate invoices." in seen["user"] and "[00:10]" not in seen["user"]


# ------------------------------------------------------------------ no findings


def test_an_analysis_without_findings_cannot_be_confirmed_or_become_v2(client: TestClient) -> None:
    opportunity_id = prepared(client)
    upload_and_analyse(client, opportunity_id, "[00:10] Daniel: We talked about the weather.\n[00:20] Mira: Indeed.\n")
    view = review(client, opportunity_id)
    assert (view["extraction"]["status"], view["extraction"]["item_count"]) == ("current", 0)
    assert view["readiness"] == {"ready_for_v2": False, "blockers": ["MEETING_FINDINGS_EMPTY", "MEETING_REVIEW_NOT_CONFIRMED"]}

    refused = confirm(client, opportunity_id, view)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "MEETING_REVIEW_NO_FINDINGS"
    after = review(client, opportunity_id)
    assert after["confirmation"]["status"] == "none" and after["readiness"] == view["readiness"]

    generation = client.post(f"/opportunities/{opportunity_id}/master-presentation/v2/generate", headers=headers())
    assert generation.status_code == 409 and "MASTER_V2_NOT_READY" in generation.text
    assert "at least one finding" in generation.text
    status = ok(client.get(f"/opportunities/{opportunity_id}/master-presentation/v2", headers=headers()))
    assert (status["state"], status["can_generate"]) == ("none", False)


def test_excluding_every_finding_is_not_a_confirmation(client: TestClient) -> None:
    opportunity_id = prepared(client)
    upload_and_analyse(client, opportunity_id, "[00:10] Daniel: Requirement: One login.\n[00:20] Mira: Decision: Pilot.\n")
    view = review(client, opportunity_id)
    everything = {category: [item["text"] for item in items] for category, items in view["extraction"]["categories"].items()}
    refused = confirm(client, opportunity_id, view, everything)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "MEETING_REVIEW_NO_FINDINGS"
    assert review(client, opportunity_id)["confirmation"]["status"] == "none"
    # One remaining finding is enough.
    ok(confirm(client, opportunity_id, view, {"decisions": ["Pilot."]}))
    after = review(client, opportunity_id)
    assert after["confirmation"]["confirmed_count"] == 1 and after["readiness"]["blockers"] == ["DISCOVERY2_MISSING"]


def test_a_stored_confirmation_without_a_confirmed_finding_blocks_v2() -> None:
    ready = {
        "first_meeting": True, "discovery": {"status": "available"}, "master": {"status": "ready"}, "transcripts": [{}],
    }
    current = {"status": "current", "item_count": 3}
    assert _blockers(**ready, extraction=current, confirmation={"status": "current", "confirmed_count": 2}) == []
    assert _blockers(**ready, extraction=current, confirmation={"status": "current", "confirmed_count": 0}) == ["MEETING_FINDINGS_NONE_CONFIRMED"]
    assert _blockers(**ready, extraction={"status": "current", "item_count": 0}, confirmation={"status": "current", "confirmed_count": 0}) == [
        "MEETING_FINDINGS_EMPTY", "MEETING_FINDINGS_NONE_CONFIRMED",
    ]
