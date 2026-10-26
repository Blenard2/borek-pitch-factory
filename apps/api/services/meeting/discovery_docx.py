"""Word (.docx) files of Discovery 1 and Discovery 2 versions (BAP-7).

Server-side files, so both Discovery documents can be downloaded and attached to the follow-up
email like the presentations. Rendered from the stored version only - nothing is generated here.

BAP-13: every label is in the document's own language (``language`` of the stored version,
English when it has none), and a German file is marked as German for spelling and hyphenation.
"""

from __future__ import annotations

import io
from typing import Any

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from services.language import stored_language

_LABELS: dict[str, dict[str, Any]] = {
    "en": {
        "origin": {"discovery1": "Carried forward from Discovery 1", "meeting": "New from the meeting", "owner": "Added by the owner"},
        "source": {"discovery1": "Discovery 1", "transcript": "transcript", "personal_notes": "owner's notes", "both": "transcript and owner's notes", "owner": "owner"},
        "status": {},
        "version": "Version {number} · {status}",
        "approved_on": " · approved {date}",
        "summary": "Summary",
        "before": "Before the meeting (Discovery 1): {text}",
        "after": "After the meeting: {text}",
        "nothing": "Nothing recorded.",
        "updated": "updated",
        "new": "new",
        "source_of": "source: {source}",
        "edited": " · edited by the owner",
        "sources": "Sources",
        "rows": ("Discovery 1", "Transcript", "Personal notes", "Confirmed findings", "Generated"),
        "discovery1_value": "version {number} ({id})",
        "transcript_value": "{name} (revision {revision})",
        "notes_saved": "saved {date}",
        "none": "none",
        "confirmed_value": "{count} confirmed {date}",
        "ai_model": "AI model",
        "test_generator": "deterministic test generator",
        "core_thesis": "Core thesis",
        "opportunities": "Opportunities by area",
        "workflows": "Target workflows",
        "human_decision": "Human decision: {gate}",
        "conclusion": "Conclusion",
        "lang": None,
    },
    "de": {
        "origin": {"discovery1": "Übernommen aus Discovery 1", "meeting": "Neu aus dem Gespräch", "owner": "Vom Verantwortlichen ergänzt"},
        "source": {"discovery1": "Discovery 1", "transcript": "Transkript", "personal_notes": "Notizen des Verantwortlichen", "both": "Transkript und Notizen des Verantwortlichen", "owner": "Verantwortlicher"},
        "status": {"approved": "freigegeben", "draft": "Entwurf"},
        "version": "Version {number} · {status}",
        "approved_on": " · freigegeben am {date}",
        "summary": "Zusammenfassung",
        "before": "Vor dem Gespräch (Discovery 1): {text}",
        "after": "Nach dem Gespräch: {text}",
        "nothing": "Nichts erfasst.",
        "updated": "aktualisiert",
        "new": "neu",
        "source_of": "Quelle: {source}",
        "edited": " · vom Verantwortlichen bearbeitet",
        "sources": "Quellen",
        "rows": ("Discovery 1", "Transkript", "Persönliche Notizen", "Bestätigte Erkenntnisse", "Erstellt"),
        "discovery1_value": "Version {number} ({id})",
        "transcript_value": "{name} (Revision {revision})",
        "notes_saved": "gespeichert {date}",
        "none": "keine",
        "confirmed_value": "{count} bestätigt {date}",
        "ai_model": "KI-Modell",
        "test_generator": "deterministischer Testgenerator",
        "core_thesis": "Kernthese",
        "opportunities": "Opportunities nach Bereich",
        "workflows": "Ziel-Workflows",
        "human_decision": "Menschliche Entscheidung: {gate}",
        "conclusion": "Fazit",
        "lang": "de-DE",
    },
}


def _labels(paper: dict[str, Any]) -> dict[str, Any]:
    return _LABELS[stored_language(paper.get("language"))]


def _version_line(version: dict[str, Any], labels: dict[str, Any]) -> str:
    status = str(version.get("status") or "")
    line = labels["version"].format(number=version.get("version_number"), status=labels["status"].get(status, status))
    if version.get("approved_at"):
        line += labels["approved_on"].format(date=str(version["approved_at"])[:10])
    return line


def _save(document: Any) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _base(title: str, subtitle: str, language_tag: str | None = None) -> Any:
    document = Document()
    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    if language_tag:
        # Spelling, grammar and hyphenation in the document's language.
        properties = style.element.get_or_add_rPr()
        lang = OxmlElement("w:lang")
        lang.set(qn("w:val"), language_tag)
        properties.append(lang)
    document.add_heading(title, level=0)
    document.add_paragraph(subtitle)
    return document


def render_discovery2_docx(paper: dict[str, Any], version: dict[str, Any]) -> bytes:
    """Discovery 2 with every item's origin and source, and the source revisions it was built from."""
    labels = _labels(paper)
    document = _base(str(paper.get("title") or "Discovery 2"), _version_line(version, labels), labels["lang"])
    summary = paper.get("summary") or {}
    if summary:
        document.add_heading(labels["summary"], level=1)
        document.add_paragraph(labels["before"].format(text=summary.get("previous", "")))
        document.add_paragraph(labels["after"].format(text=summary.get("meeting", "")))
    for section in paper.get("sections") or []:
        document.add_heading(str(section["title"]), level=1)
        if not section["items"]:
            document.add_paragraph(labels["nothing"])
            continue
        for item in section["items"]:
            paragraph = document.add_paragraph(style="List Bullet")
            paragraph.add_run(str(item["text"]))
            label = labels["origin"].get(item["origin"], item["origin"])
            if item["origin"] == "meeting":
                state = labels["updated"] if item["status"] == "updated" else labels["new"]
                source = labels["source_of"].format(source=labels["source"].get(item["source"], item["source"]))
                label += f" ({state}, {source})"
            if item.get("edited"):
                label += labels["edited"]
            note = paragraph.add_run(f"  [{label}]")
            note.italic = True
            note.font.size = Pt(9)
    sources = paper.get("sources") or {}
    if sources:
        document.add_heading(labels["sources"], level=1)
        discovery1 = sources.get("discovery1") or {}
        transcript = sources.get("transcript") or {}
        notes = sources.get("personal_notes") or {}
        review = sources.get("meeting_review") or {}
        generation = paper.get("generation") or {}
        values = [
            labels["discovery1_value"].format(number=discovery1.get("version_number"), id=discovery1.get("version_id")),
            labels["transcript_value"].format(name=transcript.get("file_name") or transcript.get("transcript_id"), revision=str(transcript.get("revision"))[:12]),
            labels["notes_saved"].format(date=notes.get("updated_at")) if notes.get("updated_at") else labels["none"],
            labels["confirmed_value"].format(count=review.get("confirmed_finding_count"), date=str(review.get("confirmed_at") or "")[:10]),
            f"{str(paper.get('generated_at') or '')[:19]} · {labels['ai_model'] if generation.get('mode') == 'live' else labels['test_generator']}",
        ]
        table = document.add_table(rows=len(values), cols=2)
        for row, label, value in zip(table.rows, labels["rows"], values):
            row.cells[0].text = label
            row.cells[1].text = str(value)
    return _save(document)


def render_discovery1_docx(paper: dict[str, Any], version: dict[str, Any]) -> bytes:
    """The approved Discovery analysis (schema 2.0) as a Word document."""
    labels = _labels(paper)
    analysis = paper["analysis"]
    framing = analysis["framing"]["document"]
    document = _base(str(framing.get("title") or "Discovery"), _version_line(version, labels), labels["lang"])
    if framing.get("subtitle"):
        document.add_paragraph(str(framing["subtitle"]))
    document.add_heading(labels["core_thesis"], level=1)
    document.add_paragraph(str(analysis["research"]["core_thesis"]["text"]))
    document.add_heading(labels["opportunities"], level=1)
    for area in analysis.get("areas") or []:
        document.add_heading(str(area["name"]), level=2)
        if area.get("lead"):
            document.add_paragraph(str(area["lead"]))
        for opportunity in area.get("opportunities") or []:
            paragraph = document.add_paragraph(style="List Bullet")
            paragraph.add_run(f"{opportunity['title']}: ").bold = True
            paragraph.add_run(f"{opportunity.get('solution', '')} {opportunity.get('result', '')}".strip())
            for question in opportunity.get("discovery_questions") or []:
                document.add_paragraph(str(question), style="List Bullet 2")
    workflows = analysis.get("target_workflows") or []
    if workflows:
        document.add_heading(labels["workflows"], level=1)
        for workflow in workflows:
            paragraph = document.add_paragraph(style="List Bullet")
            paragraph.add_run(f"{workflow['name']}: ").bold = True
            paragraph.add_run(f"{workflow.get('autonomous_flow', '')} {labels['human_decision'].format(gate=workflow.get('human_decision_gate', ''))}")
    closing = analysis.get("closing") or {}
    if closing.get("paragraphs"):
        document.add_heading(str(closing.get("headline") or closing.get("name") or labels["conclusion"]), level=1)
        for text in closing["paragraphs"]:
            document.add_paragraph(str(text))
    return _save(document)
