"""Word (.docx) files of Discovery 1 and Discovery 2 versions (BAP-7).

Server-side files, so both Discovery documents can be downloaded and attached to the follow-up
email like the presentations. Rendered from the stored version only - nothing is generated here.
"""

from __future__ import annotations

import io
from typing import Any

from docx import Document
from docx.shared import Pt

ORIGIN_LABEL = {
    "discovery1": "Carried forward from Discovery 1",
    "meeting": "New from the meeting",
    "owner": "Added by the owner",
}
SOURCE_LABEL = {
    "discovery1": "Discovery 1",
    "transcript": "transcript",
    "personal_notes": "owner's notes",
    "both": "transcript and owner's notes",
    "owner": "owner",
}


def _version_line(version: dict[str, Any]) -> str:
    status = str(version.get("status") or "")
    line = f"Version {version.get('version_number')} · {status}"
    if version.get("approved_at"):
        line += f" · approved {str(version['approved_at'])[:10]}"
    return line


def _save(document: Any) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _base(title: str, subtitle: str) -> Any:
    document = Document()
    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    document.add_heading(title, level=0)
    document.add_paragraph(subtitle)
    return document


def render_discovery2_docx(paper: dict[str, Any], version: dict[str, Any]) -> bytes:
    """Discovery 2 with every item's origin and source, and the source revisions it was built from."""
    document = _base(str(paper.get("title") or "Discovery 2"), _version_line(version))
    summary = paper.get("summary") or {}
    if summary:
        document.add_heading("Summary", level=1)
        document.add_paragraph(f"Before the meeting (Discovery 1): {summary.get('previous', '')}")
        document.add_paragraph(f"After the meeting: {summary.get('meeting', '')}")
    for section in paper.get("sections") or []:
        document.add_heading(str(section["title"]), level=1)
        if not section["items"]:
            document.add_paragraph("Nothing recorded.")
            continue
        for item in section["items"]:
            paragraph = document.add_paragraph(style="List Bullet")
            paragraph.add_run(str(item["text"]))
            label = ORIGIN_LABEL.get(item["origin"], item["origin"])
            if item["origin"] == "meeting":
                label += f" ({'updated' if item['status'] == 'updated' else 'new'}, source: {SOURCE_LABEL.get(item['source'], item['source'])})"
            if item.get("edited"):
                label += " · edited by the owner"
            note = paragraph.add_run(f"  [{label}]")
            note.italic = True
            note.font.size = Pt(9)
    sources = paper.get("sources") or {}
    if sources:
        document.add_heading("Sources", level=1)
        discovery1 = sources.get("discovery1") or {}
        transcript = sources.get("transcript") or {}
        notes = sources.get("personal_notes") or {}
        review = sources.get("meeting_review") or {}
        generation = paper.get("generation") or {}
        rows = [
            ("Discovery 1", f"version {discovery1.get('version_number')} ({discovery1.get('version_id')})"),
            ("Transcript", f"{transcript.get('file_name') or transcript.get('transcript_id')} (revision {str(transcript.get('revision'))[:12]})"),
            ("Personal notes", f"saved {notes.get('updated_at')}" if notes.get("updated_at") else "none"),
            ("Confirmed findings", f"{review.get('confirmed_finding_count')} confirmed {str(review.get('confirmed_at') or '')[:10]}"),
            ("Generated", f"{str(paper.get('generated_at') or '')[:19]} · {'AI model' if generation.get('mode') == 'live' else 'deterministic test generator'}"),
        ]
        table = document.add_table(rows=len(rows), cols=2)
        for row, (label, value) in zip(table.rows, rows):
            row.cells[0].text = label
            row.cells[1].text = str(value)
    return _save(document)


def render_discovery1_docx(paper: dict[str, Any], version: dict[str, Any]) -> bytes:
    """The approved Discovery analysis (schema 2.0) as a Word document."""
    analysis = paper["analysis"]
    framing = analysis["framing"]["document"]
    document = _base(str(framing.get("title") or "Discovery"), _version_line(version))
    if framing.get("subtitle"):
        document.add_paragraph(str(framing["subtitle"]))
    document.add_heading("Core thesis", level=1)
    document.add_paragraph(str(analysis["research"]["core_thesis"]["text"]))
    document.add_heading("Opportunities by area", level=1)
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
        document.add_heading("Target workflows", level=1)
        for workflow in workflows:
            paragraph = document.add_paragraph(style="List Bullet")
            paragraph.add_run(f"{workflow['name']}: ").bold = True
            paragraph.add_run(f"{workflow.get('autonomous_flow', '')} Human decision: {workflow.get('human_decision_gate', '')}")
    closing = analysis.get("closing") or {}
    if closing.get("paragraphs"):
        document.add_heading(str(closing.get("headline") or closing.get("name") or "Conclusion"), level=1)
        for text in closing["paragraphs"]:
            document.add_paragraph(str(text))
    return _save(document)
