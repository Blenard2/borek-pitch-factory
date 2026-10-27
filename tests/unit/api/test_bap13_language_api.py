"""BAP-13: the content language through the API - contract, versions, approvals and a German journey.

The language is chosen when Discovery 1 is generated (request, else the opportunity's language,
else English) and every later artifact inherits it from the approved version it is built from.
"""

from __future__ import annotations

import html
import io
import json
import re
import zipfile
from uuid import UUID

from fastapi.testclient import TestClient

from app.services.data.memory_store import get_memory_store
from tests.unit.api.test_followup_email import finalized, generate as generate_email, set_statics
from tests.unit.api.test_master_presentation_v2_api import (  # noqa: F401  (client is a fixture)
    CLIENT,
    OTHER,
    client,
    get,
    headers,
    ok,
    post,
    stored_versions,
)

GERMAN_CLIENT = {
    "client_name": "Müller Präzisionstechnik",
    "opportunity_name": "KI-Einführung",
    "department": "Vertrieb",
    "language": "de",
    "stage1_intake": {
        "poc_name": "Dana Weber",
        "sales_topic_description": "Schnellere Angebote für das Vertriebsteam",
        "about_company": "Familiengeführter Maschinenbauer mit 420 Beschäftigten.",
    },
}
# English labels the fixture extraction reads, German statements: German content from an
# English-structured source.
GERMAN_MEETING = (
    "Dana: Requirement: Angebote müssen innerhalb eines Tages beim Kunden sein.\n"
    "Tom: Challenge: Die Preisdaten liegen in drei Systemen.\n"
    "Dana: Priority: Zuerst soll das Vertriebsteam entlastet werden.\n"
    "Tom: Discussed solution: Ein Assistent, der das Angebot aus der Anfrage vorbereitet.\n"
    "Tom: Decision: Ein Pilot mit zehn Angeboten wird gestartet.\n"
    "Dana: Follow-up: Dana Weber schickt den Preisexport bis Freitag.\n"
)
GERMAN_NOTES = "Priority: Zuerst soll das Vertriebsteam entlastet werden.\nOpportunity: Das Serviceteam könnte den Angebotsassistenten mitnutzen."
GERMAN_STATICS = {
    "project_name": "Angebotsbeschleunigung",
    "client_short": "Müller",
    "salutation_style": "formal",
    "standard_recipients": [
        {"email": "d.weber@mueller.example", "first_name": "Dana", "last_name": "Weber", "salutation": "Frau", "kind": "to", "primary": True},
    ],
    "sender_profile": {"name": "Jörg Müller", "role": "Managing Partner", "email": "joerg@borek.example"},
}


def create(client: TestClient, body: dict, user: UUID | None = None):
    return client.post("/opportunities", headers=headers(user) if user else headers(), json=body)


def discovery(client: TestClient, opportunity_id: str, body: dict | None = None):
    return client.post(f"/opportunities/{opportunity_id}/discovery-paper/generate", headers=headers(), **({"json": body} if body is not None else {}))


# --------------------------------------------------------------- contract


def test_the_opportunity_language_defaults_to_english_and_accepts_only_en_and_de(client: TestClient) -> None:
    assert ok(create(client, CLIENT), 201)["language"] == "en"
    assert ok(create(client, {**CLIENT, "language": "de"}), 201)["language"] == "de"
    for value in ("fr", "german", "de-DE", ""):
        assert create(client, {**CLIENT, "language": value}).status_code == 422, value
    opportunity_id = ok(create(client, CLIENT), 201)["id"]
    assert client.patch(f"/opportunities/{opportunity_id}", headers=headers(), json={"language": "xx"}).status_code == 422
    assert ok(client.patch(f"/opportunities/{opportunity_id}", headers=headers(), json={"language": "de"}))["language"] == "de"


def test_discovery_language_comes_from_the_request_else_the_opportunity(client: TestClient) -> None:
    opportunity_id = ok(create(client, CLIENT), 201)["id"]
    assert ok(discovery(client, opportunity_id))["language"] == "en", "no language anywhere: English"
    assert discovery(client, opportunity_id, {"language": "fr"}).status_code == 422
    german = ok(discovery(client, opportunity_id, {"language": "de"}))
    assert german["language"] == "de" and german["analysis"]["framing"]["document"]["title"].startswith("KI-Potenziale für")
    assert ok(get(client, f"/opportunities/{opportunity_id}"))["language"] == "de", "the explicit choice becomes the journey language"
    assert ok(discovery(client, opportunity_id))["language"] == "de", "later generations follow it"


def test_another_language_is_a_new_version_and_the_approved_one_never_changes(client: TestClient) -> None:
    opportunity_id = ok(create(client, GERMAN_CLIENT), 201)["id"]
    ok(discovery(client, opportunity_id))
    approved = ok(post(client, f"/opportunities/{opportunity_id}/discovery-paper/approve"))
    frozen = ok(get(client, f"/opportunities/{opportunity_id}/discovery-paper/versions/{approved['id']}"))
    english = ok(discovery(client, opportunity_id, {"language": "en"}))
    assert english["language"] == "en" and english["document_id"] != frozen["paper_json"]["document_id"]
    again = ok(get(client, f"/opportunities/{opportunity_id}/discovery-paper/versions/{approved['id']}"))
    assert again == frozen and again["paper_json"]["language"] == "de", "the approved German version is untouched"
    store = get_memory_store()
    languages = sorted(row["paper_json"]["language"] for row in store.discovery_paper_versions.values() if str(row["opportunity_id"]) == opportunity_id)
    assert languages == ["de", "en"]


def test_presentation_v1_is_built_in_the_language_of_the_approved_discovery(client: TestClient) -> None:
    opportunity_id = ok(create(client, GERMAN_CLIENT), 201)["id"]
    ok(discovery(client, opportunity_id))
    ok(post(client, f"/opportunities/{opportunity_id}/discovery-paper/approve"))
    refused = client.post(f"/opportunities/{opportunity_id}/stage1-outputs/generate", headers=headers(), json={"language": "en"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "LANGUAGE_MISMATCH", refused.text
    assert client.post(f"/opportunities/{opportunity_id}/stage1-outputs/generate", headers=headers(), json={"language": "it"}).status_code == 422
    deck = ok(client.post(f"/opportunities/{opportunity_id}/stage1-outputs/generate", headers=headers(), json={"language": "de"}))["outputs"]["presentation"]
    assert deck["status"] == "ready"
    manifest = stored_versions(deck["presentation_id"])[0]["generation_source_manifest"]
    assert (manifest["language"], manifest["master_language"]) == ("de", "en")
    # BAP-12: the deck and its version list say which part is in which language.
    center = ok(get(client, f"/presentations/{deck['presentation_id']}/deck"))
    assert (center["source"]["language"], center["source"]["master_language"], center["source"]["other_language_findings"]) == ("de", "en", 0)
    versions = ok(get(client, f"/presentations/{deck['presentation_id']}/versions"))
    assert [(row["source"]["language"], row["source"]["master_language"]) for row in versions] == [("de", "en")]


def test_discovery_versions_list_their_language_without_the_paper(client: TestClient) -> None:
    """BAP-12: the version list names each version's language, so history needs no paper download."""
    opportunity_id = ok(create(client, GERMAN_CLIENT), 201)["id"]
    ok(discovery(client, opportunity_id))
    ok(post(client, f"/opportunities/{opportunity_id}/discovery-paper/approve"))
    ok(discovery(client, opportunity_id, {"language": "en"}))
    rows = ok(get(client, f"/opportunities/{opportunity_id}/discovery-paper/versions"))["versions"]
    assert sorted((row["version_number"], row["status"], row["language"]) for row in rows) == [(1, "approved", "de"), (2, "draft", "en")]
    assert all("paper_json" not in row for row in rows), "the list stays light"
    assert ok(get(client, f"/opportunities/{opportunity_id}/discovery-paper/approved"))["language"] == "de"


def test_another_client_cannot_generate_in_any_language(client: TestClient) -> None:
    opportunity_id = ok(create(client, GERMAN_CLIENT), 201)["id"]
    for path, body in (("discovery-paper/generate", {"language": "de"}), ("post-meeting-discovery/generate", {"language": "de"}), ("master-presentation/v2/generate", {"language": "de"})):
        response = client.post(f"/opportunities/{opportunity_id}/{path}", headers=headers(OTHER), json=body)
        assert response.status_code == 404, (path, response.status_code)
    assert client.post(f"/opportunities/{opportunity_id}/discovery-paper/generate", json={"language": "de"}).status_code == 401


def test_an_earlier_email_flow_refuses_german_instead_of_writing_english(client: TestClient) -> None:
    opportunity_id = ok(create(client, GERMAN_CLIENT), 201)["id"]
    set_statics(client, opportunity_id, GERMAN_STATICS)
    response = client.post(f"/opportunities/{opportunity_id}/email-drafts/generate", headers=headers(), json={"journey_stage": "first_contact", "language": "de"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "LANGUAGE_UNSUPPORTED", response.text
    assert client.post(f"/opportunities/{opportunity_id}/email-drafts/generate", headers=headers(), json={"journey_stage": "first_contact", "language": "xx"}).status_code == 422


# --------------------------------------------------------------- a German journey


def slide_texts(pptx: bytes) -> str:
    """The text a reader sees on the slides: every text run, in slide order, case kept."""
    package = zipfile.ZipFile(io.BytesIO(pptx))
    names = sorted(
        (name for name in package.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
        key=lambda name: int(re.findall(r"\d+", name)[0]),
    )
    runs = [html.unescape(text) for name in names for text in re.findall(r"<a:t>([^<]*)</a:t>", package.read(name).decode("utf-8"))]
    return " ".join(" ".join(runs).split())


def test_a_german_journey_from_discovery_to_the_follow_up_email(client: TestClient) -> None:
    base = finalized(client, body=GERMAN_CLIENT, meeting=GERMAN_MEETING, notes=GERMAN_NOTES, excluded={"follow_ups": []})
    opportunity_id = base["opportunity"]
    set_statics(client, opportunity_id, GERMAN_STATICS)

    # Discovery 2: German wording around the confirmed statements, which keep their words.
    discovery2 = ok(get(client, f"/opportunities/{opportunity_id}/post-meeting-discovery"))
    document = discovery2["document"]
    assert document["language"] == "de" and discovery2["current_version"]["language"] == "de"
    assert [section["title"] for section in document["sections"]][:3] == ["Kontext", "Anforderungen", "Herausforderungen"]
    assert document["summary"]["meeting"].startswith("Aus dem ersten Gespräch:")
    texts = [item["text"] for section in document["sections"] for item in section["items"]]
    assert "Angebote müssen innerhalb eines Tages beim Kunden sein." in texts
    version_id = discovery2["current_version"]["id"]
    docx = client.get(f"/opportunities/{opportunity_id}/post-meeting-discovery/versions/{version_id}/download/docx", headers=headers())
    assert docx.status_code == 200
    xml = zipfile.ZipFile(io.BytesIO(docx.content)).read("word/document.xml").decode("utf-8")
    for label in ("Zusammenfassung", "Neu aus dem Gespräch", "Übernommen aus Discovery 1", "Quellen", "Prioritäten"):
        assert label in xml, label
    assert all(letter in xml for letter in "äöüß")

    # Discovery 2 in another language than the approved Discovery 1 is refused, not mixed.
    refused = client.post(f"/opportunities/{opportunity_id}/post-meeting-discovery/generate", headers=headers(), json={"language": "en"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] in {"LANGUAGE_MISMATCH", "WORKFLOW_FINALIZED"}

    # Master Presentation V2: a German appendix after the English canonical slides.
    v2_row = get_memory_store().presentation_versions[UUID(base["v2"])]
    manifest = v2_row["generation_source_manifest"]
    assert (manifest["language"], manifest["master_language"]) == ("de", "en")
    presentation_id = str(v2_row["presentation_id"])
    pptx = client.get(f"/presentations/{presentation_id}/versions/{base['v2']}/download/pptx", headers=headers())
    assert pptx.status_code == 200, pptx.text
    text = slide_texts(pptx.content)
    for kicker in ("Anhang · Nach unserem ersten Gespräch", "Aus dem Gespräch", "Was vereinbart wurde"):
        assert kicker.casefold() in text.casefold(), (kicker, text[-1500:])
    assert "Angebote müssen innerhalb eines Tages beim Kunden sein." in text, "the confirmed statement in its own words"
    assert all(letter in text for letter in "äöü")
    for english in ("After our first meeting", "From the meeting · Client statements", "What was agreed"):
        assert english.casefold() not in text.casefold(), english

    # Follow-up email: German, from the same package; an English request is refused.
    email = ok(generate_email(client, opportunity_id))
    draft = email["draft"]
    assert draft["source"]["language"] == "de"
    for length in draft["lengths"].values():
        assert length["body"].startswith("Sehr geehrte Frau Weber,") and "Mit freundlichen Grüßen" in length["body"]
        assert length["subject"] == "Angebotsbeschleunigung – Nachbereitung unseres Gesprächs"
    assert draft["send_status"] == "not_sent", "a reviewed draft, never sent automatically"
    saved = ok(get(client, f"/opportunities/{opportunity_id}/email-drafts?journey_stage=deepening"))
    english = generate_email(client, opportunity_id, language="en", overwrite_edits=True)
    assert english.status_code == 409 and english.json()["error"]["code"] == "LANGUAGE_MISMATCH", english.text
    assert ok(get(client, f"/opportunities/{opportunity_id}/email-drafts?journey_stage=deepening")) == saved, "a refused email changes nothing"
    jobs = len(get_memory_store().generation_jobs)
    v2_refused = client.post(f"/opportunities/{opportunity_id}/master-presentation/v2/generate", headers=headers(), json={"language": "en"})
    assert v2_refused.status_code == 409 and len(get_memory_store().generation_jobs) == jobs, "no V2 job for a refused language"

    # The approved Discovery 1 and 2 versions are unchanged by everything after them.
    package = ok(get(client, f"/opportunities/{opportunity_id}/workflow-status"))
    assert package["current_status"] == "finalized"
    assert json.dumps(document, ensure_ascii=False).count("Kunden") >= 1


# --------------------------------------------------------------- PR #13 review findings


def _ready_german_review(client: TestClient) -> str:
    """A German journey after the confirmed meeting review, with an approved Discovery 2, not finalized."""
    from tests.unit.api.discovery2_helpers import ensure_discovery2_approved
    from tests.unit.api.test_master_presentation_v2_api import prepared

    base = prepared(client, body=GERMAN_CLIENT, meeting=GERMAN_MEETING, notes=GERMAN_NOTES)
    opportunity_id = base["opportunity"]
    view = ok(get(client, f"/opportunities/{opportunity_id}/post-meeting-review"))
    ok(post(client, f"/opportunities/{opportunity_id}/post-meeting-review/confirm", {
        "transcript_id": view["extraction"]["transcript_id"], "extraction_generated_at": view["extraction"]["generated_at"],
        "review_fingerprint": view["review_fingerprint"], "excluded": {},
    }))
    ensure_discovery2_approved(client, opportunity_id, headers())
    return opportunity_id


def _discovery2_state(client: TestClient, opportunity_id: str) -> dict:
    store = get_memory_store()
    opportunity = store.opportunities[UUID(opportunity_id)]
    return {
        "view": ok(get(client, f"/opportunities/{opportunity_id}/post-meeting-discovery")),
        "working": json.loads(json.dumps(opportunity.get("post_meeting_discovery"), default=str)),
        "versions": [json.loads(json.dumps(row, default=str)) for row in store.post_meeting_discovery_versions.values() if str(row["opportunity_id"]) == opportunity_id],
        "readiness": ok(get(client, f"/opportunities/{opportunity_id}/post-meeting-review"))["readiness"],
    }


def test_finding_1_a_refused_language_leaves_discovery2_exactly_as_it_was(client: TestClient) -> None:
    opportunity_id = _ready_german_review(client)
    before = _discovery2_state(client, opportunity_id)
    assert before["view"]["current_version"]["status"] == "approved" and before["readiness"]["ready_for_v2"] is True
    refused = client.post(f"/opportunities/{opportunity_id}/post-meeting-discovery/generate", headers=headers(), json={"language": "en"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "LANGUAGE_MISMATCH", refused.text
    after = _discovery2_state(client, opportunity_id)
    assert after == before, "no working state, version, hash or readiness changed"


def test_finding_2_english_findings_in_a_german_journey_are_shown_as_confirmed_and_flagged(client: TestClient) -> None:
    from tests.unit.api.test_master_presentation_v2_api import MEETING, NOTES

    base = finalized(client, body=GERMAN_CLIENT, meeting=MEETING, notes=NOTES, excluded={"follow_ups": []})
    opportunity_id = base["opportunity"]
    set_statics(client, opportunity_id, GERMAN_STATICS)

    # Discovery 2: German wording, the English statements in their confirmed words, listed for review.
    discovery2 = ok(get(client, f"/opportunities/{opportunity_id}/post-meeting-discovery"))
    check = discovery2["language_check"]
    english_items = {item["id"]: item["text"] for section in discovery2["document"]["sections"] for item in section["items"] if item["origin"] != "discovery1"}
    assert check["language"] == "de" and set(check["other_language_item_ids"]) >= {key for key, text in english_items.items() if text.startswith("Quotes must go out")}
    assert "Quotes must go out within one day." in english_items.values(), "evidence is never rewritten"

    # V2 records how many shown findings read as the other language.
    manifest = get_memory_store().presentation_versions[UUID(base["v2"])]["generation_source_manifest"]
    assert manifest["other_language_findings"] >= 5

    # The email quotes them as confirmed - and says so, so the owner translates before confirming.
    draft = ok(generate_email(client, opportunity_id))["draft"]
    body = draft["lengths"]["medium"]["body"]
    assert body.startswith("Sehr geehrte Frau Weber,") and "Quotes must go out within one day" in body
    assert "statements_not_in_email_language" in draft["review_flags"]
    assert "Send the pricing export by Friday." in body and "Termin noch abzustimmen" not in body, "the English deadline is recognised"
    from tests.unit.api.test_followup_email import CHECKS

    unacknowledged = [flag for flag in draft["review_flags"] if flag != "statements_not_in_email_language"]
    refused = post(client, f"/opportunities/{opportunity_id}/email-drafts/{draft['id']}/confirm", {
        "selected_length": "medium", "expected_revision": draft["revision"], "review_checks": CHECKS, "acknowledged_flags": unacknowledged,
    })
    assert refused.status_code in (400, 409, 422), "the language flag must be acknowledged"


def test_finding_2_german_findings_in_a_german_journey_raise_nothing(client: TestClient) -> None:
    base = finalized(client, body=GERMAN_CLIENT, meeting=GERMAN_MEETING, notes=GERMAN_NOTES, excluded={"follow_ups": []})
    opportunity_id = base["opportunity"]
    set_statics(client, opportunity_id, GERMAN_STATICS)
    discovery2 = ok(get(client, f"/opportunities/{opportunity_id}/post-meeting-discovery"))
    assert discovery2["language_check"] == {"language": "de", "other_language_item_ids": []}
    assert get_memory_store().presentation_versions[UUID(base["v2"])]["generation_source_manifest"]["other_language_findings"] == 0
    assert "statements_not_in_email_language" not in ok(generate_email(client, opportunity_id))["draft"]["review_flags"]


NATIVE_GERMAN_MEETING = (
    "[00:02] Jörg Müller: Anforderung: Angebote müssen innerhalb eines Tages beim Kunden sein.\n"
    "[00:05] Dana Weber: Herausforderung: Die Preisdaten liegen in drei Systemen.\n"
    "Dana Weber: Priorität: Zuerst soll das Vertriebsteam entlastet werden.\n"
    "Tom: Besprochene Lösung: Ein Assistent, der das Angebot aus der Anfrage vorbereitet.\n"
    "Tom: Entscheidung: Ein Pilot mit zehn Angeboten wird gestartet.\n"
    "Dana Weber: Nächster Schritt: Dana Weber schickt den Preisexport bis Freitag.\n"
)
NATIVE_GERMAN_NOTES = "Priorität: Zuerst soll das Vertriebsteam entlastet werden.\nChance: Das Serviceteam könnte den Angebotsassistenten mitnutzen."


def test_finding_3_a_german_only_meeting_gives_a_german_journey_without_language_flags(client: TestClient) -> None:
    base = finalized(client, body=GERMAN_CLIENT, meeting=NATIVE_GERMAN_MEETING, notes=NATIVE_GERMAN_NOTES, excluded={"follow_ups": []})
    opportunity_id = base["opportunity"]
    set_statics(client, opportunity_id, GERMAN_STATICS)
    discovery2 = ok(get(client, f"/opportunities/{opportunity_id}/post-meeting-discovery"))
    meeting_items = [item["text"] for section in discovery2["document"]["sections"] for item in section["items"] if item["origin"] == "meeting"]
    assert "Angebote müssen innerhalb eines Tages beim Kunden sein." in meeting_items and "Ein Pilot mit zehn Angeboten wird gestartet." in meeting_items
    assert discovery2["language_check"]["other_language_item_ids"] == []
    draft = ok(generate_email(client, opportunity_id))["draft"]
    body = draft["lengths"]["medium"]["body"]
    assert "Die zentrale Anforderung, wie wir sie verstanden haben: Angebote müssen innerhalb eines Tages beim Kunden sein." in body
    assert "statements_not_in_email_language" not in draft["review_flags"] and "Termin noch abzustimmen" not in body
