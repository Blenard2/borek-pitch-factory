"""Build PPT #2 SlideSpecs from a frozen BT-46 planner input.

This is the content path. It does not read or write a FrameworkObject.
Each slide's text comes only from the sources named by its references.
"""

from __future__ import annotations

from typing import Any

from services.presentation.first_pitch import PAGE_LAYOUTS
from services.presentation.post_meeting import (
    EXTRACTION_LAYOUT,
    MEETING_SECTION_KEYS,
    NOTES_LAYOUT,
    USE_CASE_LAYOUT,
    customer_ppt2_input,
)

_SKIP_CONTENT_KEYS = frozenset({"origin", "status", "schema_version"})


def build_post_meeting_slide_specs(
    plan_json: dict[str, Any],
    ppt2_context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Ground each planned PPT #2 slide in the frozen context, without the owner's notes (BUG-02)."""
    ppt2_context = customer_ppt2_input(ppt2_context)
    specs: list[dict[str, Any]] = []
    for planned in plan_json.get("slides") or []:
        references = [str(item) for item in planned.get("frameworkReferences") or []]
        layout_id = str(planned.get("layoutId") or "")
        order = int(planned.get("order") or len(specs) + 1)
        lines = _lines_for_references(ppt2_context, references)
        specs.append(_spec_for_layout(layout_id, order, references, lines))
    return specs


def _lines_for_references(ppt2_context: dict[str, Any], references: list[str]) -> list[str]:
    lines: list[str] = []
    pages = {
        str(page.get("key")): page
        for page in ppt2_context["approved_discovery"]["pages"]
    }
    meeting = (ppt2_context.get("meeting_extraction") or {}).get("extraction") or {}
    use_cases = {
        str(item.get("fact_id")): item
        for item in (ppt2_context.get("selected_use_cases") or {}).get("use_cases") or []
        if isinstance(item, dict)
    }
    for reference in references:
        if reference.startswith("discovery."):
            page = pages.get(reference.removeprefix("discovery.")) or {}
            lines.extend(_iter_strings(page.get("content")) or [str(page.get("title") or reference)])
        elif reference == "notes":
            text = str((ppt2_context.get("personal_notes") or {}).get("text") or "").strip()
            if text:
                lines.append(text)
        elif reference.startswith("meeting."):
            key = reference.removeprefix("meeting.")
            label = key.replace("_", " ")
            for item in meeting.get(key) or []:
                text = str(item).strip()
                if text:
                    lines.append(f"{label}: {text}")
        elif reference.startswith("use_case."):
            fact_id = reference.removeprefix("use_case.")
            item = use_cases.get(fact_id) or {}
            statement = str(item.get("statement") or fact_id).strip()
            lines.append(statement)
    if not lines:
        lines.append(references[0] if references else "Post-meeting")
    return lines


def _spec_for_layout(
    layout_id: str,
    order: int,
    references: list[str],
    lines: list[str],
) -> dict[str, Any]:
    title = _clip(lines[0])
    detail = _clip(lines[1] if len(lines) > 1 else lines[0])
    base = {
        "schema_version": "1.0",
        "layoutId": layout_id,
        "title": title,
        "sourceChapterIds": list(references),
        "slideId": f"slide_{order:02d}",
    }
    if layout_id == "COVER_01":
        return {
            **base,
            "subtitle": detail,
            "statBadges": [{"value": "Approved", "label": "Discovery"}],
        }
    if layout_id == "CONTEXT_01":
        return {
            **base,
            "problem": _block("Client context", lines[0]),
            "solution": _block("What we know", detail),
            "currentState": _block("Current picture", _clip(_join(lines[:2]))),
            "targetState": _block("Discussion aim", _clip(_join(lines[2:]) or detail)),
        }
    if layout_id == "PROBLEM_SOLUTION_01":
        return {
            **base,
            "problem": _block("Opportunity", lines[0]),
            "solution": _block("Direction", detail),
        }
    if layout_id == "SCOPE_01":
        return {
            **base,
            "included": [_clip(line) for line in lines[:3]],
            "later": [_clip(lines[-1])],
        }
    if layout_id == "EXECUTIVE_SUMMARY_01":
        return {
            **base,
            "headline": title,
            "highlights": [
                {"title": _clip(line), "description": detail} for line in lines[:3]
            ],
        }
    if layout_id == "MILESTONES_01":
        return {
            **base,
            "milestones": [
                {"name": _clip(line), "description": detail} for line in lines[:3]
            ],
        }
    if layout_id == "NEXT_STEPS_01":
        return {
            **base,
            "checklist": [_clip(line) for line in lines[:3]],
            "steps": [
                {"number": index, "text": _clip(line)}
                for index, line in enumerate(lines[:3], start=1)
            ],
            "darkBackground": False,
        }
    if layout_id == NOTES_LAYOUT:
        note = _clip(lines[0], limit=400)
        return {
            **base,
            "title": "Personal notes",
            "left": {"heading": "Owner notes", "items": [note]},
            "right": {"heading": "Owner notes", "items": [note]},
        }
    if layout_id == EXTRACTION_LAYOUT:
        return {
            **base,
            "title": "Meeting extraction",
            "requirements": [
                {
                    "category": _category(line),
                    "title": _clip(line),
                    "status": "included",
                }
                for line in lines[:8]
            ],
        }
    if layout_id == USE_CASE_LAYOUT:
        return {
            **base,
            "title": "Selected use cases",
            "components": [
                {
                    "number": index,
                    "title": _clip(reference.removeprefix("use_case."), limit=80),
                    "description": _clip(line),
                }
                for index, (reference, line) in enumerate(
                    zip(references, lines, strict=False),
                    start=1,
                )
            ],
        }
    if layout_id in PAGE_LAYOUTS.values():
        raise ValueError(f"PPT #2 has no content mapping for {layout_id}")
    raise ValueError(f"PPT #2 has no content mapping for {layout_id}")


def _category(line: str) -> str:
    head = line.split(":", 1)[0].strip()
    normalized = head.replace(" ", "_")
    if normalized in MEETING_SECTION_KEYS:
        return head
    return "Meeting"


def _block(title: str, description: str) -> dict[str, str]:
    return {"title": title, "description": _clip(description)}


def _iter_strings(value: Any) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            text = " ".join(node.split())
            if text:
                found.append(text)
            return
        if isinstance(node, dict):
            for key, item in node.items():
                if key in _SKIP_CONTENT_KEYS:
                    continue
                walk(item)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


def _join(lines: list[str]) -> str:
    return " ".join(line for line in lines if line).strip()


def _clip(text: str, limit: int = 240) -> str:
    compact = " ".join(str(text).split())
    if not compact:
        return "Post-meeting"
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"
