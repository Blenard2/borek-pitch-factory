"""Staged generation of the AI Opportunity Analysis, persisted after every stage."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import uuid4

from services.framework.discovery_analysis import live
from services.framework.discovery_analysis.brief import build_presentation_brief
from services.framework.discovery_analysis.fixture import (
    build_fixture_analysis,
    load_library,
    prioritize_by_website,
    researched_profile_paragraph,
    researched_scope,
    website_fact_label,
    website_profile_text,
)
from services.framework.discovery_analysis.layout import build_page_manifest
from services.framework.discovery_analysis.model import (
    SCHEMA_VERSION,
    DiscoveryAnalysisError,
    content_problems,
    default_optional_parts,
    initial_stages,
    intake_context,
    specificity,
    validate_discovery_analysis,
)
from services.language import DEFAULT_LANGUAGE, normalize_language

PersistFn = Callable[[dict[str, Any]], None]


class DiscoveryAnalysisStageError(RuntimeError):
    """A stage failed; the paper was persisted with that stage and the status marked failed."""

    def __init__(self, stage_key: str, message: str) -> None:
        self.stage_key = stage_key
        super().__init__(message)


def finalize(paper: dict[str, Any]) -> dict[str, Any]:
    """Derive the page manifest and the presentation brief from the content, then validate."""
    paper["presentation_brief"] = build_presentation_brief(paper)
    paper["page_manifest"] = build_page_manifest(paper)
    return validate_discovery_analysis(paper)


def generate_discovery_analysis(
    opportunity: dict[str, Any],
    *,
    persist: PersistFn,
    complete: live.CompleteFn | None = None,
    research_provider: Any = None,
    optional_parts: dict[str, bool] | None = None,
    latest_approved_version_id: str | None = None,
    document_id: str | None = None,
    generated_at: str | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> dict[str, Any]:
    """Fixture mode when ``complete`` is None, otherwise one model call per stage.

    Sparse or empty input never blocks generation: the analysis becomes less specific and
    says so, and unknown company facts stay unknown.

    BAP-13: ``language`` (``en`` or ``de``) is the language of every generated and fixed text;
    the user's own input and quoted website facts stay in the language they were written in.
    """
    language = normalize_language(language)
    context = intake_context(opportunity)
    requested = {**default_optional_parts(), **(optional_parts or {})}
    if complete is None:
        # The library holds no role or system knowledge, so fixture mode cannot write parts 5-6.
        requested = default_optional_parts()
    paper: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "opportunity_id": str(opportunity["id"]),
        "document_id": document_id or str(uuid4()),
        "latest_approved_version_id": latest_approved_version_id,
        "status": "generating",
        "generated_at": generated_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "language": language,
        "intake_context": context,
        "generation": {
            "mode": "fixture" if complete is None else "live",
            "specificity": specificity(context),
            "research_mode": "user_context_only",
            "optional_parts": requested,
            "stages": initial_stages(requested),
        },
        "analysis": None,
        "presentation_brief": None,
        "page_manifest": [],
    }
    persist(copy.deepcopy(paper))
    # Both modes start from the same skeleton: user-provided facts, unknowns, benchmark framing.
    draft = build_fixture_analysis(context, requested, language)

    # The model call of every live stage; with website research it also carries the findings.
    calls: dict[str, live.CompleteFn | None] = {"complete": complete}

    def research() -> None:
        website = _apply_research(paper, draft, context, research_provider, language)
        if complete is not None:
            material = None
            if website is not None:
                material = live.website_material(website)
                calls["complete"] = live.with_website(complete, material)
                _cite_material(draft, material)
            live.research_stage(calls["complete"], context, draft, website=material, language=language)

    def validation() -> None:
        # BAP-9 Phase 2: what live answers still missed after their retry is kept for review,
        # with the generation record - never in the printed analysis.
        warnings = draft.pop(live.QUALITY_WARNINGS_KEY, [])
        if warnings:
            paper["generation"]["quality_warnings"] = warnings[: live.MAX_QUALITY_WARNINGS]
        if complete is not None:
            _settle_live(draft, language)
        problems = content_problems({**paper, "analysis": draft, "status": "ready"})
        if problems:
            raise DiscoveryAnalysisError("; ".join(problems))
        paper["analysis"] = draft

    def layout() -> None:
        paper["presentation_brief"] = build_presentation_brief(paper)
        paper["page_manifest"] = build_page_manifest(paper)

    live_steps: dict[str, Callable[[], None]] = (
        {}
        if complete is None
        else {
            "overview": lambda: live.overview_stage(calls["complete"], context, draft, language=language),
            "deep_dives": lambda: live.deep_dives_stage(calls["complete"], context, draft, language=language),
            "shadow_processes": lambda: live.shadow_stage(calls["complete"], context, draft, language=language),
            "target_picture": lambda: live.target_stage(calls["complete"], context, draft, language=language),
            "optional_parts": lambda: live.optional_stage(calls["complete"], context, draft, requested, language=language),
            "closing": lambda: live.closing_stage(calls["complete"], context, draft, language=language),
        }
    )
    steps = {"research": research, "validation": validation, "layout": layout, **live_steps}
    for stage in paper["generation"]["stages"]:
        if stage["status"] == "skipped":
            continue
        stage["status"] = "generating"
        persist(copy.deepcopy(paper))
        try:
            steps.get(stage["key"], lambda: None)()
        except Exception as exc:
            stage["status"] = "failed"
            paper["status"] = "failed"
            paper["analysis"] = None
            paper["presentation_brief"] = None
            paper["page_manifest"] = []
            persist(copy.deepcopy(paper))
            raise DiscoveryAnalysisStageError(stage["key"], f"{stage['label']} failed: {exc}") from exc
        stage["status"] = "ready"
        if stage["key"] == "layout":
            paper["status"] = "ready"
            validate_discovery_analysis(paper)
        persist(copy.deepcopy(paper))
    return paper


# Website facts listed as known facts; the rest stay available in generation.website_research.
MAX_WEBSITE_FACTS = 14


def _apply_research(
    paper: dict[str, Any], draft: dict[str, Any], context: dict[str, Any], provider: Any, language: str = DEFAULT_LANGUAGE
) -> dict[str, Any] | None:
    """Cited evidence from an approved research provider, if one exists. Otherwise nothing:
    researched fields stay unknown and the analysis says that no research was performed.

    A website research provider (BAP-5) returns the website research for the live stages.
    """
    if provider is None:
        return None
    research_site = getattr(provider, "research_website", None)
    if callable(research_site):
        return _apply_website_research(paper, draft, context, research_site, language)
    if not context["client_name"]:
        return None
    evidence = provider.research(client_name=context["client_name"], client_web_page=context["website_url"])
    if not evidence:
        return None
    research = draft["research"]
    sources = []
    for item in evidence:
        ref = {"source_id": item.source_id, "locator": item.locator, "excerpt": item.excerpt}
        sources.append(ref)
        research["known_facts"].append(
            {"label": item.field.replace("_", " ").capitalize(), "value": item.value, "origin": "SOURCE_FACT", "source_refs": [ref]}
        )
    research["web_research"] = {"performed": True, "note": f"Company research returned {len(evidence)} cited facts."}
    draft["provenance"]["sources"] = sources
    draft["provenance"]["customer_facts_origin"] = "user_input_and_cited_research"
    paper["generation"]["research_mode"] = "provider"
    return None


def _apply_website_research(
    paper: dict[str, Any], draft: dict[str, Any], context: dict[str, Any], research_site: Any, language: str = DEFAULT_LANGUAGE
) -> dict[str, Any] | None:
    """BAP-5: facts quoted from the client's website, with their sources, kept apart from hints.

    The research state is recorded in every case (``generation.website_research``); a website
    that could not be analysed is reported as such, and the analysis is not presented as
    researched. Signals stay interpretations: they only order the areas, never become facts.
    """
    if not context["website_url"]:
        return None
    site = research_site(context["website_url"], client_name=context["client_name"] or "")
    data = site.to_dict()
    paper["generation"]["website_research"] = {key: value for key, value in data.items() if key != "content"}
    research = draft["research"]
    research["web_research"] = {"performed": False, "note": site.summary()}
    if not site.usable or not site.facts:
        return None
    refs = []
    for fact in site.facts[:MAX_WEBSITE_FACTS]:
        ref = {"source_id": fact["source_url"], "locator": fact["locator"], "excerpt": fact["excerpt"]}
        refs.append(ref)
        research["known_facts"].append({"label": website_fact_label(fact["label"], language), "value": fact["value"], "origin": "SOURCE_FACT", "source_refs": [ref]})
    research["web_research"] = {"performed": True, "note": site.summary()}
    if any(fact["kind"] in ("description", "about", "offering", "industry") for fact in site.facts):
        unknown_business = load_library(language)["research"]["always_unknown"][0]
        research["unknown_facts"] = [item for item in research["unknown_facts"] if item != unknown_business]
    # BAP-10: once research succeeded, no "no research was performed" wording is left. The
    # printed profile comes from the website - its description, else what it lists; the scope
    # note names the website as a source. A profile the user described stays the recorded
    # (internal) profile, but the printed paragraph is the website's: the user's text is never
    # printed (audit 2026-10-27, BUG-02).
    listed = site.facts[:MAX_WEBSITE_FACTS]
    profile = website_profile_text(listed, language)
    if research["company_profile"]["origin"] == "UNKNOWN":
        research["company_profile"] = {"text": profile, "origin": "SOURCE_FACT"}
    paragraph = researched_profile_paragraph(profile, context["client_name"] or "", language)
    if paragraph:
        draft["framing"]["chapters"]["overview"]["paragraphs"][0] = paragraph
    document = draft["framing"]["document"]
    if document["scope_note"] == load_library(language)["in_short"]["scope_context"]:
        document["scope_note"] = researched_scope(language)
    draft["provenance"]["sources"] = refs
    draft["provenance"]["customer_facts_origin"] = "user_input_and_cited_research"
    paper["generation"]["research_mode"] = "provider"
    prioritize_by_website(draft["areas"], data["signals"])
    return data


def _cite_material(draft: dict[str, Any], material: dict[str, Any]) -> None:
    """Everything the model is shown from the website is listed as a source of the analysis."""
    sources = draft["provenance"]["sources"]
    known = {(ref["source_id"], ref["excerpt"]) for ref in sources}
    for item in material["excerpts"]:
        ref = {"source_id": item["source_url"], "locator": f"{item['role']} page text", "excerpt": item["text"]}
        if (ref["source_id"], ref["excerpt"]) not in known:
            known.add((ref["source_id"], ref["excerpt"]))
            sources.append(ref)


def _settle_live(draft: dict[str, Any], language: str = DEFAULT_LANGUAGE) -> None:
    """Make model output structurally safe: unique ids and count-based texts that match."""
    seen: set[str] = set()
    for group in (draft["areas"], [item for area in draft["areas"] for item in area["opportunities"]], draft["shadow_processes"], draft["target_workflows"]):
        for item in group:
            base, candidate, suffix = item["id"], item["id"], 2
            while candidate in seen:
                candidate = f"{base[:60]}_{suffix}"
                suffix += 1
            item["id"] = candidate
            seen.add(candidate)
    for area in draft["areas"]:
        area.pop("_titles", None)
    library = load_library(language)
    counts = {
        "area_count": len(draft["areas"]),
        "opportunity_count": sum(len(area["opportunities"]) for area in draft["areas"]),
    }
    document = draft["framing"]["document"]
    document["subtitle"] = library["cover"]["subtitle"].format(**counts)
    document["in_short"][0] = library["in_short"]["first_statement"].format(**counts)
    draft["framing"]["leads"]["map"] = library["chapters"]["overview"]["map_lead"].format(count=counts["opportunity_count"])
    draft["provenance"]["content_origin"] = "live_model"
    researched = draft["provenance"]["customer_facts_origin"] == "user_input_and_cited_research"
    draft["provenance"]["notes"] = [
        "Opportunities, shadow processes and workflow chains were written by the model from the user's context"
        + (" and the cited website research" if researched else "")
        + "; they are hypotheses, not findings about this company.",
        library["research"]["notes"][1],
    ]
