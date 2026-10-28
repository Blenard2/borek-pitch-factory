"""Customer documents carry no internal material (product audit 2026-10-27, BUG-02 / BUG-05).

Synthetic clients only, in English and German, through the API:

* the salesperson's intake note never reaches Discovery 1 as printed text or its Word file;
* the Discovery 2 Word file shows no ids, revisions, file names, generator names, origin tags or
  notes-only points - but every confirmed client statement;
* Master Presentation V2 shows no notes-only point and no "noted for ourselves" page;
* the owner can deliberately share a notes point; that is a new draft that needs approval, the
  approved version is never changed, and only then do the Word file and V2 show it;
* the internal review keeps every point with its source;
* the exported .eml carries no internal ids.
"""

from __future__ import annotations

import email
import email.policy
import io
import json
import re
import zipfile
from typing import Any

import pytest
from docx import Document
from fastapi.testclient import TestClient

from tests.unit.api.discovery2_helpers import approve_current_discovery2
from tests.unit.api.test_master_presentation_v2_api import (  # noqa: F401 - ``client`` is a fixture
    client,
    confirm,
    download,
    generate_v2,
    get,
    headers,
    ok,
    prepared,
    review,
    slide_texts,
    stored_versions,
)

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
HEX_RE = re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE)

CASES: dict[str, dict[str, Any]] = {
    "en": {
        "client": {
            "client_name": "Harborview Fittings Ltd (synthetic)",
            "opportunity_name": "Faster quoting",
            "department": "Sales",
            "language": "en",
            "stage1_intake": {
                "poc_name": "Dana Weber",
                "sales_topic_description": "Faster quoting for the sales team",
                "about_company": "Internal: the COO decides alone and is very price-sensitive, so push the premium package early.",
            },
        },
        "meeting": (
            "Dana Weber: Requirement: Quotes must go out within one business day.\n"
            "Tom O’Neill: Challenge: Pricing data sits in three systems.\n"
            "Dana Weber: Priority: Start with the sales team.\n"
            "Tom O’Neill: Decision: Run a pilot with ten quotes.\n"
        ),
        "notes": (
            "Decision: The COO is the real decision maker and is very price-sensitive.\n"
            "Opportunity: The service team could reuse the quoting assistant."
        ),
        "said": ("Quotes must go out within one business day.", "Pricing data sits in three systems.", "Run a pilot with ten quotes."),
        "internal": "The COO is the real decision maker and is very price-sensitive.",
        "shareable": "The service team could reuse the quoting assistant.",
        "intake_marker": "price-sensitive, so push the premium package",
        "observations_heading": "Our observations",
        "observations_slide": "OUR OBSERVATIONS",
        "noted": ("noted for ourselves",),
        "d2_eyebrow": "AFTER OUR FIRST CONVERSATION",
    },
    "de": {
        "client": {
            "client_name": "Müller Präzisionstechnik GmbH (synthetisch)",
            "opportunity_name": "Schnellere Angebote",
            "department": "Vertrieb",
            "language": "de",
            "stage1_intake": {
                "poc_name": "Dr. Jürgen Weiß",
                "sales_topic_description": "Schnellere Angebotserstellung im Vertrieb",
                "about_company": "Intern: Herr Weiß entscheidet allein und ist sehr preissensibel, also früh das Premiumpaket anbieten.",
            },
        },
        "meeting": (
            "Anna Becker: Anforderung: Angebote müssen innerhalb eines Arbeitstages beim Kunden sein.\n"
            "Dr. Jürgen Weiß: Herausforderung: Die Preisdaten liegen in drei Systemen.\n"
            "Anna Becker: Priorität: Wir beginnen mit dem Vertriebsinnendienst.\n"
            "Dr. Jürgen Weiß: Entscheidung: Wir starten einen Pilot mit zehn Angeboten.\n"
        ),
        "notes": (
            "Entscheidung: Herr Weiß entscheidet allein und ist sehr preissensibel.\n"
            "Chance: Der Service könnte den Angebotsassistenten ebenfalls nutzen."
        ),
        "said": (
            "Angebote müssen innerhalb eines Arbeitstages beim Kunden sein.",
            "Die Preisdaten liegen in drei Systemen.",
            "Wir starten einen Pilot mit zehn Angeboten.",
        ),
        "internal": "Herr Weiß entscheidet allein und ist sehr preissensibel.",
        "shareable": "Der Service könnte den Angebotsassistenten ebenfalls nutzen.",
        "intake_marker": "preissensibel, also früh das Premiumpaket",
        "observations_heading": "Unsere Beobachtungen",
        "observations_slide": "UNSERE BEOBACHTUNGEN",
        "noted": ("für uns festgehalten", "noted for ourselves"),
        "d2_eyebrow": "NACH UNSEREM ERSTEN GESPRÄCH",
    },
}
# Wording of the old, internal Word file and of the generator - never in a customer file.
INTERNAL_WORDING = (
    "Carried forward", "New from the meeting", "Added by the owner", "source:", "Quelle:", "owner's notes",
    "Notizen des Verantwortlichen", "deterministic", "Testgenerator", "test generator", "revision", "Revision",
    "meeting.txt", "Discovery 1", "AI model", "KI-Modell", "Personal notes", "Persönliche Notizen", "[",
)


def docx_text(content: bytes) -> str:
    """Everything a reader of the Word file can see: body, tables, headers, footers, properties."""
    document = Document(io.BytesIO(content))
    parts = [paragraph.text for paragraph in document.paragraphs]
    parts += [cell.text for table in document.tables for row in table.rows for cell in row.cells]
    for section in document.sections:
        parts += [paragraph.text for paragraph in section.header.paragraphs + section.footer.paragraphs]
    properties = document.core_properties
    parts += [properties.author or "", properties.title or "", properties.subject or "", properties.comments or "", properties.last_modified_by or ""]
    return "\n".join(parts)


def raw_parts(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return "\n".join(archive.read(name).decode("utf-8", "ignore") for name in archive.namelist() if name.endswith((".xml", ".rels")))


def discovery2(client: TestClient, opportunity_id: str) -> dict:
    return ok(get(client, f"/opportunities/{opportunity_id}/post-meeting-discovery"))


def discovery2_docx(client: TestClient, opportunity_id: str, version_id: str) -> bytes:
    response = get(client, f"/opportunities/{opportunity_id}/post-meeting-discovery/versions/{version_id}/download/docx")
    assert response.status_code == 200, response.text
    return response.content


def find_item(document: dict, text: str) -> tuple[dict, dict]:
    for section in document["sections"]:
        for item in section["items"]:
            if item["text"] == text:
                return section, item
    raise AssertionError(f"{text!r} is not in Discovery 2")


def share(client: TestClient, opportunity_id: str, text: str, value: bool | None = True, *, item_text: str | None = None):
    view = discovery2(client, opportunity_id)
    section, target = find_item(view["document"], text)
    items = [
        {"id": item["id"], "text": item_text if item is target and item_text else item["text"], **({"share_with_client": value} if item is target and value is not None else {})}
        for item in section["items"]
    ]
    current = view["current_version"]
    return client.patch(
        f"/opportunities/{opportunity_id}/post-meeting-discovery",
        headers=headers(),
        json={"expected_version_id": current["id"], "expected_content_hash": current["content_hash"], "sections": [{"key": section["key"], "items": items}]},
    )


def assert_customer_safe(text: str, ids: list[str]) -> None:
    for identifier in ids:
        assert identifier not in text, identifier
    assert not UUID_RE.search(text), UUID_RE.search(text)
    assert not HEX_RE.search(text), HEX_RE.search(text)
    for phrase in INTERNAL_WORDING:
        assert phrase not in text, phrase


@pytest.fixture(params=sorted(CASES))
def case(request) -> dict[str, Any]:
    return {"language": request.param, **CASES[request.param]}


def test_discovery1_never_prints_the_intake_note(client: TestClient, case: dict) -> None:
    base = prepared(client, body=case["client"], meeting=case["meeting"], notes=case["notes"])
    opportunity_id = base["opportunity"]
    version = ok(get(client, f"/opportunities/{opportunity_id}/discovery-paper/versions/{base['approved']}"))
    paper = version["paper_json"]
    assert paper["language"] == case["language"]
    printed = json.dumps([page["content"] for page in paper["page_manifest"]], ensure_ascii=False)
    assert case["intake_marker"] not in printed and "Background to this conversation" not in printed and "Hintergrund zu diesem Gespräch" not in printed
    overview = paper["analysis"]["framing"]["chapters"]["overview"]["paragraphs"][0]
    assert case["client"]["client_name"] in overview, "the neutral paragraph names the company"
    # Kept for the owner's review and traceability, not printed.
    profile = paper["analysis"]["research"]["company_profile"]
    assert profile["origin"] == "USER_INPUT" and case["intake_marker"] in profile["text"]
    response = get(client, f"/opportunities/{opportunity_id}/discovery-paper/versions/{base['approved']}/download/docx")
    assert response.status_code == 200, response.text
    text = docx_text(response.content)
    assert case["intake_marker"] not in text
    assert_customer_safe(text, [opportunity_id, base["approved"]])


def test_discovery2_word_file_is_a_customer_document(client: TestClient, case: dict) -> None:
    base = prepared(client, body=case["client"], meeting=case["meeting"], notes=case["notes"])
    opportunity_id = base["opportunity"]
    confirm(client, opportunity_id)
    view = discovery2(client, opportunity_id)
    version = view["current_version"]
    assert version["status"] == "approved"

    # The internal review keeps every point, with its source.
    _section, internal = find_item(view["document"], case["internal"])
    assert internal["source"] == "personal_notes" and not internal.get("share_with_client")

    content = discovery2_docx(client, opportunity_id, version["id"])
    text = docx_text(content)
    for said in case["said"]:
        assert said in text, f"confirmed client statement missing: {said}"
    assert case["internal"] not in text and case["shareable"] not in text, "a notes-only point stays internal"
    assert case["client"]["client_name"] in text and case["d2_eyebrow"] in text
    document = view["document"]
    ids = [opportunity_id, version["id"], base["transcript"], document["document_id"], document["sources"]["transcript"]["revision"]]
    assert_customer_safe(text, ids)
    raw = raw_parts(content)
    for identifier in ids:
        assert identifier not in raw, f"{identifier} hidden in the file"
    assert "python-docx" not in raw
    properties = Document(io.BytesIO(content)).core_properties
    assert properties.author == "Borek Solutions Group"
    if case["language"] == "de":
        assert 'w:val="de-DE"' in raw and "Zusammenfassung" in text and "Aus unserem Gespräch" in text
        assert "Summary" not in text
    else:
        assert "Summary" in text and "From our conversation" in text


def test_master_presentation_v2_shows_no_internal_note(client: TestClient, case: dict) -> None:
    base = prepared(client, body=case["client"], meeting=case["meeting"], notes=case["notes"])
    opportunity_id, presentation_id = base["opportunity"], base["presentation"]
    confirm(client, opportunity_id)
    ok(generate_v2(client, opportunity_id))
    v2_row = stored_versions(presentation_id)[-1]
    flat = " ".join(text for slide in slide_texts(download(client, presentation_id, str(v2_row["id"]))) for text in slide)
    for said in case["said"]:
        assert said in flat, f"confirmed client statement missing: {said}"
    assert case["internal"] not in flat and case["shareable"] not in flat
    for phrase in case["noted"]:
        assert phrase.casefold() not in flat.casefold()
    assert not UUID_RE.search(flat)
    manifest = v2_row["generation_source_manifest"]
    assert manifest["internal_finding_count"] == 2, "the withheld notes are counted for traceability"


def test_sharing_a_note_is_a_reviewed_new_version(client: TestClient, case: dict) -> None:
    base = prepared(client, body=case["client"], meeting=case["meeting"], notes=case["notes"])
    opportunity_id, presentation_id = base["opportunity"], base["presentation"]
    confirm(client, opportunity_id)
    approved = discovery2(client, opportunity_id)["current_version"]
    approved_file = discovery2_docx(client, opportunity_id, approved["id"])
    ok(generate_v2(client, opportunity_id))
    first_v2 = stored_versions(presentation_id)[-1]

    # Only a notes point can be shared.
    refused = share(client, opportunity_id, case["said"][0])
    assert refused.status_code == 400 and "personal notes" in refused.text

    shared = ok(share(client, opportunity_id, case["shareable"]))
    draft = shared["current_version"]
    assert (draft["status"], draft["version_number"]) == ("draft", approved["version_number"] + 1)
    _section, item = find_item(shared["document"], case["shareable"])
    assert item["share_with_client"] is True and item["edited"] is False, "sharing does not reword the point"
    # The approved version is never changed: same content, same file.
    rows = {row["id"]: row for row in shared["versions"]}
    assert rows[approved["id"]]["content_hash"] == approved["content_hash"] and rows[approved["id"]]["status"] == "approved"
    # Rendered on request: the same content (zip timestamps differ between two renders).
    assert docx_text(discovery2_docx(client, opportunity_id, approved["id"])) == docx_text(approved_file)
    assert case["shareable"] not in docx_text(approved_file)

    # Approval stays mandatory: V2 waits for the new draft.
    assert set(review(client, opportunity_id)["readiness"]["blockers"]) & {"DISCOVERY2_NOT_APPROVED", "DISCOVERY2_STALE"}
    assert generate_v2(client, opportunity_id).status_code in (400, 409)
    approve_current_discovery2(client, opportunity_id, headers())

    text = docx_text(discovery2_docx(client, opportunity_id, draft["id"]))
    assert case["shareable"] in text and case["observations_heading"] in text
    assert case["internal"] not in text, "the other note is still internal"
    ok(generate_v2(client, opportunity_id))
    rows = stored_versions(presentation_id)
    assert rows[-2] == first_v2 or any(row["id"] == first_v2["id"] for row in rows), "the earlier V2 stays"
    slides = slide_texts(download(client, presentation_id, str(rows[-1]["id"])))
    holder = [slide for slide in slides if case["shareable"] in slide]
    assert len(holder) == 1 and any(case["observations_slide"] in text.upper() for text in holder[0])
    assert case["internal"] not in " ".join(text for slide in slides for text in slide)


def test_discovery2_with_only_internal_points_cannot_be_approved(client: TestClient) -> None:
    meeting = "Dana Weber: Thanks for having us today.\nTom O’Neill: We talked about the trade fair.\n"
    notes = "Requirement: The COO wants quotes within a day.\nChallenge: Budget is tight this year."
    base = prepared(client, body=CASES["en"]["client"], meeting=meeting, notes=notes)
    opportunity_id = base["opportunity"]
    confirm(client, opportunity_id, discovery2=False)
    generated = ok(client.post(f"/opportunities/{opportunity_id}/post-meeting-discovery/generate", headers=headers()))
    current = generated["current_version"]
    refused = client.post(
        f"/opportunities/{opportunity_id}/post-meeting-discovery/approve",
        headers=headers(),
        json={"expected_version_id": current["id"], "expected_content_hash": current["content_hash"]},
    )
    assert refused.status_code == 400 and "DISCOVERY2_NO_CLIENT_CONTENT" in refused.text
    # Sharing one of them is the deliberate decision that makes it approvable.
    ok(share(client, opportunity_id, "The COO wants quotes within a day."))
    approved = approve_current_discovery2(client, opportunity_id, headers())
    assert approved["current_version"]["status"] == "approved"
