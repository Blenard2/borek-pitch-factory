"""Discovery v2 - the structured AI Opportunity Analysis: constants, context and validation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from services.framework.stage1_intake import meeting_purpose_source, resolve_meeting_purpose

SCHEMA_VERSION = "2.0"
SCHEMA_PATH = (
    Path(__file__).resolve().parents[5] / "packages" / "contracts" / "discovery_analysis.schema.json"
)
PAGE_TYPES = ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9")

# Brand constants of the white paper master: they appear verbatim on every paper.
CLOSING_LINE = "Let us establish the first baseline together."
BRAND_CLAIM = "Your AI Department. Delivered, not built."

STAGES: tuple[tuple[str, str], ...] = (
    ("research", "Research & core thesis"),
    ("overview", "Areas & business case"),
    ("deep_dives", "Opportunity deep dives"),
    ("shadow_processes", "Processes outside the systems"),
    ("target_picture", "Target picture"),
    ("optional_parts", "Optional deep dives"),
    ("closing", "Closing"),
    ("validation", "Validation"),
    ("layout", "Page layout"),
)

# Counts required by the guide for a full analysis.
AREA_RANGE = (6, 8)
OPPORTUNITIES_PER_AREA = (3, 4)
OPPORTUNITY_RANGE = (20, 30)
SHADOW_RANGE = (8, 9)
WORKFLOW_RANGE = (5, 6)

# Pagination limits of the white paper master.
M5_ITEMS_PER_PAGE = 2
M6_ROWS_PER_PAGE = 8
M7_ROWS_PER_PAGE = 5

# Character budgets from the master ("up to ~n characters"). Pages hide overflow, so content
# beyond these budgets is rejected instead of being clipped silently.
LIMITS: dict[str, int] = {
    # BAP-9 Phase 2: the eyebrow can carry the company's industry; it is also every running head.
    "eyebrow": 52,
    "cover_title": 70,
    "cover_subtitle": 220,
    "short_title": 44,
    "in_short": 120,
    "scope_note": 320,
    "chapter_name": 44,
    "chapter_headline": 110,
    "chapter_paragraph": 600,
    "key_message": 140,
    "page_lead": 190,
    "area_name": 44,
    "area_lead": 150,
    "item_title": 44,
    "solution_block": 270,
    "how_it_works": 220,
    "result": 150,
    "question": 120,
    "signal": 150,
    "map_cell": 200,
    "table_subject": 70,
    "table_description": 280,
    "table_verdict": 80,
    "footnote": 440,
    "diagram_title": 60,
    "diagram_lead": 200,
    "workflow_name": 28,
    "workflow_stage": 34,
    "workflow_gate": 48,
    "card_label": 30,
    "card_value": 22,
    "card_text": 240,
    "point_title": 70,
    "point_text": 330,
    "closing_paragraph": 430,
    "shadow_approach": 140,
    "thesis": 240,
}

_NUMBER_RE = re.compile(r"\d[\d.,]*")
# Framing that marks a figure as a benchmark or working hypothesis rather than a fact.
_FRAMING_WORDS = ("benchmark", "typical", "hypothes", "to be validated")
_SCALE = r"(?:\s?(?:k|m|bn|million|billion)\b)?"
# Quantified claims: money, percentages, headcount and volumes or rates per period.
_FIGURE_RE = re.compile(
    rf"(?:[€$£]\s?\d[\d.,]*{_SCALE}"
    rf"|\b(?:EUR|USD|GBP|CHF)\s?\d[\d.,]*{_SCALE}"
    rf"|\d[\d.,]*{_SCALE}\s?(?:€|(?:EUR|USD|GBP|CHF)\b)"
    r"|\d[\d.,]*\s?%"
    r"|\d[\d.,]*\s?(?:FTEs?|employees|staff|headcount)\b"
    r"|\d[\d.,]*\s(?:[a-z]+\s){0,2}(?:per|a|each)\s(?:day|week|month|year|annum)\b)",
    re.IGNORECASE,
)
_COMMITMENT_RE = re.compile(r"\b(?:guarantee[ds]?|we commit|will save|will reduce|will cut|promised?)\b", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


class DiscoveryAnalysisError(ValueError):
    """The analysis violates the contract, the guide or the master's limits."""


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def is_v2(paper: Any) -> bool:
    return isinstance(paper, dict) and paper.get("schema_version") == SCHEMA_VERSION


def intake_context(opportunity: dict[str, Any]) -> dict[str, Any]:
    """What the user actually provided. Missing values stay empty or null."""
    raw = opportunity.get("stage1_intake")
    nested = raw if isinstance(raw, dict) else {}
    additional = str(nested.get("about_company") or "").strip()
    contact = str(nested.get("poc_name") or "").strip()
    website = str(nested.get("client_web_page") or "").strip()
    return {
        "client_name": str(opportunity.get("client_name") or "").strip(),
        "contact_name": contact or None,
        "website_url": website or None,
        "meeting_purpose": resolve_meeting_purpose(opportunity),
        "meeting_purpose_source": meeting_purpose_source(opportunity) or "unavailable",
        "additional_information": additional or None,
    }


def specificity(context: dict[str, Any]) -> str:
    """How specific the analysis can honestly be. Missing context never blocks generation."""
    if context["client_name"]:
        return "company"
    if context["meeting_purpose"] or context["additional_information"] or context["website_url"]:
        return "context"
    return "generic"


def initial_stages(optional_parts: dict[str, bool]) -> list[dict[str, str]]:
    wants_optional = any(optional_parts.values())
    return [
        {
            "key": key,
            "label": label,
            "status": "skipped" if key == "optional_parts" and not wants_optional else "waiting",
        }
        for key, label in STAGES
    ]


def default_optional_parts() -> dict[str, bool]:
    return {"roles_employees": False, "decision_map": False, "system_interfaces": False}


def empty_discovery_analysis(opportunity: dict[str, Any]) -> dict[str, Any]:
    context = intake_context(opportunity)
    optional_parts = default_optional_parts()
    paper = {
        "schema_version": SCHEMA_VERSION,
        "opportunity_id": str(opportunity["id"]),
        "document_id": None,
        "latest_approved_version_id": None,
        "status": "not_generated",
        "generated_at": None,
        "language": "en",
        "intake_context": context,
        "generation": {
            "mode": "fixture",
            "specificity": specificity(context),
            "research_mode": "user_context_only",
            "optional_parts": optional_parts,
            "stages": initial_stages(optional_parts),
        },
        "analysis": None,
        "presentation_brief": None,
        "page_manifest": [],
    }
    validate_discovery_analysis(paper)
    return paper


def validate_discovery_analysis(paper: dict[str, Any]) -> dict[str, Any]:
    """Schema, guide rules and master limits. Raises DiscoveryAnalysisError with every finding."""
    errors = sorted(
        Draft202012Validator(load_schema(), format_checker=FormatChecker()).iter_errors(paper),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path) or "<root>"
        raise DiscoveryAnalysisError(f"{location}: {first.message.splitlines()[0]}")
    problems: list[str] = []
    analysis = paper["analysis"]
    stages = paper["generation"]["stages"]
    if paper["status"] == "ready":
        if analysis is None:
            problems.append("A ready analysis must contain content")
        if any(stage["status"] not in {"ready", "skipped"} for stage in stages):
            problems.append("A ready analysis requires every generation stage to be ready")
        if paper["presentation_brief"] is None:
            problems.append("A ready analysis must carry its presentation brief")
        if not paper["page_manifest"]:
            problems.append("A ready analysis must carry its page manifest")
    if any(stage["status"] == "failed" for stage in stages) and paper["status"] != "failed":
        problems.append("A failed stage requires status failed")
    if analysis is not None and paper["status"] == "ready":
        problems.extend(content_problems(paper))
        problems.extend(manifest_problems(paper))
    if problems:
        raise DiscoveryAnalysisError("; ".join(problems))
    return paper


def content_problems(paper: dict[str, Any]) -> list[str]:
    """Guide rules: structure, counts, benchmark framing and no invented customer facts."""
    analysis = paper["analysis"]
    generation = paper["generation"]
    context = paper["intake_context"]
    problems: list[str] = []
    areas = analysis["areas"]
    opportunities = [item for area in areas for item in area["opportunities"]]
    _count(problems, "areas", len(areas), AREA_RANGE)
    _count(problems, "opportunities", len(opportunities), OPPORTUNITY_RANGE)
    for area in areas:
        _count(problems, f"opportunities in area {area['id']}", len(area["opportunities"]), OPPORTUNITIES_PER_AREA)
    _count(problems, "shadow processes", len(analysis["shadow_processes"]), SHADOW_RANGE)
    _count(problems, "target workflows", len(analysis["target_workflows"]), WORKFLOW_RANGE)
    for label, ids in (
        ("area", [area["id"] for area in areas]),
        ("opportunity", [item["id"] for item in opportunities]),
        ("shadow process", [item["id"] for item in analysis["shadow_processes"]]),
        ("target workflow", [item["id"] for item in analysis["target_workflows"]]),
    ):
        if len(set(ids)) != len(ids):
            problems.append(f"Duplicate {label} ids")
    for workflow in analysis["target_workflows"]:
        if not workflow["human_decision_gate"].strip():
            problems.append(f"Workflow {workflow['id']} has no human decision gate")

    # Benchmark framing: figures are benchmarks or working hypotheses, never commitments.
    framing = analysis["framing"]
    footnote = framing["leads"]["business_case_footnote"].lower()
    scope = framing["document"]["scope_note"].lower()
    if "benchmark" not in footnote or "hypothes" not in footnote or "not commitments" not in footnote:
        problems.append("The business-case footnote must frame figures as benchmarks and working hypotheses, not commitments")
    if "benchmark" not in scope or "not commitments" not in scope:
        problems.append("The scope note must frame figures as benchmarks, not commitments")
    problems.extend(figure_problems(paper))

    # Customer facts: only what the user provided, or cited research. Nothing else.
    research = analysis["research"]
    provided = {
        str(value).strip()
        for value in (
            context["client_name"],
            context["contact_name"],
            context["website_url"],
            context["meeting_purpose"],
            context["additional_information"],
        )
        if value and str(value).strip()
    }
    researched = generation["research_mode"] == "provider"
    if research["web_research"]["performed"] != researched:
        problems.append("web_research.performed must match the research mode")
    for fact in research["known_facts"]:
        if fact["origin"] == "USER_INPUT" and fact["value"] not in provided:
            problems.append(f"Known fact '{fact['label']}' is not something the user provided")
        if fact["origin"] == "SOURCE_FACT" and (not researched or not fact["source_refs"]):
            problems.append(f"Known fact '{fact['label']}' claims a source that does not exist")
    profile = research["company_profile"]
    if profile["origin"] == "UNKNOWN" and profile["text"] is not None:
        problems.append("An unknown company profile must not carry text")
    if profile["origin"] != "UNKNOWN" and not profile["text"]:
        problems.append("A company profile needs text or the origin UNKNOWN")
    if profile["origin"] == "SOURCE_FACT" and not researched:
        problems.append("A researched company profile requires a research provider")
    if profile["origin"] == "USER_INPUT":
        allowed_numbers = set(_NUMBER_RE.findall(" ".join(sorted(provided))))
        invented = [n for n in _NUMBER_RE.findall(profile["text"] or "") if n not in allowed_numbers]
        if invented:
            problems.append(f"The company profile states figures the user did not provide: {', '.join(invented)}")
    expected_origin = (
        "user_input_and_cited_research" if researched else "user_input_only" if provided else "none"
    )
    if analysis["provenance"]["customer_facts_origin"] != expected_origin:
        problems.append(f"provenance.customer_facts_origin must be {expected_origin}")

    # Parts 5 and 6 exist only when explicitly requested.
    optional = analysis["optional_deep_dives"]
    for key, requested in generation["optional_parts"].items():
        if requested != (optional[key] is not None):
            problems.append(f"Optional part {key} does not match what was requested")

    problems.extend(length_problems(analysis))
    return problems


def length_problems(analysis: dict[str, Any]) -> list[str]:
    """Every text against the master's character budget for the slot it is printed in."""
    out: list[str] = []

    def check(where: str, text: Any, limit_key: str) -> None:
        length = len(str(text or ""))
        if length > LIMITS[limit_key]:
            out.append(f"{where} is {length} characters (limit {LIMITS[limit_key]})")

    framing = analysis["framing"]
    document = framing["document"]
    check("cover eyebrow", document["eyebrow"], "eyebrow")
    check("cover title", document["title"], "cover_title")
    check("cover subtitle", document["subtitle"], "cover_subtitle")
    check("short title", document["short_title"], "short_title")
    for index, statement in enumerate(document["in_short"], start=1):
        check(f"in-short statement {index}", statement, "in_short")
    check("scope note", document["scope_note"], "scope_note")
    chapters = dict(framing["chapters"])
    for key, table in analysis["optional_deep_dives"].items():
        if table is not None:
            chapters[key] = table["chapter"]
            check(f"{key} lead", table["lead"], "page_lead")
            check(f"{key} footnote", table["footnote"], "footnote")
            for row in table["rows"]:
                check(f"{key} row {row['id']} subject", row["subject"], "table_subject")
                check(f"{key} row {row['id']} description", row["description"], "table_description")
                check(f"{key} row {row['id']} verdict", row["verdict"], "table_verdict")
    for key, chapter in chapters.items():
        check(f"chapter {key} name", chapter["name"], "chapter_name")
        check(f"chapter {key} headline", chapter["headline"], "chapter_headline")
        check(f"chapter {key} key message", chapter["key_message"], "key_message")
        for index, paragraph in enumerate(chapter["paragraphs"], start=1):
            check(f"chapter {key} paragraph {index}", paragraph, "chapter_paragraph")
    for key, lead in framing["leads"].items():
        check(f"lead {key}", lead, "footnote" if key.endswith("footnote") else "page_lead")
    check("core thesis", analysis["research"]["core_thesis"]["text"], "thesis")
    for area in analysis["areas"]:
        check(f"area {area['id']} name", area["name"], "area_name")
        check(f"area {area['id']} lead", area["lead"], "area_lead")
        check(f"area {area['id']} opportunity list", " · ".join(item["title"] for item in area["opportunities"]), "map_cell")
        check(f"area {area['id']} business case", business_case_text(area["business_case"]), "table_description")
        for item in area["opportunities"]:
            where = f"opportunity {item['id']}"
            check(f"{where} title", item["title"], "item_title")
            check(f"{where} solution", solution_text(item), "solution_block")
            check(f"{where} how it works", item["how_it_works"], "how_it_works")
            check(f"{where} result", item["result"], "result")
            check(f"{where} signal", item["opportunity_signal"], "signal")
            for index, question in enumerate(item["discovery_questions"], start=1):
                check(f"{where} question {index}", question, "question")
    for row in analysis["shadow_processes"]:
        check(f"shadow process {row['id']} decision", row["decision"], "table_subject")
        check(f"shadow process {row['id']} AI approach", row["ai_approach"], "shadow_approach")
        check(f"shadow process {row['id']} handled today", row["handled_today_via"], "table_verdict")
    picture = analysis["target_picture"]
    for label, findings in (("shadow findings", analysis["shadow_findings"]), ("target findings", target_findings(picture))):
        for card in findings["cards"]:
            check(f"{label} card label", card["label"], "card_label")
            check(f"{label} card value", card["value"], "card_value")
            check(f"{label} card text", card["text"], "card_text")
        for point in findings["points"]:
            check(f"{label} point title", point["title"], "point_title")
            check(f"{label} point text", point["text"], "point_text")
    check("diagram title", picture["diagram_title"], "diagram_title")
    check("diagram lead", picture["diagram_lead"], "diagram_lead")
    for workflow in analysis["target_workflows"]:
        check(f"workflow {workflow['id']} name", workflow["name"], "workflow_name")
        check(f"workflow {workflow['id']} flow", workflow["autonomous_flow"], "table_description")
        check(f"workflow {workflow['id']} gate", workflow["human_decision_gate"], "workflow_gate")
        for stage in workflow["stages"]:
            check(f"workflow {workflow['id']} stage", stage, "workflow_stage")
    closing = analysis["closing"]
    check("closing name", closing["name"], "chapter_name")
    check("closing headline", closing["headline"], "chapter_headline")
    for index, paragraph in enumerate(closing["paragraphs"], start=1):
        check(f"closing paragraph {index}", paragraph, "closing_paragraph")
    check("closing steps lead", analysis["framing"]["leads"]["closing_steps"], "page_lead")
    for index, step in enumerate(closing["steps"], start=1):
        check(f"closing step {index} title", step["title"], "point_title")
        check(f"closing step {index} text", step["text"], "point_text")
    return out


def manifest_problems(paper: dict[str, Any]) -> list[str]:
    """The stored manifest must be exactly what the layout derives from the content."""
    from services.framework.discovery_analysis.layout import build_page_manifest

    problems: list[str] = []
    manifest = paper["page_manifest"]
    if [page["number"] for page in manifest] != list(range(1, len(manifest) + 1)):
        problems.append("Page numbers must be consecutive from 1")
    if len({page["id"] for page in manifest}) != len(manifest):
        problems.append("Page ids must be unique")
    if any(page["type"] not in PAGE_TYPES for page in manifest):
        problems.append("Only master page types M1-M9 are allowed")
    for previous, current in zip(manifest, manifest[1:]):
        if previous["dark"] and current["dark"]:
            problems.append(f"Two dark pages in a row: {previous['id']} and {current['id']}")
    if manifest != build_page_manifest(paper):
        problems.append("The page manifest does not match the analysis content")
    return problems


def solution_text(opportunity: dict[str, Any]) -> str:
    """Solution as printed: what is built, then the mechanism."""
    return f"{opportunity['solution'].rstrip()} Technically: {opportunity['ai_technology'].rstrip()}"


def payback_label(payback_type: str) -> str:
    return "Quick win · 3–6 mo." if payback_type == "quick_win" else "Strategic · 12–24 mo."


def business_case_text(case: dict[str, Any]) -> str:
    """Value levers joined with middle dots, the outcome, then the benchmark ranges."""
    text = " · ".join(case["value_levers"]) + f" — {case['outcome']}"
    ranges = [
        f"{item['metric']} {item['range']} ({item['basis'].replace('_', ' ')})"
        for item in case["impact_ranges"]
    ]
    return f"{text}. {'; '.join(ranges)}." if ranges else f"{text}."


def target_findings(picture: dict[str, Any]) -> dict[str, Any]:
    levels = picture["maturity_levels"]
    return {
        "cards": [
            picture["dark_processing_pattern"],
            {"label": levels["label"], "value": levels["value"], "text": levels["text"]},
        ],
        "points": picture["points"],
    }


def figure_problems(paper: dict[str, Any]) -> list[str]:
    """Every quantified claim needs framing or provenance.

    The guide wants business cases with impact ranges, so figures - money, percentages,
    headcount, volumes - are welcome. What is not allowed is a figure stated as a fact about
    the customer that nobody supplied. A sentence with a figure is accepted when

    * it is framed as an industry benchmark or working hypothesis, or
    * the figure was provided by the user, or comes from cited research.

    Wording that turns a figure into a commitment is rejected in any case.
    """
    analysis = paper["analysis"]
    context = paper["intake_context"]
    research = analysis["research"]
    supplied_text = " ".join(
        [str(value) for value in context.values() if value]
        + [fact["value"] + " " + " ".join(ref["excerpt"] for ref in fact["source_refs"]) for fact in research["known_facts"] if fact["origin"] == "SOURCE_FACT"]
        + [ref["excerpt"] for ref in analysis["provenance"]["sources"]]
    )
    supplied = set(_NUMBER_RE.findall(supplied_text))
    problems: list[str] = []
    for where, text in _printed_texts(analysis, ""):
        for sentence in _SENTENCE_RE.split(text):
            figures = [match.group(0) for match in _FIGURE_RE.finditer(sentence)]
            if not figures:
                continue
            if _COMMITMENT_RE.search(sentence):
                problems.append(f"{where} presents a figure as a commitment: {figures[0]}")
                continue
            if any(word in sentence.lower() for word in _FRAMING_WORDS):
                continue
            unsupported = [figure for figure in figures if not set(_NUMBER_RE.findall(figure)) <= supplied]
            if unsupported:
                problems.append(
                    f"{where} states a figure without benchmark framing or provenance: {', '.join(unsupported)}"
                )
    return problems


def _printed_texts(node: Any, where: str) -> list[tuple[str, str]]:
    """(location, text) for every printed text. Research and provenance have their own checks;
    impact ranges are framed by their ``basis`` field."""
    if isinstance(node, str):
        return [(where, node)]
    if isinstance(node, list):
        return [pair for index, item in enumerate(node) for pair in _printed_texts(item, f"{where}[{item.get('id', index) if isinstance(item, dict) else index}]")]
    if isinstance(node, dict):
        return [
            pair
            for key, item in node.items()
            if key not in {"research", "provenance", "impact_ranges", "id"}
            for pair in _printed_texts(item, f"{where}.{key}" if where else key)
        ]
    return []


def _count(problems: list[str], label: str, actual: int, bounds: tuple[int, int]) -> None:
    low, high = bounds
    if not low <= actual <= high:
        problems.append(f"{actual} {label} (expected {low}-{high})")
