"""Discovery v2: the AI Opportunity Analysis contract, generation, validation and page layout."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from services.framework.discovery_analysis import live
from services.framework.discovery_analysis.edit import apply_edits, edit_section
from services.framework.discovery_analysis.fixture import load_library
from services.framework.discovery_analysis.layout import build_page_manifest
from services.framework.discovery_analysis.model import (
    BRAND_CLAIM,
    CLOSING_LINE,
    LIMITS,
    DiscoveryAnalysisError,
    empty_discovery_analysis,
    validate_discovery_analysis,
)
from services.framework.discovery_analysis.pipeline import (
    DiscoveryAnalysisStageError,
    finalize,
    generate_discovery_analysis,
)

BLANK = {"id": "11111111-1111-4111-8111-111111111111", "client_name": "", "opportunity_name": "", "stage1_intake": None}
POPULATED = {
    "id": "11111111-1111-4111-8111-111111111112",
    "client_name": "Nordwind Maschinenbau",
    "opportunity_name": "AI introduction",
    "stage1_intake": {
        "client_web_page": "https://nordwind.example",
        "poc_name": "Dana Weber",
        "sales_topic_description": "Reduce quote turnaround and invoice handling effort",
        "about_company": "Family-owned machine builder with 420 employees. Quotes are prepared in spreadsheets.",
    },
}
FIXED = {"generated_at": "2026-10-07T10:00:00Z", "document_id": "22222222-2222-4222-8222-222222222222"}


def generate(opportunity: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return generate_discovery_analysis(opportunity, persist=lambda paper: None, **FIXED, **kwargs)


def pages_of(paper: dict[str, Any], page_type: str) -> list[dict[str, Any]]:
    return [page for page in paper["page_manifest"] if page["type"] == page_type]


def refinalized(paper: dict[str, Any]) -> dict[str, Any]:
    return finalize(copy.deepcopy(paper))


# --- structure and counts -----------------------------------------------------------------


def test_page_count_is_derived_from_content_and_never_fixed_to_seven() -> None:
    paper = generate(BLANK)
    manifest = paper["page_manifest"]
    assert len(manifest) > 7
    assert [page["number"] for page in manifest] == list(range(1, len(manifest) + 1))
    assert "pages" not in paper

    smaller = copy.deepcopy(paper)
    removed = smaller["analysis"]["areas"].pop()
    smaller = refinalized(smaller)
    assert len(smaller["page_manifest"]) < len(manifest)
    assert not any(page["id"].startswith(f"area-{removed['id']}-") for page in smaller["page_manifest"])
    assert manifest == build_page_manifest(paper), "the layout is a pure function of the content"


def test_standard_analysis_has_parts_one_to_four_and_a_closing() -> None:
    paper = generate(POPULATED)
    openers = pages_of(paper, "M3")
    assert [page["id"] for page in openers] == ["ch-overview", "ch-deep-dive", "ch-shadow", "ch-target"]
    assert [page["chapter"]["number"] for page in openers] == [1, 2, 3, 4]
    assert paper["page_manifest"][0]["type"] == "M1"
    assert paper["page_manifest"][1]["type"] == "M2"
    steps, closing = paper["page_manifest"][-2:]
    assert (closing["id"], closing["type"], closing["chapter"]["number"]) == ("closing", "M9", 5)
    assert closing["content"]["closing_line"] == CLOSING_LINE == "Let us establish the first baseline together."
    assert closing["content"]["brand_claim"] == BRAND_CLAIM == "Your AI Department. Delivered, not built."
    # The guide's three steps are printed as numbered points on an M4 page right before M9.
    assert (steps["id"], steps["type"], steps["chapter"]) == ("closing-steps", "M4", closing["chapter"])
    assert [(point["number"], point["title"]) for point in steps["content"]["points"]] == [
        ("01", "Baseline workshop"),
        ("02", "Pilot within weeks"),
        ("03", "Scaling roadmap"),
    ]
    assert all(point["text"].strip() for point in steps["content"]["points"])
    assert steps["content"]["table"]["rows"] == [] and steps["content"]["kpis"] == []
    assert [step["title"] for step in paper["analysis"]["closing"]["steps"]] == [p["title"] for p in steps["content"]["points"]]
    order = [page["id"].split("-")[0] for page in paper["page_manifest"]]
    assert order.index("overview") < order.index("area") < order.index("shadow") < order.index("target")


def test_areas_and_opportunities_meet_the_guide_counts() -> None:
    analysis = generate(BLANK)["analysis"]
    areas = analysis["areas"]
    opportunities = [item for area in areas for item in area["opportunities"]]
    assert 6 <= len(areas) <= 8
    assert 20 <= len(opportunities) <= 30
    assert all(3 <= len(area["opportunities"]) <= 4 for area in areas)
    assert len({item["id"] for item in opportunities}) == len(opportunities)


def test_every_opportunity_has_the_complete_deep_dive() -> None:
    analysis = generate(BLANK)["analysis"]
    for area in analysis["areas"]:
        assert area["business_case"]["value_levers"] and area["business_case"]["payback_type"] in {"quick_win", "strategic"}
        for item in area["opportunities"]:
            for field in ("title", "solution", "ai_technology", "how_it_works", "result", "opportunity_signal"):
                assert item[field].strip(), (item["id"], field)
            assert 2 <= len(item["discovery_questions"]) <= 3
            assert all(question.endswith("?") for question in item["discovery_questions"])


def test_shadow_processes_and_workflows_meet_the_guide_counts() -> None:
    analysis = generate(BLANK)["analysis"]
    assert 8 <= len(analysis["shadow_processes"]) <= 9
    assert all(row["decision"] and row["handled_today_via"] and row["ai_approach"] for row in analysis["shadow_processes"])
    assert 5 <= len(analysis["target_workflows"]) <= 6
    for workflow in analysis["target_workflows"]:
        assert len(workflow["stages"]) == 3
        assert workflow["human_decision_gate"].strip(), "every chain keeps a human decision gate"
    picture = analysis["target_picture"]
    assert picture["dark_processing_pattern"]["text"] and picture["learning_loop"]
    assert len(picture["maturity_levels"]["levels"]) == 3


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda a: a["areas"].__delitem__(slice(5, None)), "areas (expected 6-8)"),
        (lambda a: a["shadow_processes"].__delitem__(slice(7, None)), "shadow processes (expected 8-9)"),
        (lambda a: a["target_workflows"].__delitem__(slice(4, None)), "target workflows (expected 5-6)"),
        (lambda a: a["areas"][0]["opportunities"].__delitem__(slice(2, None)), "opportunities in area"),
        (lambda a: a["target_workflows"][0].__setitem__("id", a["target_workflows"][1]["id"]), "Duplicate target workflow ids"),
    ],
)
def test_validator_rejects_counts_outside_the_guide(mutate, message: str) -> None:
    paper = generate(BLANK)
    mutate(paper["analysis"])
    with pytest.raises(DiscoveryAnalysisError, match=message.replace("(", r"\(").replace(")", r"\)")):
        refinalized(paper)


# --- pagination ---------------------------------------------------------------------------


def test_only_master_page_types_are_used_and_dark_pages_never_touch() -> None:
    manifest = generate(POPULATED)["page_manifest"]
    assert {page["type"] for page in manifest} == {"M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9"}
    assert all(page["dark"] == (page["type"] in {"M1", "M3", "M9"}) for page in manifest)
    assert not any(a["dark"] and b["dark"] for a, b in zip(manifest, manifest[1:]))
    assert len({page["id"] for page in manifest}) == len(manifest)


def test_deep_dives_hold_at_most_two_opportunities_per_page_and_lose_none() -> None:
    paper = generate(BLANK)
    for area in paper["analysis"]["areas"]:
        pages = [page for page in paper["page_manifest"] if page["id"].startswith(f"area-{area['id']}-")]
        assert all(1 <= len(page["content"]["items"]) <= 2 for page in pages)
        printed = [item["id"] for page in pages for item in page["content"]["items"]]
        assert printed == [item["id"] for item in area["opportunities"]]
        assert [item["number"] for page in pages for item in page["content"]["items"]] == [
            f"{n:02d}" for n in range(1, len(printed) + 1)
        ]
        if len(pages) > 1:
            assert pages[0]["content"]["title"] == f"{area['name']} (1/{len(pages)})"
        assert {page["content"]["lead"] for page in pages} == {area["lead"]}


def test_tables_hold_at_most_eight_rows_per_page_and_lose_none() -> None:
    paper = generate(BLANK)
    extra = copy.deepcopy(paper["analysis"]["shadow_processes"][0])
    extra["id"] = "ninth_process"
    paper["analysis"]["shadow_processes"].append(extra)
    paper = refinalized(paper)
    tables = [page for page in paper["page_manifest"] if page["id"].startswith("shadow-table-")]
    assert len(tables) >= 2, "rows that do not fit continue on further M6 pages"
    assert [page["nav_label"] for page in tables] == [f"Shadow processes ({n}/{len(tables)})" for n in range(1, len(tables) + 1)]
    assert all(page["type"] == "M6" and len(page["content"]["rows"]) <= 8 for page in tables)
    assert [row["id"] for page in tables for row in page["content"]["rows"]] == [
        row["id"] for row in paper["analysis"]["shadow_processes"]
    ]
    for page in pages_of(paper, "M6"):
        assert 1 <= len(page["content"]["rows"]) <= 8
        assert len(page["content"]["columns"]) == 3
        # One binding geometry for every M6 table; no per-table width variant exists.
        assert set(page["content"]) == {"lead", "columns", "rows", "footnote", "running"}
    # In the master geometry the eight standard shadow rows already need two pages.
    standard = [page for page in generate(BLANK)["page_manifest"] if page["id"].startswith("shadow-table-")]
    assert [len(page["content"]["rows"]) for page in standard] == [4, 4]
    assert standard[0]["content"]["columns"] == ["Decision / process", "Steered today via", "AI approach"]


def test_six_workflows_split_over_two_diagram_pages_and_five_fit_one() -> None:
    paper = generate(BLANK)
    workflows = paper["analysis"]["target_workflows"]
    assert len(workflows) == 6
    diagrams = pages_of(paper, "M7")
    assert [len(page["content"]["rows"]) for page in diagrams] == [3, 3]
    assert [row["id"] for page in diagrams for row in page["content"]["rows"]] == [w["id"] for w in workflows]
    assert all(row["gate"] and len(row["stages"]) == 3 for page in diagrams for row in page["content"]["rows"])
    assert [page["content"]["title"] for page in diagrams] == [
        "The target picture at a glance (1/2)",
        "The target picture at a glance (2/2)",
    ]

    five = copy.deepcopy(paper)
    five["analysis"]["target_workflows"].pop()
    five = refinalized(five)
    assert [len(page["content"]["rows"]) for page in pages_of(five, "M7")] == [5]


def test_contents_page_numbers_follow_the_paginated_result() -> None:
    for opportunity in (BLANK, POPULATED):
        paper = generate(opportunity)
        by_id = {page["id"]: page for page in paper["page_manifest"]}
        entries = by_id["contents"]["content"]["entries"]
        assert [entry["number"] for entry in entries] == ["01", "02", "03", "04", "05"]
        for entry in entries:
            assert by_id[entry["page_id"]]["number"] == entry["page"]
        assert entries[-1]["page_id"] == "closing-steps"
        assert entries[-1]["page"] == len(paper["page_manifest"]) - 1, "the conclusion starts on the three-step page"
        assert [entry["page"] for entry in entries] == sorted(entry["page"] for entry in entries)

    # Less content, fewer pages: every later chapter moves up by exactly the pages that went away.
    shorter = generate(BLANK)
    removed = shorter["analysis"]["areas"].pop()
    shorter = refinalized(shorter)
    full = generate(BLANK)
    gone = len(full["page_manifest"]) - len(shorter["page_manifest"])
    assert gone >= len([page for page in full["page_manifest"] if page["id"].startswith(f"area-{removed['id']}-")]) >= 1
    before = {e["page_id"]: e["page"] for e in full["page_manifest"][1]["content"]["entries"]}
    after = {e["page_id"]: e["page"] for e in shorter["page_manifest"][1]["content"]["entries"]}
    assert after["ch-overview"] == before["ch-overview"]
    assert after["ch-target"] - after["ch-shadow"] == before["ch-target"] - before["ch-shadow"]
    assert after["closing-steps"] == before["closing-steps"] - gone
    numbers = {page["id"]: page["number"] for page in shorter["page_manifest"]}
    assert all(numbers[page_id] == page for page_id, page in after.items())


# --- honesty: benchmarks, unknowns, no invented facts ---------------------------------------


def test_figures_are_framed_as_benchmarks_and_working_hypotheses() -> None:
    paper = generate(POPULATED)
    analysis = paper["analysis"]
    assert analysis["provenance"]["figures_basis"] == "industry_benchmark_or_working_hypothesis"
    assert analysis["research"]["core_thesis"]["origin"] == "WORKING_HYPOTHESIS"
    assert "not commitments" in analysis["framing"]["leads"]["business_case_footnote"]
    assert "not commitments" in analysis["framing"]["document"]["scope_note"]
    for area in analysis["areas"]:
        assert all(item["basis"] in {"industry_benchmark", "working_hypothesis"} for item in area["business_case"]["impact_ranges"])
        for item in area["opportunities"]:
            if "%" in item["result"]:
                assert "benchmark" in item["result"] or "typically" in item["result"], item["id"]

    unframed = copy.deepcopy(paper)
    unframed["analysis"]["areas"][0]["opportunities"][0]["result"] = "Turnaround drops by 60 % in your sales team."
    with pytest.raises(DiscoveryAnalysisError, match="without benchmark framing or provenance: 60 %"):
        refinalized(unframed)
    promised = copy.deepcopy(paper)
    promised["analysis"]["framing"]["leads"]["business_case_footnote"] = "These savings are guaranteed."
    with pytest.raises(DiscoveryAnalysisError, match="benchmarks and working hypotheses"):
        refinalized(promised)


def with_result(paper: dict[str, Any], text: str) -> dict[str, Any]:
    changed = copy.deepcopy(paper)
    changed["analysis"]["areas"][0]["opportunities"][0]["result"] = text
    return changed


@pytest.mark.parametrize(
    "text",
    [
        "Industry benchmark: 20–40% reduction in manual processing time.",
        "Typically 2–3 FTE of retyping work are freed per team (industry benchmark).",
        "Working hypothesis: €100k–€200k annual value potential, to be validated.",
        "Working hypothesis: EUR 150k per year in avoided rework; to be validated in the workshop.",
        "Typical payback within 3–6 months; USD 50k pilots are an industry benchmark.",
        "Quote drafts in hours instead of days.",
    ],
)
def test_benchmark_and_hypothesis_figures_are_allowed(text: str) -> None:
    assert refinalized(with_result(generate(BLANK), text))["status"] == "ready"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("The customer currently loses €500k per year.", "without benchmark framing or provenance: €500k"),
        ("Your team spends 12 FTE on retyping.", "without benchmark framing or provenance: 12 FTE"),
        ("The company has 950 employees in order processing.", "without benchmark framing or provenance: 950 employees"),
        ("Processing 40,000 invoices per month by hand.", "without benchmark framing or provenance: 40,000 invoices per month"),
        ("Savings of 35 % in the first year.", "without benchmark framing or provenance: 35 %"),
        ("We guarantee 30 % lower cost (industry benchmark).", "presents a figure as a commitment: 30 %"),
        ("The pilot will save €200k, to be validated.", "presents a figure as a commitment: €200k"),
    ],
)
def test_unsupported_customer_figures_and_commitments_are_rejected(text: str, message: str) -> None:
    with pytest.raises(DiscoveryAnalysisError, match=message):
        refinalized(with_result(generate(BLANK), text))


def test_customer_figures_are_allowed_with_provenance() -> None:
    # Supplied by the user: the same sentence that is rejected for a blank client is accepted.
    supplied = {
        **POPULATED,
        "stage1_intake": {**POPULATED["stage1_intake"], "about_company": "We lose about €500k per year to rework; 420 employees."},
    }
    paper = generate(supplied)
    stated = refinalized(with_result(paper, "The customer currently loses €500k per year across 420 employees."))
    assert stated["status"] == "ready"
    with pytest.raises(DiscoveryAnalysisError, match="without benchmark framing or provenance: €900k"):
        refinalized(with_result(paper, "The customer currently loses €900k per year."))

    # Researched with a citation: accepted because the figure is in the cited excerpt.
    class Evidence:
        field, value, source_id, locator, excerpt = "revenue", "EUR 80m", "annual-report", "p. 4", "Revenue: EUR 80m in 2025"

    class Provider:
        def research(self, *, client_name: str, client_web_page: str | None):
            return [Evidence()]

    researched = generate(POPULATED, research_provider=Provider())
    assert refinalized(with_result(researched, "Revenue of EUR 80m gives the scale for the business case."))["status"] == "ready"
    with pytest.raises(DiscoveryAnalysisError, match="without benchmark framing or provenance"):
        refinalized(with_result(generate(POPULATED), "Revenue of EUR 80m gives the scale for the business case."))


def test_blank_client_generates_a_clearly_generic_analysis_without_fake_facts() -> None:
    paper = generate(BLANK)
    assert paper["status"] == "ready"
    assert paper["generation"]["specificity"] == "generic"
    assert paper["generation"]["research_mode"] == "user_context_only"
    research = paper["analysis"]["research"]
    assert research["known_facts"] == []
    assert research["company_profile"] == {"text": None, "origin": "UNKNOWN"}
    assert research["web_research"]["performed"] is False
    assert {"Company name", "Meeting purpose", "Industry and business model"} <= set(research["unknown_facts"])
    assert paper["analysis"]["provenance"]["customer_facts_origin"] == "none"
    assert paper["analysis"]["provenance"]["sources"] == []
    assert paper["analysis"]["framing"]["document"]["title"] == "AI opportunity analysis"
    # Without any context the document says what it is based on - patterns of comparable organisations.
    assert "patterns from comparable organisations" in paper["analysis"]["framing"]["document"]["scope_note"]
    assert not any(area["priority"] for area in paper["analysis"]["areas"])
    rendered = json.dumps(paper, ensure_ascii=False)
    for placeholder in ("Unknown Company", "Example GmbH", "N/A", "Not provided", "{client}", "{purpose}"):
        assert placeholder not in rendered


def test_populated_context_is_reflected_and_stays_marked_as_user_input() -> None:
    paper = generate(POPULATED)
    analysis = paper["analysis"]
    assert paper["generation"]["specificity"] == "company"
    assert analysis["framing"]["document"]["title"] == "AI opportunities for Nordwind Maschinenbau"
    assert "Nordwind Maschinenbau" in analysis["framing"]["chapters"]["overview"]["headline"]
    assert "Reduce quote turnaround and invoice handling effort" in analysis["research"]["core_thesis"]["text"]
    assert analysis["research"]["company_profile"]["origin"] == "USER_INPUT"
    assert "420 employees" in analysis["framing"]["chapters"]["overview"]["paragraphs"][0]
    assert {fact["origin"] for fact in analysis["research"]["known_facts"]} == {"USER_INPUT"}
    assert [area["id"] for area in analysis["areas"] if area["priority"]] == ["sales_quoting", "finance_controlling"]
    assert [area["id"] for area in analysis["areas"]][:2] == ["sales_quoting", "finance_controlling"]
    assert analysis["provenance"]["customer_facts_origin"] == "user_input_only"
    # The company still was not researched, and the paper says so.
    assert analysis["research"]["web_research"]["performed"] is False
    assert "Industry and business model" in analysis["research"]["unknown_facts"]

    context_only = generate({**BLANK, "stage1_intake": {"sales_topic_description": "Procurement automation"}})
    assert context_only["generation"]["specificity"] == "context"
    assert context_only["analysis"]["framing"]["document"]["title"] == "AI opportunity analysis"
    assert [a["id"] for a in context_only["analysis"]["areas"] if a["priority"]] == ["procurement"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda r: r["known_facts"].append({"label": "Employees", "value": "1,200", "origin": "USER_INPUT", "source_refs": []}),
            "not something the user provided",
        ),
        (
            lambda r: r["known_facts"].append({"label": "Revenue", "value": "EUR 80m", "origin": "SOURCE_FACT", "source_refs": []}),
            "claims a source that does not exist",
        ),
        (
            lambda r: r.__setitem__("company_profile", {"text": "A machine builder with 950 employees.", "origin": "USER_INPUT"}),
            "figures the user did not provide: 950",
        ),
        (
            lambda r: r.__setitem__("company_profile", {"text": "Market leader in Europe.", "origin": "SOURCE_FACT"}),
            "requires a research provider",
        ),
        (lambda r: r["web_research"].__setitem__("performed", True), "must match the research mode"),
    ],
)
def test_validator_rejects_fabricated_customer_facts(mutate, message: str) -> None:
    paper = generate(POPULATED)
    mutate(paper["analysis"]["research"])
    with pytest.raises(DiscoveryAnalysisError, match=message):
        refinalized(paper)


def test_research_provider_boundary_adds_only_cited_facts() -> None:
    class Evidence:
        field, value, source_id, locator, excerpt = "headquarters", "Kiel", "registry", "entry 4", "Seat: Kiel"

    class Provider:
        def research(self, *, client_name: str, client_web_page: str | None):
            assert (client_name, client_web_page) == ("Nordwind Maschinenbau", "https://nordwind.example")
            return [Evidence()]

    paper = generate(POPULATED, research_provider=Provider())
    research = paper["analysis"]["research"]
    assert paper["generation"]["research_mode"] == "provider"
    assert research["web_research"]["performed"] is True
    cited = [fact for fact in research["known_facts"] if fact["origin"] == "SOURCE_FACT"]
    assert [(fact["value"], fact["source_refs"][0]["source_id"]) for fact in cited] == [("Kiel", "registry")]
    assert paper["analysis"]["provenance"]["customer_facts_origin"] == "user_input_and_cited_research"
    assert paper["presentation_brief"]["provenance"]["source_refs"][0]["locator"] == "entry 4"
    # Without a provider nothing is researched - and a blank client is never sent to one.
    assert generate(BLANK, research_provider=Provider())["generation"]["research_mode"] == "user_context_only"


def test_parts_five_and_six_are_absent_unless_requested() -> None:
    paper = generate(POPULATED)
    assert paper["generation"]["optional_parts"] == {"roles_employees": False, "decision_map": False, "system_interfaces": False}
    assert paper["analysis"]["optional_deep_dives"] == {"roles_employees": None, "decision_map": None, "system_interfaces": None}
    assert {s["key"]: s["status"] for s in paper["generation"]["stages"]}["optional_parts"] == "skipped"
    assert len(pages_of(paper, "M3")) == 4
    # Fixture mode has no role or system knowledge, so it never writes them even when asked.
    asked = generate(POPULATED, optional_parts={"roles_employees": True})
    assert asked["analysis"]["optional_deep_dives"]["roles_employees"] is None
    assert asked["generation"]["optional_parts"]["roles_employees"] is False
    smuggled = copy.deepcopy(paper)
    smuggled["analysis"]["optional_deep_dives"]["decision_map"] = optional_table("Decision map")
    with pytest.raises(DiscoveryAnalysisError, match="Optional part decision_map does not match"):
        refinalized(smuggled)


# --- determinism, overflow, edits ---------------------------------------------------------


def test_fixture_generation_is_deterministic() -> None:
    assert generate(POPULATED) == generate(POPULATED)
    assert generate(BLANK) == generate(BLANK)
    assert generate(BLANK)["page_manifest"] != generate(POPULATED)["page_manifest"]


def test_library_is_industry_neutral_and_is_not_the_reference_customer() -> None:
    text = json.dumps(load_library(), ensure_ascii=False)
    for word in ("Quotient", "CRDMO", "pharma", "Pharma", "clinical", "GMP suite", "volunteer"):
        assert word not in text


def test_text_beyond_the_master_budget_is_rejected_not_clipped() -> None:
    paper = generate(POPULATED)
    cases = {
        ("framing", "document", "subtitle"): LIMITS["cover_subtitle"],
        ("framing", "chapters", "overview", "key_message"): LIMITS["key_message"],
        ("closing", "headline"): LIMITS["chapter_headline"],
        ("areas", 0, "opportunities", 0, "result"): LIMITS["result"],
        ("shadow_findings", "points", 0, "text"): LIMITS["point_text"],
    }
    for path, limit in cases.items():
        broken = copy.deepcopy(paper)
        node = broken["analysis"]
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = "word " * (limit // 5 + 2)
        with pytest.raises(DiscoveryAnalysisError, match=rf"characters \(limit {limit}\)"):
            refinalized(broken)
    every_page = json.dumps(paper["page_manifest"], ensure_ascii=False)
    assert "…" not in every_page, "nothing in the layout is clipped"


def test_edits_address_sections_and_every_view_follows() -> None:
    paper = generate(POPULATED)
    area = paper["analysis"]["areas"][0]
    target = f"opportunity:{area['id']}/{area['opportunities'][0]['id']}"
    assert target in next(p for p in paper["page_manifest"] if p["id"] == f"area-{area['id']}-1")["edit_targets"]
    assert set(edit_section(paper["analysis"], target)) == {
        "title", "solution", "ai_technology", "how_it_works", "result", "discovery_questions", "opportunity_signal"
    }
    edited = copy.deepcopy(paper)
    edited["analysis"] = apply_edits(paper["analysis"], [{"target": target, "value": {"title": "Faster quoting"}}])
    edited = finalize(edited)
    by_id = {page["id"]: page for page in edited["page_manifest"]}
    assert by_id[f"area-{area['id']}-1"]["content"]["items"][0]["title"] == "Faster quoting"
    assert "Faster quoting" in by_id["overview-map-1"]["content"]["table"]["rows"][0]["text"]
    assert edited["presentation_brief"]["priority_opportunities"][0]["title"] == "Faster quoting"
    assert paper["analysis"]["areas"][0]["opportunities"][0]["title"] != "Faster quoting", "the input is not mutated"

    for bad in (
        [{"target": "opportunity:nope/nope", "value": {"title": "x"}}],
        [{"target": target, "value": {"id": "renamed"}}],
        [{"target": "research", "value": {"known_facts": []}}],
        [{"target": target, "value": {"title": "a"}}, {"target": target, "value": {"title": "b"}}],
        [{"target": "framing", "value": {"document": {"unknown": "x"}}}],
    ):
        with pytest.raises(DiscoveryAnalysisError):
            apply_edits(paper["analysis"], bad)


def test_empty_analysis_is_valid_and_carries_no_content() -> None:
    paper = empty_discovery_analysis(BLANK)
    assert validate_discovery_analysis(paper) is paper
    assert (paper["status"], paper["analysis"], paper["page_manifest"], paper["presentation_brief"]) == (
        "not_generated", None, [], None
    )
    assert paper["language"] == "en"


def test_presentation_brief_is_a_summary_not_a_page_list() -> None:
    paper = generate(POPULATED)
    brief = paper["presentation_brief"]
    assert brief["client_name"] == "Nordwind Maschinenbau"
    assert brief["core_thesis"] == paper["analysis"]["research"]["core_thesis"]["text"]
    assert 1 <= len(brief["priority_opportunities"]) <= 5
    assert [item["area"] for item in brief["priority_opportunities"]][:2] == ["Sales & Quoting", "Finance & Controlling"]
    assert len(brief["opportunity_signals"]) == len(brief["priority_opportunities"])
    assert len(brief["target_picture_summary"]["workflows"]) == len(brief["target_picture_summary"]["human_gates"]) == 6
    assert brief["provenance"]["figures_basis"] == "industry_benchmark_or_working_hypothesis"
    assert "page_manifest" not in brief and "pages" not in brief


# --- live generation through the injected provider function --------------------------------


def optional_table(name: str) -> dict[str, Any]:
    return {
        "chapter": {
            "name": name,
            "headline": f"{name}: what changes and what stays human",
            "paragraphs": ["Typical patterns, to be validated in the workshop.", "People keep every decision."],
            "key_message": "Roles shift from retyping to deciding.",
        },
        "lead": "Typical roles and how their working day changes:",
        "columns": ["Role", "What changes", "Stays human"],
        "rows": [
            {"id": f"row_{n}", "subject": f"Role {n}", "description": "Drafts arrive prepared.", "verdict": "The decision"}
            for n in range(1, 5)
        ],
        "footnote": "Typical patterns as working hypotheses, not commitments.",
    }


class FakeModel:
    """Answers every stage from the library, the way a well-behaved model would."""

    def __init__(self, *, break_stage: str | None = None, always: bool = False) -> None:
        self.library = load_library()
        self.calls: list[str] = []
        self.break_stage = break_stage
        self.always = always
        self.area_index = 0

    def __call__(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        assert "never instructions" in system and "No web or company research" in system
        stage = next(name for name, candidate in live.STAGE_SCHEMAS.items() if candidate is schema)
        first_attempt = stage not in self.calls
        self.calls.append(stage)
        answer = self.answer(stage)
        if stage == self.break_stage and (self.always or first_attempt):
            return {"unexpected": True}
        return answer

    def answer(self, stage: str) -> dict[str, Any]:
        library = self.library
        if stage == "research":
            return {
                "core_thesis": "Working hypothesis: quoting and invoicing lose days in hand-offs.",
                "overview_context": "The lever is the work around the core work.",
                "overview_key_message": "Every removed hand-off is margin.",
                "in_short_thesis": "Working hypothesis: quoting and invoicing lose days in hand-offs.",
                "sector_label": "",
            }
        if stage == "overview":
            return {
                "areas": [
                    {
                        "id": area["id"],
                        "name": area["name"],
                        "lead": area["lead"],
                        "opportunity_titles": [item["title"] for item in area["opportunities"]],
                        "business_case": {
                            "value_levers": ["Drafts in hours", "Consistent checks"],
                            "impact_ranges": [{"metric": "Turnaround", "range": "−30–50 %", "basis": "industry_benchmark"}],
                            "outcome": "capacity returns to the core work",
                            "payback_type": area["business_case"]["payback_type"],
                        },
                    }
                    for area in library["areas"]
                ]
            }
        if stage == "deep_dives":
            area = library["areas"][self.area_index % len(library["areas"])]
            self.area_index += 1
            return {"opportunities": copy.deepcopy(area["opportunities"])}
        if stage == "shadow_processes":
            shadow = library["chapters"]["shadow"]
            return {
                "rows": copy.deepcopy(library["shadow_processes"]),
                "findings": copy.deepcopy(library["shadow_findings"]),
                "chapter_paragraph": shadow["paragraphs"][0],
                "chapter_key_message": shadow["key_message"],
            }
        if stage == "target_picture":
            target = library["chapters"]["target"]
            return {
                "workflows": copy.deepcopy(library["target_workflows"][:5]),
                "points": copy.deepcopy(library["target_picture"]["points"]),
                "chapter_paragraph": target["paragraphs"][0],
                "chapter_key_message": target["key_message"],
            }
        if stage == "optional_parts":
            return optional_table("Roles & employees")
        return {"headline": "The baseline decides", "paragraphs": ["Three steps follow.", "Quick wins carry the programme."]}


def test_live_generation_is_staged_and_code_keeps_facts_and_framing() -> None:
    model = FakeModel()
    saves: list[dict[str, Any]] = []
    paper = generate_discovery_analysis(POPULATED, persist=saves.append, complete=model, **FIXED)
    assert paper["status"] == "ready"
    assert paper["generation"]["mode"] == "live"
    assert model.calls == ["research", "overview", *["deep_dives"] * 7, "shadow_processes", "target_picture", "closing"]
    analysis = paper["analysis"]
    assert analysis["provenance"]["content_origin"] == "live_model"
    assert analysis["research"]["core_thesis"]["text"].startswith("Working hypothesis: quoting")
    # Facts, unknowns and the research statement come from code, not from the model.
    assert analysis["research"]["web_research"]["performed"] is False
    assert {fact["origin"] for fact in analysis["research"]["known_facts"]} == {"USER_INPUT"}
    assert "not commitments" in analysis["framing"]["leads"]["business_case_footnote"]
    assert len(analysis["target_workflows"]) == 5 and len(pages_of(paper, "M7")) == 1
    assert "5 workflow chains" in analysis["framing"]["leads"]["target_table"]
    assert "24 opportunities" in analysis["framing"]["document"]["subtitle"]
    # Progress is persisted stage by stage.
    running = [next((s["key"] for s in save["generation"]["stages"] if s["status"] == "generating"), None) for save in saves]
    assert [key for key in running if key] == ["research", "overview", "deep_dives", "shadow_processes", "target_picture", "closing", "validation", "layout"]
    assert all(save["analysis"] is None for save in saves[:-3])


def test_live_generation_writes_requested_optional_parts_as_extra_chapters() -> None:
    model = FakeModel()
    paper = generate(POPULATED, complete=model, optional_parts={"roles_employees": True})
    assert paper["generation"]["optional_parts"]["roles_employees"] is True
    assert paper["analysis"]["optional_deep_dives"]["roles_employees"]["rows"]
    assert paper["analysis"]["optional_deep_dives"]["decision_map"] is None
    openers = pages_of(paper, "M3")
    assert [page["id"] for page in openers] == ["ch-overview", "ch-deep-dive", "ch-shadow", "ch-target", "ch-roles-employees"]
    table = next(page for page in paper["page_manifest"] if page["id"] == "roles-employees-table-1")
    assert (table["type"], table["edit_targets"]) == ("M6", ["optional:roles_employees"])
    assert paper["page_manifest"][-1]["chapter"]["number"] == 6
    assert [e["number"] for e in paper["page_manifest"][1]["content"]["entries"]] == ["01", "02", "03", "04", "05", "06"]
    assert {page["type"] for page in paper["page_manifest"]} <= {"M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9"}


def test_live_stage_retries_once_then_fails_without_partial_content() -> None:
    retried = FakeModel(break_stage="shadow_processes")
    assert generate(POPULATED, complete=retried)["status"] == "ready"
    assert retried.calls.count("shadow_processes") == 2

    saves: list[dict[str, Any]] = []
    broken = FakeModel(break_stage="shadow_processes", always=True)
    with pytest.raises(DiscoveryAnalysisStageError) as failure:
        generate_discovery_analysis(POPULATED, persist=saves.append, complete=broken, **FIXED)
    assert failure.value.stage_key == "shadow_processes"
    last = saves[-1]
    assert last["status"] == "failed"
    assert (last["analysis"], last["page_manifest"], last["presentation_brief"]) == (None, [], None)
    stages = {stage["key"]: stage["status"] for stage in last["generation"]["stages"]}
    assert (stages["deep_dives"], stages["shadow_processes"], stages["target_picture"]) == ("ready", "failed", "waiting")


def test_bundled_web_sample_is_the_current_fixture_output() -> None:
    """Preview mode shows this file; it must never drift from what the server generates."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("sample_script", root / "scripts" / "generate_discovery_analysis_sample.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stored = json.loads(module.TARGET.read_text(encoding="utf-8"))
    assert stored == module.build_sample(), "run scripts/generate_discovery_analysis_sample.py"
    assert stored["intake_context"]["client_name"] == ""
    assert stored["generation"]["specificity"] == "generic"
