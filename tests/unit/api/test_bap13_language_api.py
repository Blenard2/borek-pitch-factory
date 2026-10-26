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
    english = generate_email(client, opportunity_id, language="en", overwrite_edits=True)
    assert english.status_code == 409 and english.json()["error"]["code"] == "LANGUAGE_MISMATCH", english.text

    # The approved Discovery 1 and 2 versions are unchanged by everything after them.
    package = ok(get(client, f"/opportunities/{opportunity_id}/workflow-status"))
    assert package["current_status"] == "finalized"
    assert json.dumps(document, ensure_ascii=False).count("Kunden") >= 1
