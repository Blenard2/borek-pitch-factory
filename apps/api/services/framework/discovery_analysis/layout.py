"""Content -> page manifest. Pure and deterministic: the same analysis always yields the same pages.

Only the nine page types of the white paper master (M1-M9) are produced. Nothing is ever
truncated: content that does not fit one page continues on the next page of the same type.
"""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlsplit

from services.framework.discovery_analysis.fixture import load_library
from services.framework.discovery_analysis.model import (
    BRAND_CLAIM,
    CLOSING_LINE,
    M5_ITEMS_PER_PAGE,
    M6_ROWS_PER_PAGE,
    M7_ROWS_PER_PAGE,
    business_case_text,
    payback_label,
    solution_text,
    target_findings,
)

# Print geometry of the master at 96 dpi (A4, light page padding 56/76/90). The figures below
# are deliberately conservative estimates; the web renderer additionally measures real overflow.
_PAGE_BODY_PX = 977
_RUNNING_HEAD_PX = 26
_TABLE_HEAD_PX = 48
_ROW_PADDING_PX = 24
_ROW_LINE_PX = 18
_LEAD_LINE_PX = 25
_LEAD_CHARS = 80
_FOOTNOTE_CHARS = 125
_FOOTNOTE_LINE_PX = 15.2
# Characters per line for the three M6 columns. The master's geometry is binding:
# 24 % - fluid - 20 %. Rows that get tall in it continue on the next M6 page.
_M6_CHARS = (21, 54, 19)
_M4_CHARS = (27, 72)
_M5_TEXT_CHARS = 80
_M5_LINE_PX = 19.4
_M5_ITEM_FIXED_PX = 180
_M5_HEAD_PX = 100

OPTIONAL_PART_ORDER = ("roles_employees", "decision_map", "system_interfaces")


def build_page_manifest(paper: dict[str, Any]) -> list[dict[str, Any]]:
    analysis = paper["analysis"]
    framing = analysis["framing"]
    document = framing["document"]
    chapters = framing["chapters"]
    leads = framing["leads"]
    library = load_library()
    labels = library["chapters"]
    publisher = library["publisher"]
    month, year = _month_year(paper.get("generated_at"))
    running_left = f"{document['eyebrow']} {year}".strip()
    footer = f"{publisher['company'].upper()} · {month}/{year}" if year else publisher["company"].upper()
    areas = analysis["areas"]
    picture = analysis["target_picture"]
    pages: list[dict[str, Any]] = []
    contents: list[dict[str, Any]] = []
    chapter_number = 0

    def add(page_id: str, page_type: str, nav_label: str, edit_targets: list[str], content: dict[str, Any], chapter: dict[str, Any] | None) -> None:
        right = f"{chapter['number']:02d} · {chapter['name']}" if chapter else document["short_title"]
        pages.append(
            {
                "id": page_id,
                "number": len(pages) + 1,
                "type": page_type,
                "dark": page_type in {"M1", "M3", "M9"},
                "chapter": chapter,
                "nav_label": nav_label,
                "edit_targets": edit_targets,
                "content": {**content, "running": {"left": running_left, "right": right, "footer": footer}},
            }
        )

    def open_chapter(key: str, chapter: dict[str, Any], edit_target: str) -> dict[str, Any]:
        nonlocal chapter_number
        chapter_number += 1
        ref = {"number": chapter_number, "name": chapter["name"]}
        contents.append({"number": f"{chapter_number:02d}", "label": f"Part {chapter_number} · {chapter['name']}", "page_id": f"ch-{key}"})
        add(
            f"ch-{key}",
            "M3",
            chapter["name"],
            [edit_target],
            {
                "chapter_number": f"{chapter_number:02d}",
                "chapter_name": chapter["name"],
                "headline": chapter["headline"],
                "paragraphs": list(chapter["paragraphs"]),
                "key_message": chapter["key_message"],
            },
            ref,
        )
        return ref

    def add_tables(base_id: str, nav_label: str, lead: str, columns: list[str], rows: list[dict[str, Any]], footnote: str | None, edit_targets: list[str], row_target: str | None, chapter: dict[str, Any]) -> None:
        chunks = _paginate_rows(rows, lead, footnote, _M6_CHARS, M6_ROWS_PER_PAGE)
        for index, chunk in enumerate(chunks, start=1):
            add(
                f"{base_id}-{index}",
                "M6",
                _numbered(nav_label, index, len(chunks)),
                [*edit_targets, *([f"{row_target}:{row['id']}" for row in chunk] if row_target else [])],
                {"lead": lead, "columns": columns, "rows": chunk, "footnote": footnote},
                chapter,
            )

    add(
        "cover",
        "M1",
        "Cover",
        ["framing"],
        {
            "eyebrow": document["eyebrow"],
            "title": document["title"],
            "subtitle": document["subtitle"],
            "document_type_line": f"{document['document_type']} {year}".strip(),
            "as_of": f"As of {month}/{year[2:]}" if year else "",
            "place": publisher["place"],
            "url": publisher["url"],
        },
        None,
    )
    add("contents", "M2", "Contents", ["framing"], {}, None)  # filled once every page number is known

    # Part 1 - overview: company background (website research), opportunity map, business case.
    chapter = open_chapter("overview", chapters["overview"], "framing")
    background, background_note = company_background(paper)
    if background:
        # Quotes from the website with their source; not editable, they are what the site says.
        add_tables(
            "company-background",
            labels["overview"]["background_nav"],
            background_lead(paper),
            list(labels["overview"]["background_columns"]),
            background,
            background_note,
            [],
            None,
            chapter,
        )
    map_rows = [
        {
            "id": area["id"],
            "label": area["name"],
            "text": " · ".join(item["title"] for item in area["opportunities"]),
            "highlighted": area["priority"],
        }
        for area in areas
    ]
    # The point is headed "Working hypothesis", so the label is not printed a second time.
    thesis = analysis["research"]["core_thesis"]["text"]
    label = f"{labels['overview']['thesis_title']}: "
    if thesis.lower().startswith(label.lower()) and len(thesis) > len(label):
        thesis = thesis[len(label)].upper() + thesis[len(label) + 1 :]
    thesis_px = 110 + _lines(thesis, 88) * 22
    map_chunks = _paginate_rows(
        [{**row, "_cells": (row["label"], row["text"])} for row in map_rows],
        leads["map"],
        None,
        _M4_CHARS,
        len(map_rows),
        reserved_px=thesis_px,
    )
    for index, chunk in enumerate(map_chunks, start=1):
        add(
            f"overview-map-{index}",
            "M4",
            _numbered("Opportunity map", index, len(map_chunks)),
            ["thesis", "framing"],
            {
                "lead": leads["map"],
                "points": [{"number": "01", "title": labels["overview"]["thesis_title"], "text": thesis}] if index == 1 else [],
                "kpis": [],
                "table": {"columns": list(labels["overview"]["map_columns"]), "rows": chunk},
                "footnotes": [],
            },
            chapter,
        )
    ordered = [area for area in areas if area["business_case"]["payback_type"] == "quick_win"] + [
        area for area in areas if area["business_case"]["payback_type"] != "quick_win"
    ]
    add_tables(
        "overview-business-case",
        "Business case",
        leads["business_case"],
        list(labels["overview"]["business_case_columns"]),
        [
            {
                "id": area["id"],
                "subject": area["name"],
                "description": business_case_text(area["business_case"]),
                "verdict": payback_label(area["business_case"]["payback_type"]),
                "grouped": area["business_case"]["payback_type"] != "quick_win",
            }
            for area in ordered
        ],
        leads["business_case_footnote"],
        ["framing"],
        "area",
        chapter,
    )

    # Part 2 - one deep dive per opportunity, at most two per page.
    chapter = open_chapter("deep-dive", chapters["deep_dive"], "framing")
    for area in areas:
        items = [
            {
                "id": item["id"],
                "number": f"{position:02d}",
                "title": item["title"],
                "solution": solution_text(item),
                "how_it_works": item["how_it_works"],
                "result": item["result"],
                "questions": list(item["discovery_questions"]),
                "signal": item["opportunity_signal"],
            }
            for position, item in enumerate(area["opportunities"], start=1)
        ]
        chunks = _paginate_items(items, area["lead"])
        for index, chunk in enumerate(chunks, start=1):
            add(
                f"area-{area['id']}-{index}",
                "M5",
                _numbered(area["name"], index, len(chunks)),
                [f"area:{area['id']}", *[f"opportunity:{area['id']}/{item['id']}" for item in chunk]],
                {"title": _numbered(area["name"], index, len(chunks)), "lead": area["lead"], "items": chunk},
                chapter,
            )

    # Part 3 - processes outside the systems.
    chapter = open_chapter("shadow", chapters["shadow"], "framing")
    add_tables(
        "shadow-table",
        "Shadow processes",
        leads["shadow_table"],
        list(labels["shadow"]["table_columns"]),
        [
            {
                "id": row["id"],
                "subject": row["decision"],
                "description": row["handled_today_via"],
                "verdict": row["ai_approach"],
                "grouped": False,
            }
            for row in analysis["shadow_processes"]
        ],
        leads["shadow_footnote"],
        ["framing"],
        "shadow_process",
        chapter,
    )
    add("shadow-findings", "M8", "Why it matters", ["shadow_findings", "framing"], {"lead": leads["shadow_findings"], **_findings(analysis["shadow_findings"])}, chapter)

    # Part 4 - target picture: the chains as a table, then as a diagram.
    chapter = open_chapter("target", chapters["target"], "framing")
    workflows = analysis["target_workflows"]
    add_tables(
        "target-chains",
        "Workflow chains",
        leads["target_table"],
        list(labels["target"]["table_columns"]),
        [
            {
                "id": workflow["id"],
                "subject": workflow["name"],
                "description": workflow["autonomous_flow"],
                "verdict": workflow["human_decision_gate"],
                "grouped": False,
            }
            for workflow in workflows
        ],
        leads["target_footnote"],
        ["framing"],
        "target_workflow",
        chapter,
    )
    diagram_chunks = _balanced(workflows, M7_ROWS_PER_PAGE)
    for index, chunk in enumerate(diagram_chunks, start=1):
        add(
            f"target-diagram-{index}",
            "M7",
            _numbered("Target picture", index, len(diagram_chunks)),
            ["target_picture", *[f"target_workflow:{workflow['id']}" for workflow in chunk]],
            {
                "title": _numbered(picture["diagram_title"], index, len(diagram_chunks)),
                "lead": picture["diagram_lead"],
                "rows": [
                    {"id": workflow["id"], "name": workflow["name"], "stages": list(workflow["stages"]), "gate": workflow["human_decision_gate"]}
                    for workflow in chunk
                ],
            },
            chapter,
        )
    add("target-findings", "M8", "How it emerges", ["target_picture", "framing"], {"lead": leads["target_findings"], **_findings(target_findings(picture))}, chapter)

    # Parts 5 and 6 - only when they were requested and exist.
    for key in OPTIONAL_PART_ORDER:
        table = analysis["optional_deep_dives"][key]
        if table is None:
            continue
        slug = key.replace("_", "-")
        chapter = open_chapter(slug, table["chapter"], f"optional:{key}")
        add_tables(
            f"{slug}-table",
            table["chapter"]["name"],
            table["lead"],
            list(table["columns"]),
            [{**row, "grouped": False} for row in table["rows"]],
            table["footnote"],
            [f"optional:{key}"],
            None,
            chapter,
        )

    # Conclusion: the three steps as numbered points (M4), then the branded closing page (M9).
    closing = analysis["closing"]
    chapter_number += 1
    chapter = {"number": chapter_number, "name": closing["name"]}
    contents.append({"number": f"{chapter_number:02d}", "label": closing["name"], "page_id": "closing-steps"})
    add(
        "closing-steps",
        "M4",
        "Three steps",
        ["closing", "framing"],
        {
            "lead": leads["closing_steps"],
            "points": [
                {"number": f"{position:02d}", "title": step["title"], "text": step["text"]}
                for position, step in enumerate(closing["steps"], start=1)
            ],
            "kpis": [],
            "table": {"columns": [], "rows": []},
            "footnotes": [],
        },
        chapter,
    )
    add(
        "closing",
        "M9",
        closing["name"],
        ["closing"],
        {
            "chapter_number": f"{chapter_number:02d}",
            "chapter_name": closing["name"],
            "headline": closing["headline"],
            "paragraphs": list(closing["paragraphs"]),
            "closing_line": CLOSING_LINE,
            "brand_claim": BRAND_CLAIM,
            "contact_line": f"{publisher['phone']} · {publisher['url']}",
            "address": publisher["address"],
        },
        chapter,
    )

    # Contents page numbers are computed last, from the paginated result.
    number_of = {page["id"]: page["number"] for page in pages}
    pages[1]["content"].update(
        {
            "title": "Contents",
            "entries": [
                {"number": entry["number"], "label": entry["label"], "page": number_of[entry["page_id"]], "page_id": entry["page_id"]}
                for entry in contents
            ],
            "in_short": list(document["in_short"]),
            "scope_note": document["scope_note"],
            "publisher": {
                "company": publisher["company"],
                "address": publisher["address"],
                "unit": publisher["unit"],
                "group": publisher["place"].split(" · ")[0],
                "phone": publisher["phone"],
                "url": publisher["url"],
            },
        }
    )
    for page in pages:
        for row in page["content"].get("table", {}).get("rows", []):
            row.pop("_cells", None)
    return pages


def _findings(findings: dict[str, Any]) -> dict[str, Any]:
    return {
        "cards": [dict(card) for card in findings["cards"]],
        "points": [
            {"number": f"{position:02d}", "title": point["title"], "text": point["text"]}
            for position, point in enumerate(findings["points"], start=1)
        ],
    }


def _numbered(label: str, index: int, total: int) -> str:
    return f"{label} ({index}/{total})" if total > 1 else label


def _lines(text: str, chars_per_line: int) -> int:
    return max(1, math.ceil(len(str(text)) / chars_per_line))


def _row_px(cells: tuple[str, ...], chars: tuple[int, ...]) -> float:
    return _ROW_PADDING_PX + _ROW_LINE_PX * max(_lines(cell, width) for cell, width in zip(cells, chars))


def _balanced(items: list[Any], per_page: int) -> list[list[Any]]:
    """Even split: six chains become 3 + 3, never 5 + 1."""
    page_count = max(1, math.ceil(len(items) / per_page))
    size = math.ceil(len(items) / page_count)
    return [items[start : start + size] for start in range(0, len(items), size)]


def _paginate_rows(
    rows: list[dict[str, Any]],
    lead: str,
    footnote: str | None,
    chars: tuple[int, ...],
    max_rows: int,
    *,
    reserved_px: float = 0,
) -> list[list[dict[str, Any]]]:
    """Rows per page: never more than the master allows, and never more than fit."""
    budget = _PAGE_BODY_PX - _RUNNING_HEAD_PX - _TABLE_HEAD_PX - reserved_px
    budget -= 36 + _lines(lead, _LEAD_CHARS) * _LEAD_LINE_PX
    if footnote:
        budget -= 40 + _lines(footnote, _FOOTNOTE_CHARS) * _FOOTNOTE_LINE_PX
    heights = [
        _row_px(row.get("_cells") or (row["subject"], row["description"], row["verdict"]), chars)
        for row in rows
    ]
    page_count = max(1, math.ceil(len(rows) / max_rows))
    while True:
        chunks = _balanced(list(range(len(rows))), math.ceil(len(rows) / page_count))
        if page_count >= len(rows) or all(sum(heights[i] for i in chunk) <= budget for chunk in chunks):
            return [[rows[i] for i in chunk] for chunk in chunks]
        page_count += 1


def _item_px(item: dict[str, Any]) -> float:
    lines = sum(_lines(item[key], _M5_TEXT_CHARS) for key in ("solution", "how_it_works", "result", "signal"))
    lines += sum(_lines(question, _M5_TEXT_CHARS) for question in item["questions"])
    return _M5_ITEM_FIXED_PX + lines * _M5_LINE_PX


def _paginate_items(items: list[dict[str, Any]], lead: str) -> list[list[dict[str, Any]]]:
    """Two deep dives per page; a pair that would not fit is set one per page instead."""
    budget = _PAGE_BODY_PX - _RUNNING_HEAD_PX - _M5_HEAD_PX - (_lines(lead, 100) - 1) * 20
    chunks: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(items):
        pair = items[index : index + M5_ITEMS_PER_PAGE]
        if len(pair) > 1 and sum(_item_px(item) for item in pair) > budget:
            pair = pair[:1]
        chunks.append(pair)
        index += len(pair)
    return chunks


_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


# Research record versions that print the company background page (1.1: BAP-10, 1.2: BAP-9 wording).
_BACKGROUND_VERSIONS = ("1.1", "1.2")


def background_lead(paper: dict[str, Any]) -> str:
    """The lead of the company background table.

    The layout reads it whenever pages are rebuilt, and a stored document's pages must rebuild
    exactly, so the wording is versioned with the research record: analyses recorded with 1.1
    keep the BAP-10 sentence; 1.2 (BAP-9) names the company and speaks to it.
    """
    overview = load_library()["chapters"]["overview"]
    if ((paper.get("generation") or {}).get("website_research") or {}).get("schema_version") == "1.1":
        return overview["background_lead_1_1"]
    company = str((paper.get("intake_context") or {}).get("client_name") or "").strip() or "the company"
    lead = overview["background_lead"].replace("{company}", company)
    return lead[0].upper() + lead[1:]


def company_background(paper: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """BAP-10: the website facts as M6 rows - topic, quoted statement, source page - and the note.

    Only for analyses whose website research succeeded and was recorded in version 1.1 or later;
    earlier analyses keep exactly the pages they were stored with.
    """
    generation = paper.get("generation") or {}
    research = generation.get("website_research") or {}
    if research.get("schema_version") not in _BACKGROUND_VERSIONS or generation.get("research_mode") != "provider" or not paper.get("analysis"):
        return [], None
    rows = []
    for index, fact in enumerate(paper["analysis"]["research"]["known_facts"], start=1):
        if fact["origin"] != "SOURCE_FACT" or not fact["source_refs"]:
            continue
        url = str(fact["source_refs"][0]["source_id"])
        rows.append(
            {
                "id": f"website_fact_{index}",
                "subject": fact["label"].removesuffix(" (website)"),
                "description": fact["value"],
                "verdict": source_label(url),
                "source_url": url,
                "grouped": False,
            }
        )
    if not rows:
        return [], None
    template = load_library()["chapters"]["overview"]["background_footnote"]
    return rows, template.format(site=research.get("site") or source_label(rows[0]["source_url"]), date=_long_date(research.get("researched_at")))


def source_label(url: str) -> str:
    """A source address as printed: host and path, short enough for the source column."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path.rstrip("/")
    label = f"{host}{path}" if path else host
    if len(label) <= 44:
        return label
    last = path.rsplit("/", 1)[-1]
    return f"{host}/…/{last[:24]}" if last else label[:43] + "…"


def _long_date(value: Any) -> str:
    text = str(value or "")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-" and text[5:7].isdigit() and 1 <= int(text[5:7]) <= 12:
        return f"{int(text[8:10])} {_MONTHS[int(text[5:7]) - 1]} {text[0:4]}"
    return "the date of the analysis"


def _month_year(generated_at: Any) -> tuple[str, str]:
    text = str(generated_at or "")
    if len(text) >= 7 and text[4] == "-":
        return text[5:7], text[0:4]
    return "", ""
