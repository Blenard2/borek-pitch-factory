"""Post-meeting follow-up email: finalized V2 sources, saved edits, review, confirmation, export."""

from __future__ import annotations

import copy
import email
import email.policy
import hashlib
import threading
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.services import followup_email as email_service
from app.services.data.memory_store import get_memory_store
from llm.claude.client import ClaudeClientError
from tests.unit.api.discovery2_helpers import ensure_discovery2_approved
from tests.unit.api.test_master_presentation_v2_api import (
    EXCLUDED,
    OBSERVATION,
    OTHER,
    OWNER,
    client,  # noqa: F401 - fixture
    confirm,
    download,
    generate_v2,
    get,
    headers,
    ok,
    post,
    prepared,
)

CHECKS = ["recipients", "dates_and_owners", "supported_statements", "review_flags", "tone", "attachments"]
STATICS = {
    "project_name": "Nordwind Quoting",
    "client_short": "Nordwind",
    "salutation_style": "informal",
    "standard_recipients": [
        {"email": "dana.weber@nordwind.example", "first_name": "Dana", "last_name": "Weber", "salutation": "Ms", "kind": "to", "primary": True},
        {"email": "tom.keller@nordwind.example", "first_name": "Tom", "last_name": "Keller", "kind": "cc"},
    ],
    "sender_profile": {"name": "Jörg Müller", "role": "Managing Partner", "email": "joerg@borek.example"},
}


def set_statics(client: TestClient, opportunity_id: str, statics: dict | None = None, user: UUID = OWNER) -> None:
    response = client.patch(f"/opportunities/{opportunity_id}", headers=headers(user), json={"followup_statics": statics or STATICS})
    assert response.status_code == 200, response.text


def finalized(client: TestClient, user: UUID = OWNER, *, statics: bool = True, excluded: dict | None = None, **kwargs) -> dict:
    """A reviewed and finalized Master Presentation V2 package."""
    base = prepared(client, user, **kwargs)
    opportunity_id = base["opportunity"]
    view = ok(get(client, f"/opportunities/{opportunity_id}/post-meeting-review", user))
    ok(
        post(
            client,
            f"/opportunities/{opportunity_id}/post-meeting-review/confirm",
            {
                "transcript_id": view["extraction"]["transcript_id"],
                "extraction_generated_at": view["extraction"]["generated_at"],
                "review_fingerprint": view["review_fingerprint"],
                "excluded": {"follow_ups": [EXCLUDED]} if excluded is None else excluded,
            },
            user,
        )
    )
    ensure_discovery2_approved(client, opportunity_id, headers(user))
    base["v2"] = ok(generate_v2(client, opportunity_id, user))["presentation_version_id"]
    ok(post(client, f"/opportunities/{opportunity_id}/workflow/owner-reviewed", user=user))
    base["package"] = ok(post(client, f"/opportunities/{opportunity_id}/workflow/finalize", user=user))["finalization"]
    if statics:
        set_statics(client, opportunity_id, user=user)
    return base


def root(opportunity_id: str) -> str:
    return f"/opportunities/{opportunity_id}/email-drafts"


def generate(client: TestClient, opportunity_id: str, user: UUID = OWNER, **body):
    return post(client, f"{root(opportunity_id)}/generate", {"journey_stage": "deepening", **body}, user)


def load(client: TestClient, opportunity_id: str, user: UUID = OWNER) -> dict:
    return ok(get(client, f"{root(opportunity_id)}?journey_stage=deepening", user))["draft"]


def patch(client: TestClient, opportunity_id: str, draft_id: str, body: dict, user: UUID = OWNER):
    return client.patch(f"{root(opportunity_id)}/{draft_id}", headers=headers(user), json=body)


def confirm_email(client: TestClient, opportunity_id: str, draft: dict, length: str = "medium", user: UUID = OWNER, **override):
    body = {"selected_length": length, "expected_revision": draft["revision"], "review_checks": CHECKS, "acknowledged_flags": draft["review_flags"], **override}
    return post(client, f"{root(opportunity_id)}/{draft['id']}/confirm", body, user)


def export(client: TestClient, opportunity_id: str, draft: dict, user: UUID = OWNER, revision: int | None = None):
    return get(client, f"{root(opportunity_id)}/{draft['id']}/export?revision={revision or draft['revision']}", user)


def test_three_lengths_are_written_from_the_finalized_v2_package(client: TestClient) -> None:
    base = finalized(client, excluded={"follow_ups": []})
    opportunity_id = base["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]

    source = draft["source"]
    assert (source["kind"], source["product_version"]) == ("master_presentation_v2", "V2")
    assert source["presentation_version_id"] == base["v2"] == base["package"]["ppt2_version_id"]
    assert source["presentation_id"] == base["presentation"]
    assert source["snapshot_hash"] == base["package"]["ppt2_generation_source_manifest"]["snapshot_hash"]
    assert source["transcript_id"] == base["transcript"] and source["approved_discovery_version_id"] == base["approved"]
    assert draft["source_status"] == "valid" and draft["status"] == "draft" and draft["send_status"] == "not_sent"
    assert draft["revision"] == 1 and draft["confirmed_revision"] is None and draft["word_limits"] == {"short": 150, "medium": 300, "extensive": 500}

    short, medium, extensive = (draft["lengths"][name] for name in ("short", "medium", "extensive"))
    assert len({short["body"], medium["body"], extensive["body"]}) == 3, "three different drafts"
    assert short["word_count"] <= 150 and medium["word_count"] <= 300 and extensive["word_count"] <= 500
    assert short["word_count"] < medium["word_count"] <= extensive["word_count"]
    for text in (short, medium, extensive):
        assert text["subject"] == "Nordwind Quoting — Follow-up to our meeting" and text["edited"] is False
        assert text["body"].startswith("Hi Dana,\n") and "Jörg Müller" in text["body"] and "Managing Partner - BOREK" in text["body"]
        assert "{{" not in text["body"] and "None" not in text["body"]
        # A discussed solution is never reported as agreed, an owner observation never as a client statement.
        assert OBSERVATION not in text["body"]
        assert "Send the pricing export by Friday." in text["body"], "the confirmed follow-up is a next step"
    assert "the situation at Nordwind" in short["body"], "the client is named as the owner entered it"
    assert "quotes must go out within one day" in short["body"] and "way forward" not in short["body"]
    assert "For the way forward, this is the direction that was discussed: run a pilot with ten quotes." in medium["body"]
    assert "One idea we talked through, not decided yet: a drafting assistant that prepares the quote from the request." in medium["body"]
    assert "not decided yet: a drafting assistant" in extensive["body"]
    assert "we noted the following" not in short["body"] + medium["body"] + extensive["body"]
    # A letter, not a report: no headed sections, and the attached presentation is introduced.
    for text in (short, medium, extensive):
        assert not {"Key points", "Requirements", "Decisions noted", "Next steps"} & set(text["body"].splitlines())
        assert "attached" in text["body"] and text["body"].index("attached") < text["body"].index("Send the pricing export by Friday.")
    assert source["renderer_version"] == "followup-finalized:v3"
    assert "trade fair" not in extensive["body"], "small talk was never a finding"
    # No date, participant or owner is invented.
    assert "2026" not in extensive["body"] and "Participants" not in extensive["body"]
    assert {"fixture_extraction", "speaker_unverified", "meeting_date_unconfirmed", "owner_notes_omitted", "action_owner_unconfirmed"} <= set(draft["review_flags"])
    assert "action_date_unconfirmed" not in draft["review_flags"], "'by Friday' is the stated deadline"
    assert draft["recipients"] == {
        "to": [{"name": "Dana Weber", "email": "dana.weber@nordwind.example"}],
        "cc": [{"name": "Tom Keller", "email": "tom.keller@nordwind.example"}],
        "sender": {"name": "Jörg Müller", "role": "Managing Partner", "email": "joerg@borek.example"},
    }
    assert load(client, opportunity_id) == draft, "the generated draft is persisted"


def test_excluded_findings_and_later_source_changes_never_reach_the_email(client: TestClient) -> None:
    base = finalized(client)  # the follow-up is excluded
    opportunity_id = base["opportunity"]
    first = ok(generate(client, opportunity_id))["draft"]
    bodies = " ".join(first["lengths"][name]["body"] for name in ("short", "medium", "extensive"))
    assert EXCLUDED not in bodies and "next step" not in bodies.lower(), "an excluded finding is not an agreed next step, and the empty block is omitted"
    assert "no_next_steps_confirmed" in first["review_flags"] and "action_owner_unconfirmed" not in first["review_flags"]

    # After finalization: a newer transcript, changed notes and a re-analysis. None of it is in the package.
    import io

    later = client.post(
        f"/opportunities/{opportunity_id}/transcripts",
        headers=headers(),
        files={"file": ("later.txt", io.BytesIO(b"Dana: Decision: Cancel the pilot.\nDana: Requirement: Everything must be free.\n"), "text/plain")},
    )
    later_id = ok(later, 201)["transcript"]["id"]
    ok(client.put(f"/opportunities/{opportunity_id}/personal-notes", headers=headers(), json={"text": "Decision: The client wants a discount."}))
    ok(post(client, f"/opportunities/{opportunity_id}/meeting-extraction/generate", {"transcript_id": later_id}))
    again = ok(generate(client, opportunity_id))["draft"]
    assert again["lengths"] == first["lengths"] and again["source"] == first["source"]
    assert again["source"]["transcript_id"] == base["transcript"] != later_id
    assert "Cancel the pilot" not in str(again) and "discount" not in str(again)


def test_generation_needs_a_finalized_package_and_complete_statics(client: TestClient) -> None:
    base = prepared(client)
    opportunity_id = base["opportunity"]
    set_statics(client, opportunity_id)
    early = generate(client, opportunity_id)
    assert early.status_code == 400 and "FOLLOWUP_FINALIZATION_REQUIRED" in early.text, "never from the latest transcript"
    confirm(client, opportunity_id)
    ok(generate_v2(client, opportunity_id))
    ok(post(client, f"/opportunities/{opportunity_id}/workflow/owner-reviewed"))
    not_final = generate(client, opportunity_id)
    assert not_final.status_code == 400 and "FOLLOWUP_FINALIZATION_REQUIRED" in not_final.text
    assert load(client, opportunity_id) is None

    other = finalized(client, statics=False)
    missing = generate(client, other["opportunity"])
    assert missing.status_code == 400 and "FOLLOWUP_STATICS_REQUIRED" in missing.text
    formal = copy.deepcopy(STATICS)
    formal["salutation_style"] = "formal"
    formal["standard_recipients"][0].pop("salutation")
    assert client.patch(f"/opportunities/{other['opportunity']}", headers=headers(), json={"followup_statics": formal}).status_code == 422
    # Statics stored before that rule existed: the greeting is not written with a missing name.
    get_memory_store().opportunities[UUID(other["opportunity"])]["followup_statics"] = copy.deepcopy(formal)
    incomplete = generate(client, other["opportunity"])
    assert incomplete.status_code == 400 and "FOLLOWUP_STATICS_INCOMPLETE" in incomplete.text
    formal["standard_recipients"][0]["salutation"] = "Ms"
    set_statics(client, other["opportunity"], formal)
    assert ok(generate(client, other["opportunity"]))["draft"]["lengths"]["short"]["body"].startswith("Dear Ms Weber,\n")


def test_a_package_whose_frozen_findings_cannot_be_recovered_is_refused(client: TestClient) -> None:
    base = finalized(client)
    opportunity_id = base["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]
    store = get_memory_store()
    for job in store.generation_jobs.values():
        snapshot = (job.get("result_json") or {}).get("_enqueue", {}).get("master_v2_snapshot")
        if snapshot:
            snapshot["findings"]["decisions"].append({"text": "The client signed the contract.", "source": "transcript"})
    refused = generate(client, opportunity_id, overwrite_edits=True)
    assert refused.status_code == 409 and "FOLLOWUP_SOURCE_UNAVAILABLE" in refused.text, "altered findings do not match the manifest checksum"
    assert load(client, opportunity_id)["source_status"] == "changed"
    blocked = confirm_email(client, opportunity_id, draft)
    assert blocked.status_code == 409 and "FOLLOWUP_SOURCE_CHANGED" in blocked.text
    assert "signed the contract" not in str(load(client, opportunity_id))


def test_edits_are_saved_per_length_and_survive_reload(client: TestClient) -> None:
    opportunity_id = finalized(client)["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]
    draft_id = draft["id"]
    medium = draft["lengths"]["medium"]
    edited_body = medium["body"].replace("Thank you for taking the time to speak with us.", "Thank you for the very open and friendly conversation in Hamburg.")
    saved = ok(
        patch(
            client,
            opportunity_id,
            draft_id,
            {"expected_revision": 1, "selected_length": "medium", "lengths": {"medium": {"subject": "Nordwind — next steps after our meeting", "body": edited_body}}},
        )
    )["draft"]
    assert saved["revision"] == 2 and saved["selected_length"] == "medium" and saved["status"] == "draft"
    assert saved["lengths"]["medium"] == {
        "subject": "Nordwind — next steps after our meeting",
        "body": edited_body,
        "word_count": saved["lengths"]["medium"]["word_count"],
        "edited": True,
    }
    assert saved["lengths"]["medium"]["word_count"] == medium["word_count"] + 1, "word count follows the saved text"
    assert saved["lengths"]["short"] == draft["lengths"]["short"] and saved["lengths"]["extensive"] == draft["lengths"]["extensive"]

    # Another length is edited later: the first edit stays.
    short_body = draft["lengths"]["short"]["body"].replace("In brief, the main points", "The main points")
    second = ok(patch(client, opportunity_id, draft_id, {"expected_revision": 2, "selected_length": "short", "lengths": {"short": {"subject": draft["lengths"]["short"]["subject"], "body": short_body}}}))["draft"]
    assert second["revision"] == 3 and second["selected_length"] == "short"
    assert second["lengths"]["medium"] == saved["lengths"]["medium"] and second["lengths"]["short"]["edited"] is True
    assert second["lengths"]["extensive"]["edited"] is False

    # Reload and reopen: the exact saved state comes back, from the server.
    assert load(client, opportunity_id) == second
    assert load(TestClient(client.app), opportunity_id) == second
    # Saving the same content again changes nothing.
    assert ok(patch(client, opportunity_id, draft_id, {"expected_revision": 3, "selected_length": "short"}))["draft"]["revision"] == 3


def test_a_stale_edit_is_refused_and_does_not_overwrite(client: TestClient) -> None:
    opportunity_id = finalized(client)["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]
    text = draft["lengths"]["short"]
    first = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 1, "lengths": {"short": {"subject": "From the first editor", "body": text["body"]}}}))["draft"]
    stale = patch(client, opportunity_id, draft["id"], {"expected_revision": 1, "lengths": {"short": {"subject": "From the second editor", "body": text["body"]}}})
    assert stale.status_code == 409 and "EMAIL_DRAFT_CONFLICT" in stale.text
    assert load(client, opportunity_id) == first and first["lengths"]["short"]["subject"] == "From the first editor"

    # Eight simultaneous saves on the same revision: exactly one is stored.
    barrier = threading.Barrier(8)
    codes: list[int] = []

    def save(index: int) -> None:
        barrier.wait()
        response = patch(client, opportunity_id, draft["id"], {"expected_revision": 2, "lengths": {"short": {"subject": f"Editor {index}", "body": text["body"]}}})
        codes.append(response.status_code)

    threads = [threading.Thread(target=save, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(codes) == [200] + [409] * 7
    assert load(client, opportunity_id)["revision"] == 3


def test_edits_are_validated_on_the_server(client: TestClient) -> None:
    opportunity_id = finalized(client)["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]
    text = draft["lengths"]["short"]

    def save(subject: str, body: str, length: str = "short"):
        return patch(client, opportunity_id, draft["id"], {"expected_revision": 1, "lengths": {length: {"subject": subject, "body": body}}})

    too_long = text["body"].replace("Best regards", ("word " * 160) + "\n\nBest regards")
    cases = {
        "EMAIL_LENGTH_EXCEEDED": save(text["subject"], too_long),
        "EMAIL_PLACEHOLDER_UNRESOLVED": save(text["subject"], text["body"] + "\n{{next_meeting}}"),
        "EMAIL_SUBJECT_INVALID": save("First line\nBcc: someone@example.com", text["body"]),
        "EMAIL_BODY_INVALID": save(text["subject"], "   \n  "),
    }
    for code, response in cases.items():
        assert response.status_code == 400 and code in response.text, (code, response.text)
    assert save("   ", text["body"]).status_code in (400, 422)
    assert patch(client, opportunity_id, draft["id"], {"expected_revision": 1, "lengths": {"tiny": {"subject": "x", "body": "y"}}}).status_code == 422
    assert patch(client, opportunity_id, draft["id"], {"lengths": {}}).status_code == 422, "a save must name its revision"
    # The same text fits the longer draft.
    assert save(text["subject"], too_long, "medium").status_code == 200
    assert load(client, opportunity_id)["lengths"]["short"] == text


def test_regenerating_never_discards_saved_work_silently(client: TestClient) -> None:
    opportunity_id = finalized(client)["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]
    assert ok(generate(client, opportunity_id))["draft"]["revision"] == 2, "an untouched draft can be regenerated"
    text = draft["lengths"]["short"]
    edited = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 2, "lengths": {"short": {"subject": "My own subject", "body": text["body"]}}}))["draft"]
    refused = generate(client, opportunity_id)
    assert refused.status_code == 409 and "EMAIL_DRAFT_HAS_EDITS" in refused.text
    assert load(client, opportunity_id) == edited
    replaced = ok(generate(client, opportunity_id, overwrite_edits=True))["draft"]
    assert replaced["id"] == draft["id"] and replaced["revision"] == 4
    assert replaced["lengths"]["short"] == text and replaced["status"] == "draft"


def test_confirmation_needs_the_review_and_binds_to_the_saved_revision(client: TestClient) -> None:
    opportunity_id = finalized(client)["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]
    url = f"{root(opportunity_id)}/{draft['id']}/confirm"

    bare = post(client, url, {"selected_length": "medium"})
    assert bare.status_code == 400 and "EMAIL_REVISION_REQUIRED" in bare.text
    partial = confirm_email(client, opportunity_id, draft, review_checks=CHECKS[:-1])
    assert partial.status_code == 400 and "EMAIL_REVIEW_INCOMPLETE" in partial.text and "attachments" in partial.text
    unacknowledged = confirm_email(client, opportunity_id, draft, acknowledged_flags=draft["review_flags"][:1])
    assert unacknowledged.status_code == 400 and "EMAIL_REVIEW_FLAGS_OPEN" in unacknowledged.text
    assert load(client, opportunity_id)["status"] == "draft"

    # Someone saves a change while the review is open: the review was of another text.
    text = draft["lengths"]["medium"]
    changed = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 1, "lengths": {"medium": {"subject": "Changed meanwhile", "body": text["body"]}}}))["draft"]
    stale = confirm_email(client, opportunity_id, draft)
    assert stale.status_code == 409 and "EMAIL_DRAFT_CONFLICT" in stale.text
    assert load(client, opportunity_id)["status"] == "draft"

    confirmed = ok(confirm_email(client, opportunity_id, changed))["draft"]
    assert confirmed["status"] == "confirmed" and confirmed["send_status"] == "not_sent"
    assert confirmed["selected_length"] == "medium" and confirmed["lengths"]["medium"]["subject"] == "Changed meanwhile"
    assert confirmed["revision"] == confirmed["confirmed_revision"] == 3
    assert confirmed["confirmed_by"] == str(OWNER) and confirmed["confirmed_at"]
    assert confirmed["review"] == {"checks": CHECKS, "acknowledged_flags": draft["review_flags"]}
    assert confirmed["source"] == draft["source"], "the source reference is kept"
    actions = {row["action"] for row in get_memory_store().audit_logs.values()}
    assert {"email_draft.generate", "email_draft.update", "email_draft.confirm"} <= actions

    # Nothing was sent, and nothing can be.
    sent = post(client, f"{root(opportunity_id)}/{draft['id']}/send")
    assert sent.status_code == 400 and "EMAIL_SEND_FORBIDDEN" in sent.text
    assert load(client, opportunity_id)["send_status"] == "not_sent"

    # An edit after confirmation withdraws it until the owner reviews again.
    reopened = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 3, "lengths": {"medium": {"subject": "Edited after confirmation", "body": text["body"]}}}))["draft"]
    assert reopened["status"] == "draft" and reopened["confirmed_revision"] is None and reopened["confirmed_at"] is None and reopened["review"] is None
    not_confirmed = export(client, opportunity_id, reopened)
    assert not_confirmed.status_code == 409 and "EMAIL_NOT_CONFIRMED" in not_confirmed.text
    again = ok(confirm_email(client, opportunity_id, reopened, "short"))["draft"]
    assert again["status"] == "confirmed" and again["selected_length"] == "short" and again["confirmed_revision"] == 5


def test_attachments_are_the_approved_files_of_the_finalized_version(client: TestClient) -> None:
    base = finalized(client)
    opportunity_id, presentation_id = base["opportunity"], base["presentation"]
    draft = ok(generate(client, opportunity_id))["draft"]
    # The two V2 files come first; BAP-7 adds V1 and both Discovery documents after them.
    pptx, pdf = draft["attachments"][:2]
    assert [item["key"] for item in draft["attachments"]] == ["pptx", "pdf", "v1_pptx", "v1_pdf", "discovery1_docx", "discovery2_docx"]
    assert [item["selected"] for item in draft["attachments"][2:]] == [False] * 4, "only the V2 PDF is selected by default"
    assert (pptx["format"], pdf["format"]) == ("pptx", "pdf")
    assert pptx["file_name"] == "Master Presentation - Nordwind Maschinenbau - V2.pptx" and pdf["file_name"].endswith(" - V2.pdf")
    for item in (pptx, pdf):
        assert item["presentation_version_id"] == base["v2"] and item["presentation_id"] == presentation_id
        assert item["available"] is True and item["product_version"] == "V2"
    approved = {kind: download(client, presentation_id, base["v2"], kind) for kind in ("pptx", "pdf")}
    assert pptx["size_bytes"] == len(approved["pptx"]) and pdf["size_bytes"] == len(approved["pdf"])
    assert (pptx["selected"], pdf["selected"]) == (False, True), "the selection is stored, not implied"

    chosen = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 1, "attachments": {"pptx": True}}))["draft"]
    assert [item["selected"] for item in chosen["attachments"]][:2] == [True, True] and chosen["revision"] == 2
    assert [item["selected"] for item in load(client, opportunity_id)["attachments"]][:2] == [True, True]

    unconfirmed = export(client, opportunity_id, chosen)
    assert unconfirmed.status_code == 409 and "EMAIL_NOT_CONFIRMED" in unconfirmed.text
    confirmed = ok(confirm_email(client, opportunity_id, chosen, "extensive"))["draft"]
    stale = export(client, opportunity_id, confirmed, revision=confirmed["revision"] - 1)
    assert stale.status_code == 409 and "EMAIL_EXPORT_STALE" in stale.text

    response = export(client, opportunity_id, confirmed)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("message/rfc822")
    assert response.headers["content-disposition"] == 'attachment; filename="Nordwind Quoting - follow-up email.eml"'
    assert b"\r\n" in response.content and b"\n" not in response.content.replace(b"\r\n", b""), "CRLF line ends"
    message = email.message_from_bytes(response.content, policy=email.policy.default)
    assert message["MIME-Version"] == "1.0" and message["X-Unsent"] == "1"
    assert message["From"] == "Jörg Müller <joerg@borek.example>"
    assert message["To"] == "Dana Weber <dana.weber@nordwind.example>" and message["Cc"] == "Tom Keller <tom.keller@nordwind.example>"
    assert message["Subject"] == confirmed["lengths"]["extensive"]["subject"]
    assert message["Message-ID"].endswith("@borek.example>") and message["Date"]
    # Audit 2026-10-27: no internal ids in the message the owner sends to the client; the
    # application receives them with the download instead.
    assert not [name for name in message.keys() if name.lower().startswith("x-borek")]
    assert response.headers["X-Email-Draft-Id"] == confirmed["id"] and response.headers["X-Email-Draft-Revision"] == str(confirmed["revision"])
    assert response.headers["X-Email-Presentation-Version-Id"] == base["v2"]
    assert confirmed["id"] not in response.content.decode("ascii", "ignore") and base["v2"] not in response.content.decode("ascii", "ignore")
    assert b"=?utf-8?" in response.content.split(b"\r\n\r\n")[0].lower(), "non-ASCII header text is RFC 2047 encoded"
    body = message.get_body(preferencelist=("plain",))
    assert body.get_content_type() == "text/plain" and body.get_content_charset() == "utf-8"
    assert body["Content-Transfer-Encoding"] == "quoted-printable"
    assert "\n".join(body.get_content().splitlines()).strip() == confirmed["lengths"]["extensive"]["body"]
    files = {part.get_filename(): part for part in message.iter_attachments()}
    assert list(files) == ["Master Presentation - Nordwind Maschinenbau - V2.pptx", "Master Presentation - Nordwind Maschinenbau - V2.pdf"]
    assert files[pptx["file_name"]].get_content_type() == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    assert files[pdf["file_name"]].get_content_type() == "application/pdf"
    assert files[pptx["file_name"]].get_content() == approved["pptx"], "the exact approved PPTX bytes"
    assert files[pdf["file_name"]].get_content() == approved["pdf"], "the exact approved PDF bytes"
    assert hashlib.sha256(files[pdf["file_name"]].get_content()).hexdigest() == hashlib.sha256(approved["pdf"]).hexdigest()
    # Exporting changes nothing and sends nothing.
    after = load(client, opportunity_id)
    assert after == confirmed and after["send_status"] == "not_sent"

    # Only what is selected is attached.
    none = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": confirmed["revision"], "attachments": {"pptx": False, "pdf": False}}))["draft"]
    assert none["status"] == "draft", "changing the package withdraws the confirmation"
    plain = export(client, opportunity_id, ok(confirm_email(client, opportunity_id, none))["draft"])
    parsed = email.message_from_bytes(plain.content, policy=email.policy.default)
    assert not parsed.is_multipart() and list(parsed.iter_attachments()) == [] and "X-Email-Presentation-Version-Id" not in plain.headers


def test_a_missing_approved_file_is_reported_and_never_replaced(client: TestClient) -> None:
    base = finalized(client)
    opportunity_id = base["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]
    confirmed = ok(confirm_email(client, opportunity_id, draft))["draft"]
    version = get_memory_store().presentation_versions[UUID(base["v2"])]
    pdf_path = Path(version["pdf_storage_path"])
    kept = pdf_path.read_bytes()
    pdf_path.unlink()
    try:
        listed = load(client, opportunity_id)["attachments"]
        assert [(item["format"], item["available"], item["size_bytes"]) for item in listed][1] == ("pdf", False, None)
        assert listed[1]["selected"] is True and listed[1]["presentation_version_id"] == base["v2"]
        missing = export(client, opportunity_id, confirmed)
        assert missing.status_code == 409 and "EMAIL_ATTACHMENT_UNAVAILABLE" in missing.text
        reopened = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": confirmed["revision"], "selected_length": "short"}))["draft"]
        blocked = confirm_email(client, opportunity_id, reopened)
        assert blocked.status_code == 409 and "EMAIL_ATTACHMENT_UNAVAILABLE" in blocked.text
    finally:
        pdf_path.write_bytes(kept)


def test_drafts_and_exports_are_scoped_to_the_owner_and_the_opportunity(client: TestClient) -> None:
    mine = finalized(client)
    theirs = finalized(client, OTHER)
    my_draft = ok(generate(client, mine["opportunity"]))["draft"]
    their_draft = ok(generate(client, theirs["opportunity"], OTHER))["draft"]
    confirmed = ok(confirm_email(client, mine["opportunity"], my_draft))["draft"]

    assert get(client, f"{root(mine['opportunity'])}?journey_stage=deepening", OTHER).status_code == 404
    assert generate(client, mine["opportunity"], OTHER).status_code == 404
    assert patch(client, mine["opportunity"], my_draft["id"], {"expected_revision": confirmed["revision"], "selected_length": "short"}, OTHER).status_code == 404
    assert confirm_email(client, mine["opportunity"], confirmed, user=OTHER).status_code == 404
    assert export(client, mine["opportunity"], confirmed, OTHER).status_code == 404
    # A draft id from another opportunity is not found under mine, whoever asks.
    cross = patch(client, mine["opportunity"], their_draft["id"], {"expected_revision": 1, "selected_length": "short"})
    assert cross.status_code == 404 and "EMAIL_DRAFT_NOT_FOUND" in cross.text
    assert export(client, mine["opportunity"], their_draft).status_code == 404
    assert post(client, f"{root(mine['opportunity'])}/{their_draft['id']}/confirm", {"selected_length": "short"}).status_code == 404
    assert load(client, mine["opportunity"]) == confirmed
    assert their_draft["source"]["presentation_version_id"] == theirs["v2"] != mine["v2"]


def test_other_stages_and_standalone_ppt2_opportunities_keep_their_drafts(client: TestClient) -> None:
    """first_contact, concretisation and a transcript-based deepening draft are unchanged in substance."""
    import io

    created = client.post(
        "/opportunities",
        headers=headers(),
        json={"client_name": "Acme", "opportunity_name": "Invoice pilot", "department": "Sales", "followup_statics": STATICS},
    )
    opportunity_id = ok(created, 201)["id"]
    first = ok(post(client, f"{root(opportunity_id)}/generate", {"journey_stage": "first_contact"}))["draft"]
    assert first["source"] is None and first["attachments"] == [] and first["source_status"] == "not_applicable"
    assert first["revision"] == 1 and "First contact" in first["lengths"]["short"]["subject"]
    # The earlier confirm call - a length and nothing else - still works.
    plain = ok(post(client, f"{root(opportunity_id)}/{first['id']}/confirm", {"selected_length": "short"}))["draft"]
    assert plain["status"] == "confirmed" and plain["revision"] == 2 and plain["review"] is None
    exported = get(client, f"{root(opportunity_id)}/{first['id']}/export?revision=2")
    assert exported.status_code == 200 and not email.message_from_bytes(exported.content, policy=email.policy.default).is_multipart()
    attach = client.patch(f"{root(opportunity_id)}/{first['id']}", headers=headers(), json={"expected_revision": 2, "attachments": {"pdf": True}})
    assert attach.status_code == 400 and "EMAIL_ATTACHMENT_UNAVAILABLE" in attach.text, "no approved version, nothing to attach"

    # Deepening without a Master Presentation: the transcript-based draft, as before.
    uploaded = client.post(
        f"/opportunities/{opportunity_id}/transcripts",
        headers=headers(),
        files={"file": ("meeting.txt", io.BytesIO(b"Markus: We will send the sample invoices by 12.10.2026.\nLena: Decision: Start with one entity.\n"), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    legacy = ok(post(client, f"{root(opportunity_id)}/generate", {"journey_stage": "deepening"}))["draft"]
    assert legacy["source"] is None and legacy["attachments"] == [] and legacy["review_flags"] == []
    assert "Follow-up" in legacy["lengths"]["medium"]["subject"] and legacy["lengths"]["medium"]["body"].startswith("Hi Dana,")
    assert ok(post(client, f"{root(opportunity_id)}/{legacy['id']}/confirm", {"selected_length": "medium"}))["draft"]["status"] == "confirmed"
    assert post(client, f"{root(opportunity_id)}/generate", {"journey_stage": "concretisation"}).status_code in (200, 400, 403)


def test_drafts_of_different_stages_are_saved_side_by_side(client: TestClient) -> None:
    """A save of one stage never replaces another stage's draft, whatever happens at the same moment."""
    opportunity_id = finalized(client)["opportunity"]
    deepening = ok(generate(client, opportunity_id))["draft"]
    first = ok(post(client, f"{root(opportunity_id)}/generate", {"journey_stage": "first_contact"}))["draft"]
    assert first["id"] != deepening["id"] and load(client, opportunity_id) == deepening, "generating one stage leaves the other untouched"

    def first_contact() -> dict:
        return ok(get(client, f"{root(opportunity_id)}?journey_stage=first_contact"))["draft"]

    for round_number in range(6):
        deepening, first = load(client, opportunity_id), first_contact()
        barrier = threading.Barrier(2)
        answers: dict[str, int] = {}
        text = deepening["lengths"]["short"]

        def edit_deepening() -> None:
            barrier.wait()
            body = {"expected_revision": deepening["revision"], "lengths": {"short": {"subject": f"Deepening round {round_number}", "body": text["body"]}}}
            answers["deepening"] = patch(client, opportunity_id, deepening["id"], body).status_code

        def change_first_contact() -> None:
            barrier.wait()
            if round_number % 3 == 0:  # confirm
                response = post(client, f"{root(opportunity_id)}/{first['id']}/confirm", {"selected_length": "short", "expected_revision": first["revision"]})
            elif round_number % 3 == 1:  # save
                body = {"expected_revision": first["revision"], "lengths": {"medium": {"subject": f"First contact round {round_number}", "body": first["lengths"]["medium"]["body"]}}}
                response = patch(client, opportunity_id, first["id"], body)
            else:  # regenerate
                response = post(client, f"{root(opportunity_id)}/generate", {"journey_stage": "first_contact", "overwrite_edits": True})
            answers["first_contact"] = response.status_code

        threads = [threading.Thread(target=edit_deepening), threading.Thread(target=change_first_contact)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert answers == {"deepening": 200, "first_contact": 200}, (round_number, answers)
        after_deepening, after_first = load(client, opportunity_id), first_contact()
        assert after_deepening["revision"] == deepening["revision"] + 1 and after_deepening["lengths"]["short"]["subject"] == f"Deepening round {round_number}"
        assert after_first["revision"] == first["revision"] + 1, "the other stage's change is stored as well"
        if round_number % 3 == 0:
            assert after_first["status"] == "confirmed"
        elif round_number % 3 == 1:
            assert after_first["lengths"]["medium"]["subject"] == f"First contact round {round_number}"
        else:
            assert after_first["status"] == "draft" and after_first["lengths"]["medium"]["edited"] is False
    assert load(client, opportunity_id)["source"]["kind"] == "master_presentation_v2", "the pinned source survived every round"


def test_two_api_processes_save_atomically_per_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Separate store instances over one database: the function of migration 043 decides.

    The store sends one stage's draft and the revision it expects. It never sends the whole
    ``email_drafts`` object back, so what it read earlier cannot replace another stage's change.
    """
    from app.services.data.supabase_store import SupabaseDataStore

    opportunity_id, user_id = uuid4(), uuid4()
    row = {"id": str(opportunity_id), "created_by": str(user_id), "pitch_owner_id": str(user_id), "client_name": "Acme", "opportunity_name": "Pilot", "email_drafts": None,
           "created_at": "2026-10-09T08:00:00+00:00", "updated_at": "2026-10-09T08:00:00+00:00"}
    guard = threading.Lock()
    calls: list[dict] = []
    snapshots: list[dict] = []  # what a process read before it wrote

    def request(self, method, name, *, json_body=None, params=None, headers=None):
        assert (method, name) == ("POST", "rpc/save_email_draft"), "no PATCH of the whole email_drafts object"
        assert set(json_body) == {"p_opportunity_id", "p_journey_stage", "p_expected_revision", "p_draft"}
        with guard:  # the UPDATE in save_email_draft locks the row: one statement at a time
            calls.append(copy.deepcopy(json_body))
            stage, expected = json_body["p_journey_stage"], json_body["p_expected_revision"]
            drafts = row["email_drafts"] if isinstance(row["email_drafts"], dict) else {}
            existing = drafts.get(stage)
            matches = existing is None if expected is None else (existing is not None and int(existing.get("revision") or 1) == expected)
            if json_body["p_opportunity_id"] != row["id"] or not matches:
                return httpx.Response(200, json=None)
            row["email_drafts"] = {**drafts, stage: copy.deepcopy(json_body["p_draft"])}  # jsonb_set on the current value
            return httpx.Response(200, json=copy.deepcopy(json_body["p_draft"]))

    def fetch(self, _id):
        with guard:
            snapshot = copy.deepcopy(row)
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(SupabaseDataStore, "_request", request)
    monkeypatch.setattr(SupabaseDataStore, "_fetch_opportunity_row", fetch)
    monkeypatch.setattr("app.services.pitch_owner.user_can_access_opportunity", lambda _row, _user: True)
    stores = [SupabaseDataStore(f"token-of-process-{index}") for index in range(6)]
    lengths = {name: {"subject": "s", "body": "b", "word_count": 1} for name in ("short", "medium", "extensive")}

    def save(store: SupabaseDataStore, stage: str, expected: int | None, **extra) -> object:
        try:
            return store.save_email_draft(opportunity_id=opportunity_id, user_id=user_id, journey_stage=stage,
                                          draft={"status": "draft", "selected_length": None, "lengths": lengths, **extra}, expected_revision=expected)
        except HTTPException as exc:
            return exc

    def race(jobs: list[tuple[str, int | None]]) -> list[object]:
        barrier = threading.Barrier(len(jobs))
        outcomes: list[object] = [None] * len(jobs)

        def run(index: int) -> None:
            barrier.wait()
            outcomes[index] = save(stores[index % len(stores)], jobs[index][0], jobs[index][1], editor=index)

        threads = [threading.Thread(target=run, args=(index,)) for index in range(len(jobs))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return outcomes

    # The same draft: one of six processes wins, at creation and at every later revision.
    created = race([("deepening", None)] * 6)
    winners = [item for item in created if isinstance(item, dict)]
    assert len(winners) == 1 and all(getattr(item, "status_code", None) == 409 for item in created if not isinstance(item, dict))
    assert row["email_drafts"]["deepening"]["revision"] == 1 and row["email_drafts"]["deepening"]["editor"] == winners[0]["editor"]
    assert calls[0]["p_expected_revision"] is None, "the first write requires that no draft exists"
    updated = race([("deepening", 1)] * 6)
    winners = [item for item in updated if isinstance(item, dict)]
    assert len(winners) == 1 and winners[0]["revision"] == 2 and row["email_drafts"]["deepening"]["editor"] == winners[0]["editor"]
    stale = save(stores[0], "deepening", 1)
    assert getattr(stale, "status_code", None) == 409 and "EMAIL_DRAFT_CONFLICT" in str(stale.detail)

    # Different stages at the same moment: every one of them is stored.
    both = race([("first_contact", None), ("deepening", 2), ("concretisation", None)])
    assert all(isinstance(item, dict) for item in both), both
    assert {stage: draft["revision"] for stage, draft in row["email_drafts"].items()} == {"deepening": 3, "first_contact": 1, "concretisation": 1}
    for _round in range(5):
        revisions = {stage: draft["revision"] for stage, draft in row["email_drafts"].items()}
        outcome = race([("first_contact", revisions["first_contact"]), ("deepening", revisions["deepening"]), ("concretisation", revisions["concretisation"])])
        assert all(isinstance(item, dict) for item in outcome)
        assert {stage: draft["revision"] for stage, draft in row["email_drafts"].items()} == {stage: number + 1 for stage, number in revisions.items()}

    # The lost update, step by step: a process reads, another stage is saved, then the first one writes.
    snapshots.clear()
    stale_view = copy.deepcopy(row)
    assert isinstance(save(stores[1], "first_contact", row["email_drafts"]["first_contact"]["revision"], note="saved in between"), dict)
    monkeypatch.setattr(SupabaseDataStore, "_fetch_opportunity_row", lambda self, _id: copy.deepcopy(stale_view))
    late = save(stores[2], "deepening", stale_view["email_drafts"]["deepening"]["revision"], note="written from an old read")
    assert isinstance(late, dict), "its own stage had not changed, so the save is accepted"
    assert row["email_drafts"]["first_contact"]["note"] == "saved in between", "the other stage's newer draft is still there"
    assert row["email_drafts"]["deepening"]["note"] == "written from an old read"
    assert "email_drafts" not in calls[-1] and calls[-1]["p_journey_stage"] == "deepening"

    migration = (Path(__file__).resolve().parents[3] / "apps/services/api/supabase/migrations/043_email_draft_atomic_save.sql").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION public.save_email_draft(" in migration and "SECURITY INVOKER" in migration
    assert "jsonb_set(" in migration and "ARRAY[p_journey_stage]" in migration, "only the named stage is replaced"
    assert "COALESCE((email_drafts -> p_journey_stage ->> 'revision')::integer, 1) = p_expected_revision" in migration
    assert "GRANT EXECUTE ON FUNCTION public.save_email_draft(UUID, TEXT, INTEGER, JSONB) TO authenticated, service_role" in migration
    assert "SECURITY DEFINER" not in migration and "CREATE TABLE" not in migration and "CREATE POLICY" not in migration.upper()


def test_a_borek_employees_statements_are_not_reported_as_the_clients(client: TestClient) -> None:
    """The transcript holds what BOREK said as well. Being in the transcript is not the client saying it."""
    meeting = (
        "Lena Hoffmann (BOREK): Requirement: We recommend a fixed-price pilot of six weeks.\n"
        "Lena Hoffmann (BOREK): Priority: Borek suggests starting with the service team.\n"
        "Lena Hoffmann (BOREK): Decision: Borek will prepare a proposal for a pilot.\n"
        "Lena Hoffmann (BOREK): Follow-up: Borek will send the proposal by 16.10.2026.\n"
        "Dana: Challenge: Pricing data sits in three systems.\n"
    )
    opportunity_id = finalized(client, meeting=meeting, notes=None, excluded={})["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]
    assert "speaker_unverified" in draft["review_flags"]
    for name in ("short", "medium", "extensive"):
        body = draft["lengths"][name]["body"]
        assert "e recommend a fixed-price pilot of six weeks" in body, "the statement is reported"
        frame = body
        for said in ("e recommend a fixed-price pilot of six weeks", "Borek suggests starting with the service team",
                     "Borek will prepare a proposal for a pilot", "Borek will send the proposal by 16.10.2026", "ricing data sits in three systems"):
            frame = frame.replace(said, "")
        for phrase in ("Your ", "you said", "you described", "you raised", "What we heard", "we agreed", "agreed"):
            assert phrase not in frame, (name, phrase)
    extensive = draft["lengths"]["extensive"]["body"]
    assert "The core requirement, as we understood it: we recommend a fixed-price pilot of six weeks." in extensive
    assert "this is the direction that was discussed: Borek will prepare a proposal for a pilot." in extensive
    assert "Your requirements" not in extensive and "Your priorities" not in extensive


def test_each_review_flag_has_to_be_acknowledged_by_name(client: TestClient) -> None:
    opportunity_id = finalized(client)["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]
    flags = draft["review_flags"]
    assert len(flags) >= 4
    url = f"{root(opportunity_id)}/{draft['id']}/confirm"
    base = {"selected_length": "medium", "expected_revision": draft["revision"], "review_checks": CHECKS}

    # Nothing sent, some sent, everything but one, and names the draft does not have: all refused.
    for sent in (None, [], flags[:1], flags[:-1], ["something_else", "fixture_extraction_"], [flag.upper() for flag in flags]):
        refused = post(client, url, {**base, **({} if sent is None else {"acknowledged_flags": sent})})
        assert refused.status_code == 400 and "EMAIL_REVIEW_FLAGS_OPEN" in refused.text, (sent, refused.text)
        missing = [flag for flag in flags if flag not in (sent or [])]
        assert all(flag in refused.text for flag in missing), "the answer names every flag that is still open"
        assert load(client, opportunity_id)["status"] == "draft"
    # The checklist item "review flags" does not stand in for the flags themselves.
    assert "review_flags" in CHECKS
    # Every flag named: accepted, in any order, and the stored review lists exactly the draft's flags.
    confirmed = ok(post(client, url, {**base, "acknowledged_flags": [*reversed(flags), "something_else"]}))["draft"]
    assert confirmed["status"] == "confirmed" and confirmed["review"]["acknowledged_flags"] == flags
    assert confirmed["send_status"] == "not_sent"


def test_a_finding_too_long_for_a_draft_is_refused_and_never_shortened(client: TestClient) -> None:
    long_requirement = " ".join(["Requirement:", *(f"word{index}" for index in range(170))]) + " end."
    opportunity_id = finalized(client, meeting=f"Dana: {long_requirement}\n", notes=None, excluded={})["opportunity"]
    refused = generate(client, opportunity_id)
    assert refused.status_code == 400 and "FOLLOWUP_CONTENT_TOO_LONG" in refused.text, refused.text
    assert "short draft (150 content words)" in refused.text
    assert load(client, opportunity_id) is None, "no draft over its limit, and no shortened statement, is stored"

    # With statements that fit, the long one is left out of the drafts it cannot be part of - whole.
    meeting = f"Dana: {long_requirement}\nDana: Requirement: Quotes must go out within one day.\nTom: Decision: Run a pilot with ten quotes.\n"
    other = finalized(client, meeting=meeting, notes=None, excluded={})["opportunity"]
    draft = ok(generate(client, other))["draft"]
    limits = draft["word_limits"]
    for name in ("short", "medium", "extensive"):
        text = draft["lengths"][name]
        assert text["word_count"] <= limits[name]
        assert ("word0 " in text["body"]) == ("word169 end" in text["body"]), "whole or absent"
    assert "word0" not in draft["lengths"]["short"]["body"] and "uotes must go out within one day" in draft["lengths"]["short"]["body"]
    assert "word169 end" in draft["lengths"]["extensive"]["body"]


def test_the_text_only_says_attached_while_a_file_is_selected(client: TestClient) -> None:
    opportunity_id = finalized(client)["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]
    assert [item["selected"] for item in draft["attachments"]][:2] == [False, True]
    assert all("attached" in draft["lengths"][name]["body"] for name in ("short", "medium", "extensive"))

    # The owner rewrites the medium email, keeping the sentence about the attachment in their own words.
    mine = draft["lengths"]["medium"]["body"].replace("Please find attached the presentation,", "As discussed, I have attached the presentation,")
    edited = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 1, "lengths": {"medium": {"subject": draft["lengths"]["medium"]["subject"], "body": mine}}}))["draft"]

    # No file selected: the generated texts no longer claim an attachment. The owner's text is not touched.
    none = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 2, "attachments": {"pdf": False}}))["draft"]
    assert [item["selected"] for item in none["attachments"]][:2] == [False, False] and none["revision"] == 3
    for name in ("short", "extensive"):
        text = none["lengths"][name]
        assert "attach" not in text["body"].lower() and text["edited"] is False, name
        assert text["word_count"] < draft["lengths"][name]["word_count"]
        # Only that sentence went: every other paragraph is the one generated before.
        assert set(text["body"].split("\n\n")) < set(draft["lengths"][name]["body"].split("\n\n"))
    assert none["lengths"]["medium"] == edited["lengths"]["medium"], "an edited text is never rewritten for the owner"
    assert "I have attached the presentation" in none["lengths"]["medium"]["body"]
    assert none["review_flags"] == draft["review_flags"]
    assert load(client, opportunity_id) == none

    # A file is selected again: the sentence returns to the generated texts, exactly as before.
    again = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 3, "attachments": {"pptx": True}}))["draft"]
    for name in ("short", "extensive"):
        assert again["lengths"][name] == draft["lengths"][name], name
    assert again["lengths"]["medium"] == edited["lengths"]["medium"]
    # Selecting a second file changes no text.
    both = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 4, "attachments": {"pdf": True}}))["draft"]
    assert both["lengths"] == again["lengths"] and both["revision"] == 5

    # A new draft is written for the selection it finds: without a file, without the sentence.
    ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 5, "attachments": {"pptx": False, "pdf": False}}))
    fresh = ok(generate(client, opportunity_id, overwrite_edits=True))["draft"]
    assert [item["selected"] for item in fresh["attachments"]][:2] == [False, False]
    assert all("attach" not in fresh["lengths"][name]["body"].lower() for name in ("short", "medium", "extensive"))
    exported = export(client, opportunity_id, ok(confirm_email(client, opportunity_id, fresh))["draft"])
    message = email.message_from_bytes(exported.content, policy=email.policy.default)
    assert "attach" not in message.get_content().lower() and list(message.iter_attachments()) == []


# ----------------------------------------------------------------------------- AI writer stage

AI_PARAGRAPHS = [
    {"kind": "opening", "sentences": [{"text": "Thank you for the open conversation.", "fact_ids": []}]},
    {"kind": "context", "sentences": [
        {"text": "Quotes need to go out within one day, while pricing data currently sits in three systems.", "fact_ids": ["R1", "C1"]},
        {"text": "The sales team is the natural place to start, and a pilot with ten quotes was noted as the way forward.", "fact_ids": ["P1", "D1"]},
    ]},
    {"kind": "contribution", "sentences": [
        {"text": "One idea we discussed is a drafting assistant that prepares the quote from the request; it is an option to explore and not yet decided.", "fact_ids": ["S1"]},
    ]},
    {"kind": "closing", "sentences": [{"text": "Please let me know if I have understood anything differently.", "fact_ids": []}]},
]


def ai_draft(**changes) -> dict:
    draft = {name: {"paragraphs": copy.deepcopy(AI_PARAGRAPHS)} for name in ("short", "medium", "extensive")}
    for name in draft:
        for index, sentence in changes.items():
            draft[name]["paragraphs"][int(index)]["sentences"][0] = sentence
    return draft


def approve_all(user: str) -> dict:
    import re

    found = re.findall(r"^(short|medium|extensive) #(\d+) \[", user, re.M)
    return {"verdicts": [{"length": length, "index": int(index), "supported": True, "issue": ""} for length, index in found]}


def live(monkeypatch: pytest.MonkeyPatch, writer, verifier=approve_all) -> dict:
    """AI writing on, with the model replaced by scripted answers. Returns the call counters."""
    calls = {"writer": 0, "verifier": 0, "users": []}

    def model_call(*, stage: str, **_kwargs):
        def complete(system: str, user: str, schema: dict) -> dict:
            role = "writer" if stage == "followup_email_writer" else "verifier"
            calls[role] += 1
            calls["users"].append(user)
            answer = writer if role == "writer" else verifier
            if isinstance(answer, Exception):
                raise answer
            return answer(user) if callable(answer) else copy.deepcopy(answer)

        return complete

    monkeypatch.setattr(email_service, "ai_writing_enabled", lambda: True)
    monkeypatch.setattr(email_service, "_model_call", model_call)
    return calls


def test_live_mode_stores_the_verified_ai_text_and_says_so(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    opportunity_id = finalized(client)["opportunity"]
    calls = live(monkeypatch, ai_draft())
    draft = ok(generate(client, opportunity_id))["draft"]
    assert calls["writer"] == 1 and calls["verifier"] == 1
    assert draft["writer"] == {
        "mode": "ai", "model": draft["writer"]["model"], "prompt_version": "followup-email-writer:v1", "verified": True, "fallback_reason": None, "checked_sentences": 15,
    }
    assert draft["writer"]["model"]
    for name in ("short", "medium", "extensive"):
        body = draft["lengths"][name]["body"]
        assert body.startswith("Hi Dana,\n\nThank you for the open conversation.\n\nQuotes need to go out within one day, while pricing data currently sits in three systems. The sales team")
        assert "as we understood" not in body and body.endswith("Best regards\nJörg Müller\nManaging Partner - BOREK")
        assert "attached" in body, "a file is selected, so the code added the attachment sentence"
        assert draft["lengths"][name]["word_count"] <= draft["word_limits"][name] and draft["lengths"][name]["edited"] is False
    assert "ai_written" in draft["review_flags"] and "ai_fallback" not in draft["review_flags"] and "content_trimmed" not in draft["review_flags"]
    assert {"fixture_extraction", "speaker_unverified", "owner_notes_omitted"} <= set(draft["review_flags"]), "what the owner has to check about the findings is unchanged"
    # Only the confirmed findings went to the model: no transcript line, note, recipient or sender.
    sent = "\n".join(calls["users"])
    assert "R1 [requirement] Quotes must go out within one day." in sent and "CLIENT: Nordwind" in sent
    for private in ("Dana", "Weber", "Tom Keller", "Jörg", "joerg@", "trade fair", OBSERVATION, EXCLUDED, "Tom:", "Requirement:"):
        assert private not in sent, private
    # The sentence-level record is kept with the stored draft, not sent to the browser.
    stored = get_memory_store().get_email_draft(opportunity_id=UUID(opportunity_id), user_id=OWNER, journey_stage="deepening")
    record = stored["writer"]["verification"]
    assert record["sentence_count"] == 15 and record["cited_fact_ids"] == ["C1", "D1", "P1", "R1", "S1"]
    assert "writer_variants" not in draft and "verification" not in draft["writer"]
    assert load(client, opportunity_id) == draft

    # Changing the attachment selection uses the text that was verified for it: no new model call.
    none = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 1, "attachments": {"pdf": False}}))["draft"]
    assert all("attach" not in none["lengths"][name]["body"].lower() for name in none["lengths"]) and none["writer"] == draft["writer"]
    back = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 2, "attachments": {"pdf": True}}))["draft"]
    assert back["lengths"] == draft["lengths"] and calls["writer"] == 1 and calls["verifier"] == 1
    # It is reviewed, confirmed and exported like any other draft, and nothing is sent.
    confirmed = ok(confirm_email(client, opportunity_id, back))["draft"]
    assert confirmed["status"] == "confirmed" and confirmed["send_status"] == "not_sent" and "ai_written" in confirmed["review"]["acknowledged_flags"]
    message = email.message_from_bytes(export(client, opportunity_id, confirmed).content, policy=email.policy.default)
    assert "Quotes need to go out within one day" in message.get_body().get_content() and message["X-Unsent"] == "1"


@pytest.mark.parametrize(
    ("writer", "verifier", "code", "calls_expected"),
    [
        (ClaudeClientError("Claude is not reachable right now.", code="PROVIDER_UNAVAILABLE"), approve_all, "PROVIDER_UNAVAILABLE", (1, 0)),
        (ClaudeClientError("ANTHROPIC_API_KEY is not set."), approve_all, "AI_UNAVAILABLE", (1, 0)),
        (ai_draft(**{"3": {"text": "We will deliver a drafting assistant by Friday for 40,000 euros.", "fact_ids": ["S1"]}}), approve_all, "AI_GROUNDING_REJECTED", (2, 0)),
        (ai_draft(), lambda user: {"verdicts": [{**item, "supported": False, "issue": "not in the facts"} for item in approve_all(user)["verdicts"]]}, "AI_GROUNDING_REJECTED", (2, 2)),
        (RuntimeError("boom"), approve_all, "AI_WRITER_FAILED", (1, 0)),
    ],
    ids=["model-unavailable", "no-credentials", "rules-reject", "verifier-rejects", "unexpected-error"],
)
def test_a_failed_or_rejected_ai_draft_falls_back_and_is_labelled(client: TestClient, monkeypatch: pytest.MonkeyPatch, writer, verifier, code: str, calls_expected: tuple) -> None:
    opportunity_id = finalized(client)["opportunity"]
    calls = live(monkeypatch, writer, verifier)
    draft = ok(generate(client, opportunity_id))["draft"]
    assert (calls["writer"], calls["verifier"]) == calls_expected
    written_by = draft["writer"]
    assert (written_by["mode"], written_by["verified"], written_by["checked_sentences"]) == ("fallback", False, None)
    assert written_by["fallback_reason"]["code"] == code and written_by["fallback_reason"]["message"]
    assert "ai_fallback" in draft["review_flags"] and "ai_written" not in draft["review_flags"]
    everything = str(draft)
    # The owner gets the rule-based text, and nothing of the rejected draft.
    assert "as we understood" in draft["lengths"]["medium"]["body"]
    for rejected in ("40,000", "by Friday", "We will deliver", "Thank you for the open conversation."):
        assert rejected not in everything, rejected
    assert "boom" not in everything, "an internal error is not shown"
    # The fallback is a complete draft: it can be reviewed and confirmed, with the fallback acknowledged by name.
    refused = post(client, f"{root(opportunity_id)}/{draft['id']}/confirm", {
        "selected_length": "medium", "expected_revision": draft["revision"], "review_checks": CHECKS,
        "acknowledged_flags": [flag for flag in draft["review_flags"] if flag != "ai_fallback"],
    })
    assert refused.status_code == 400 and "ai_fallback" in refused.text
    assert ok(confirm_email(client, opportunity_id, draft))["draft"]["status"] == "confirmed"


def test_fixture_mode_never_calls_a_model_and_is_labelled_rule_based(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_model(**_kwargs):
        raise AssertionError("no model call in fixture mode")

    monkeypatch.setattr(email_service, "_model_call", no_model)
    opportunity_id = finalized(client)["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]
    assert draft["writer"] == {"mode": "deterministic", "model": None, "prompt_version": "followup-finalized:v3", "verified": False, "fallback_reason": None, "checked_sentences": None}
    assert "ai_written" not in draft["review_flags"] and "ai_fallback" not in draft["review_flags"]
    # Regenerating in live mode replaces it with AI text; saved edits are still protected.
    calls = live(monkeypatch, ai_draft())
    kept = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 1, "lengths": {"short": {"subject": "My subject", "body": draft["lengths"]["short"]["body"] + "\nP.S. Mine."}}}))["draft"]
    assert generate(client, opportunity_id).status_code == 409 and calls["writer"] == 0, "no model call before the owner agrees to replace the draft"
    fresh = ok(generate(client, opportunity_id, overwrite_edits=True))["draft"]
    assert fresh["writer"]["mode"] == "ai" and fresh["revision"] == kept["revision"] + 1 and "P.S. Mine." not in fresh["lengths"]["short"]["body"]


def test_an_email_that_announces_a_missing_attachment_cannot_be_confirmed(client: TestClient) -> None:
    opportunity_id = finalized(client)["opportunity"]
    draft = ok(generate(client, opportunity_id))["draft"]
    mine = draft["lengths"]["medium"]["body"].replace("Please find attached the presentation,", "As discussed, I have attached the presentation,")
    ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 1, "lengths": {"medium": {"subject": draft["lengths"]["medium"]["subject"], "body": mine}}}))
    none = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": 2, "attachments": {"pdf": False}}))["draft"]
    assert "I have attached the presentation" in none["lengths"]["medium"]["body"], "the owner's text is not rewritten"

    # Every check ticked and every flag acknowledged: it is still not confirmed.
    refused = confirm_email(client, opportunity_id, none, "medium")
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "EMAIL_ATTACHMENT_CONTRADICTION", refused.text
    assert load(client, opportunity_id)["status"] == "draft"
    # The other lengths were generated without the sentence and can be confirmed.
    assert ok(confirm_email(client, opportunity_id, none, "short"))["draft"]["status"] == "confirmed"

    # Resolved by the owner, either way: the sentence is changed ...
    current = load(client, opportunity_id)
    reworded = mine.replace("As discussed, I have attached the presentation,", "No attachment this time; the presentation follows separately,")
    fixed = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": current["revision"], "lengths": {"medium": {"subject": draft["lengths"]["medium"]["subject"], "body": reworded}}}))["draft"]
    assert ok(confirm_email(client, opportunity_id, fixed, "medium"))["draft"]["status"] == "confirmed", "saying that nothing is attached is not a contradiction"
    # ... or the file is selected.
    current = load(client, opportunity_id)
    again = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": current["revision"], "lengths": {"medium": {"subject": draft["lengths"]["medium"]["subject"], "body": mine}}}))["draft"]
    assert confirm_email(client, opportunity_id, again, "medium").status_code == 400
    selected = ok(patch(client, opportunity_id, draft["id"], {"expected_revision": again["revision"], "attachments": {"pdf": True}}))["draft"]
    confirmed = ok(confirm_email(client, opportunity_id, selected, "medium"))["draft"]
    assert confirmed["status"] == "confirmed" and [item["selected"] for item in confirmed["attachments"]][:2] == [False, True]



def test_no_request_is_made_with_missing_placeholder_or_refused_credentials(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """AI writing on, the real model path in place: the preflight decides before anything is sent."""
    requests: list[str] = []

    def provider(*_args, **_kwargs):
        requests.append("request")
        raise ClaudeClientError("Claude rejected the API key. Check ANTHROPIC_API_KEY in .env.", code="CLAUDE_AUTH", retryable=False)

    monkeypatch.setattr(email_service, "ai_writing_enabled", lambda: True)
    monkeypatch.setattr(email_service, "structured_complete", provider)
    monkeypatch.setattr(email_service, "_REFUSED_KEYS", set())
    opportunity_id = finalized(client)["opportunity"]

    def written_by() -> dict:
        return ok(generate(client, opportunity_id, overwrite_edits=True))["draft"]["writer"]

    # The test suite's own placeholder key, an empty key and a malformed key: no request at all.
    for key, code in ((None, "CLAUDE_CREDENTIALS_PLACEHOLDER"), ("", "CLAUDE_CREDENTIALS_MISSING"), ("sk-ant-short", "CLAUDE_CREDENTIALS_INVALID")):
        if key is not None:
            monkeypatch.setenv("ANTHROPIC_API_KEY", key)
        writer = written_by()
        assert (writer["mode"], writer["fallback_reason"]["code"]) == ("fallback", code) and requests == []
        assert "no model was called" in writer["fallback_reason"]["message"]

    # A key of the right form that the provider refuses: asked once, then never again in this process.
    refused = "sk-ant-api03-" + "A1b2C3d4-_" * 8
    monkeypatch.setenv("ANTHROPIC_API_KEY", refused)
    assert email_service.model_preflight() is None
    assert written_by()["fallback_reason"]["code"] == "CLAUDE_AUTH" and requests == ["request"]
    for _again in range(3):
        writer = written_by()
        assert writer["fallback_reason"]["code"] == "CLAUDE_AUTH" and "rejected earlier" in writer["fallback_reason"]["message"]
    assert requests == ["request"], "one refused request, however often the email is generated"
    assert refused not in str(writer) and refused[:12] not in str(email_service._REFUSED_KEYS), "neither the key nor a readable part of it is kept or shown"
    # A different key is a new situation and is tried.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "Z9y8X7w6-_" * 8)
    assert email_service.model_preflight() is None
    written_by()
    assert requests == ["request", "request"]


def test_ai_writing_needs_its_own_switch_and_live_mode(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    assert settings.FOLLOWUP_AI_WRITER_ENABLED is False, "off unless an environment switches it on"
    for switch, mode, expected in ((False, "fixture", False), (True, "fixture", False), (False, "live", False), (True, "live", True)):
        monkeypatch.setattr(settings, "FOLLOWUP_AI_WRITER_ENABLED", switch)
        monkeypatch.setattr(settings, "AI_EXECUTION_MODE", mode)
        assert email_service.ai_writing_enabled() is expected, (switch, mode)

    # A live environment without the switch: the rule-based writer, labelled as such, and no model call.
    def no_model(**_kwargs):
        raise AssertionError("no model call without FOLLOWUP_AI_WRITER_ENABLED")

    monkeypatch.setattr(email_service, "_model_call", no_model)
    monkeypatch.setattr(settings, "FOLLOWUP_AI_WRITER_ENABLED", False)
    monkeypatch.setattr(settings, "AI_EXECUTION_MODE", "fixture")  # the package is prepared without any model
    opportunity_id = finalized(client)["opportunity"]
    monkeypatch.setattr(settings, "AI_EXECUTION_MODE", "live")  # only the email is generated in a live environment
    draft = ok(generate(client, opportunity_id))["draft"]
    assert draft["writer"]["mode"] == "deterministic" and draft["writer"]["verified"] is False and draft["writer"]["model"] is None
    assert "ai_written" not in draft["review_flags"] and "ai_fallback" not in draft["review_flags"]
    assert ok(confirm_email(client, opportunity_id, draft))["draft"]["status"] == "confirmed"
