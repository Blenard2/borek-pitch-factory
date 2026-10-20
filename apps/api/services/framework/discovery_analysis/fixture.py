"""Deterministic (fixture) analysis: the opportunity library shaped by what the user provided.

Nothing here is a statement about the customer. Company-specific content is limited to the
user's own input; everything else is a typical pattern or an industry benchmark.
"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from services.framework.discovery_analysis.model import LIMITS

LIBRARY_PATH = Path(__file__).with_name("library.json")

_FACT_LABELS = (
    ("client_name", "Company name"),
    ("contact_name", "Contact person"),
    ("website_url", "Website"),
    ("meeting_purpose", "Meeting purpose"),
    ("additional_information", "Additional information"),
)


@lru_cache(maxsize=1)
def _library_text() -> str:
    return LIBRARY_PATH.read_text(encoding="utf-8")


def load_library() -> dict[str, Any]:
    return json.loads(_library_text())


def build_fixture_analysis(context: dict[str, Any], optional_parts: dict[str, bool]) -> dict[str, Any]:
    library = load_library()
    client = context["client_name"]
    purpose = context["meeting_purpose"]
    additional = context["additional_information"] or ""
    areas = _areas(library, f"{purpose} {additional}")
    opportunity_count = sum(len(area["opportunities"]) for area in areas)
    counts = {"area_count": len(areas), "opportunity_count": opportunity_count}
    chapters = library["chapters"]
    cover = library["cover"]
    in_short = library["in_short"]
    provided = [(label, context[key]) for key, label in _FACT_LABELS if context[key]]

    profile_text = _fit_sentences(additional, LIMITS["chapter_paragraph"] - len(chapters["overview"]["profile_provided"]))
    profile_paragraph = (
        chapters["overview"]["profile_provided"].format(text=profile_text)
        if profile_text
        else chapters["overview"]["profile_unknown"]
    )
    if client:
        scope = _named(in_short["scope_named"], in_short["scope_context"], client, LIMITS["scope_note"])
    elif provided:
        scope = in_short["scope_context"]
    else:
        scope = in_short["scope_generic"]
    thesis = library["thesis"]["generic"]
    if purpose:
        with_purpose = library["thesis"]["with_purpose"].format(purpose=purpose)
        if len(with_purpose) <= LIMITS["thesis"]:
            thesis = with_purpose

    return {
        "research": {
            "company_profile": {
                "text": profile_text or None,
                "origin": "USER_INPUT" if profile_text else "UNKNOWN",
            },
            "core_thesis": {"text": thesis, "origin": "WORKING_HYPOTHESIS"},
            "known_facts": [
                {"label": label, "value": value, "origin": "USER_INPUT", "source_refs": []}
                for label, value in provided
            ],
            "unknown_facts": [label for key, label in _FACT_LABELS if not context[key]]
            + library["research"]["always_unknown"],
            "web_research": {"performed": False, "note": library["research"]["web_research_note"]},
        },
        "framing": {
            "document": {
                "eyebrow": cover["eyebrow"],
                "title": _named(cover["title_named"], cover["title_generic"], client, LIMITS["cover_title"]),
                "subtitle": cover["subtitle"].format(**counts),
                "short_title": cover["short_title"],
                "document_type": cover["document_type"],
                "in_short": [
                    in_short["first_statement"].format(**counts),
                    in_short["second_statement"],
                    in_short["third_statement"],
                ],
                "scope_note": scope,
            },
            "chapters": {
                "overview": {
                    "name": chapters["overview"]["name"],
                    "headline": _named(
                        chapters["overview"]["headline_named"],
                        chapters["overview"]["headline_generic"],
                        client,
                        LIMITS["chapter_headline"],
                    ),
                    "paragraphs": [profile_paragraph, chapters["overview"]["context_paragraph"]],
                    "key_message": chapters["overview"]["key_message"],
                },
                "deep_dive": {
                    "name": chapters["deep_dive"]["name"],
                    "headline": _named(
                        chapters["deep_dive"]["headline_named"],
                        chapters["deep_dive"]["headline_generic"],
                        client,
                        LIMITS["chapter_headline"],
                    ),
                    "paragraphs": list(chapters["deep_dive"]["paragraphs"]),
                    "key_message": chapters["deep_dive"]["key_message"],
                },
                "shadow": _chapter(chapters["shadow"]),
                "target": _chapter(chapters["target"]),
            },
            "leads": {
                "map": chapters["overview"]["map_lead"].format(count=opportunity_count),
                "business_case": chapters["overview"]["business_case_lead"],
                "business_case_footnote": chapters["overview"]["business_case_footnote"],
                "shadow_table": chapters["shadow"]["table_lead"],
                "shadow_footnote": chapters["shadow"]["table_footnote"],
                "shadow_findings": chapters["shadow"]["findings_lead"],
                "target_table": chapters["target"]["table_lead"].format(count=len(library["target_workflows"])),
                "target_footnote": chapters["target"]["table_footnote"],
                "target_findings": chapters["target"]["findings_lead"],
                "closing_steps": chapters["closing"]["steps_lead"],
            },
        },
        "areas": areas,
        "shadow_processes": copy.deepcopy(library["shadow_processes"]),
        "shadow_findings": copy.deepcopy(library["shadow_findings"]),
        "target_workflows": copy.deepcopy(library["target_workflows"]),
        "target_picture": copy.deepcopy(library["target_picture"]),
        # Parts 5 and 6 need company-specific role and system knowledge. The library has none,
        # so they are produced by live generation only; fixture mode never fabricates them.
        "optional_deep_dives": {key: None for key in optional_parts},
        "closing": {
            "name": chapters["closing"]["name"],
            "headline": chapters["closing"]["headline"],
            "paragraphs": list(chapters["closing"]["paragraphs"]),
            "steps": copy.deepcopy(chapters["closing"]["steps"]),
        },
        "provenance": {
            "content_origin": "fixture_library",
            "figures_basis": "industry_benchmark_or_working_hypothesis",
            "customer_facts_origin": "user_input_only" if provided else "none",
            "sources": [],
            "notes": list(library["research"]["notes"]),
        },
    }


# BAP-5: which library area a website signal points at. A signal is an interpretation of the
# site's wording; it only moves an area forward, it is never shown as a finding.
SIGNAL_AREAS = {
    "quoting_sales": "sales_quoting",
    "customer_service": "order_service",
    "order_processing": "order_service",
    "logistics": "order_service",
    "production": "operations_planning",
    "field_service": "operations_planning",
    "finance": "finance_controlling",
    "documents_compliance": "quality_compliance",
    "recruiting": "knowledge_people",
}


def prioritize_by_website(areas: list[dict[str, Any]], signals: list[dict[str, Any]], limit: int = 2) -> None:
    """Mark the areas the strongest website signals point at as priority (in place, stable order)."""
    wanted: list[str] = []
    for signal in signals:
        area_id = SIGNAL_AREAS.get(str(signal.get("key")))
        if area_id and area_id not in wanted:
            wanted.append(area_id)
        if len(wanted) == limit:
            break
    for area in areas:
        if area["id"] in wanted:
            area["priority"] = True
    areas[:] = [area for area in areas if area["priority"]] + [area for area in areas if not area["priority"]]


def researched_profile_paragraph(text: str) -> str | None:
    """The overview's profile paragraph from a description quoted from the client's website."""
    template = load_library()["chapters"]["overview"]["profile_researched"]
    fitted = _fit_sentences(text, LIMITS["chapter_paragraph"] - len(template))
    return template.format(text=fitted) if fitted else None


def _areas(library: dict[str, Any], user_text: str) -> list[dict[str, Any]]:
    """Library areas; the ones the user's own words point at come first."""
    haystack = user_text.lower()
    areas = []
    for area in library["areas"]:
        priority = any(
            re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", haystack)
            for keyword in area["keywords"]
        )
        areas.append(
            {
                "id": area["id"],
                "name": area["name"],
                "lead": area["lead"],
                "priority": priority,
                "opportunities": copy.deepcopy(area["opportunities"]),
                "business_case": copy.deepcopy(area["business_case"]),
            }
        )
    return [area for area in areas if area["priority"]] + [area for area in areas if not area["priority"]]


def _chapter(chapter: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": chapter["name"],
        "headline": chapter["headline"],
        "paragraphs": list(chapter["paragraphs"]),
        "key_message": chapter["key_message"],
    }


def _named(named: str, generic: str, client: str, limit: int) -> str:
    """The company-specific wording when a name exists and fits the slot, else the generic one."""
    if client:
        text = named.format(client=client)
        if len(text) <= limit:
            return text
    return generic


def _fit_sentences(text: str, limit: int) -> str:
    """Leading whole sentences of the user's text that fit; the full text stays in the intake."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    kept = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > limit:
            break
        kept = candidate
    return kept or text[: limit - 1].rsplit(" ", 1)[0] + "…"
