"""Master Presentation V2: frozen source snapshot, generation manifest and plan.

V2 is the post-meeting version of the SAME presentation as V1: the unchanged canonical deck
followed by a new appendix. Everything a V2 generation uses is frozen in one snapshot when the
generation is requested:

    approved Discovery v2   exact version id and the content the appendix may draw on
    approved Discovery 2    BAP-7: the authoritative post-meeting Discovery version - its meeting
                            items, its carried-forward opportunities and its open questions
    Master Presentation     canonical master identity and the V1 version V2 follows
    transcript              id and content revision (the appendix uses the confirmed findings)
    meeting findings        text and source of every meeting item of the approved Discovery 2;
                            an item the owner wrote or reworded has the source "owner"
    personal notes          revision and text, kept as the owner's own source
    Borek use cases         the selected approved reference facts with their corpus identities
    meeting review          who confirmed when, the review fingerprint and its source hash

The plan is built from the snapshot alone, so the worker renders exactly what was frozen; the
manifest records the identities and the snapshot checksum and is stored with the version.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from services.language import in_other_language, stored_language
from services.meeting.discovery2 import content_hash as discovery2_content_hash
from services.meeting.discovery2 import meeting_items as discovery2_meeting_items
from services.presentation.borek_deck.deck_plan import PLAN_ENGINE
from services.presentation.master_deck.appendix import AppendixPlanError, DISCOVERY_SCHEMA_VERSION, appendix_source_hash
from services.presentation.master_deck.appendix_v2 import (
    CATEGORIES,
    PLANNER_VERSION,
    confirmed_findings,
    plan_v2_appendix,
    shown_finding_references,
)
from services.presentation.master_deck.plan import MANIFEST_KIND_V2, MASTER_V2, arrange_slides
from services.presentation.master_deck.registry import (
    DECK_LAYOUT,
    DEFAULT_MASTER_ID,
    MasterDeck,
    first_appendix_position,
    get_master,
    verify_master,
)

SNAPSHOT_KIND = "master_presentation_v2_snapshot"
PRODUCT_VERSION = "V2"
PRODUCT_STAGE = "post_meeting"
# "owner": written or reworded by the owner while reviewing Discovery 2 - neither a transcript
# statement nor a note, and never presented as one.
FINDING_SOURCES = frozenset({"transcript", "personal_notes", "both", "owner"})
# The parts of an approved Discovery 2 item the appendix may show.
DISCOVERY2_ITEM_FIELDS = ("id", "text", "origin", "status", "source", "discovery1_ref", "edited")


class SnapshotError(ValueError):
    """The frozen sources are incomplete, inconsistent or not the ones the job was created with."""

    code = "MASTER_V2_SNAPSHOT_INVALID"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    """Checksum of the complete frozen source material."""
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def generation_fingerprint(snapshot: dict[str, Any]) -> str:
    """Identity of what a V2 is generated FROM, independent of when it was confirmed.

    Covers the canonical master, the presentation and its V1 base, the planner, the approved
    Discovery content, the transcript content, the notes text, every confirmed finding with its
    source, the exclusions and the selected use cases with their corpus versions. It leaves out
    the confirmation time, the reviewer, the time of the analysis and the notes timestamp: a
    renewed confirmation of unchanged inputs is the same generation input, and a ready V2
    built from it is still the current one.
    """
    notes = snapshot["personal_notes"]["text"]
    inputs = {
        "planner_version": snapshot["planner_version"],
        "master": snapshot["master"],
        "presentation": snapshot["presentation"],
        "approved_discovery": {
            "version_id": snapshot["approved_discovery"]["version_id"],
            "appendix_source_hash": snapshot["approved_discovery"]["appendix_source_hash"],
        },
        "transcript": {
            "transcript_id": snapshot["transcript"]["transcript_id"],
            "revision": snapshot["transcript"]["revision"],
        },
        "personal_notes_sha256": None if notes in (None, "") else hashlib.sha256(str(notes).encode("utf-8")).hexdigest(),
        "findings": snapshot["findings"],
        "excluded_findings": sorted(snapshot["excluded_findings"]),
        # Absent only in snapshots frozen before Discovery 2 existed.
        "approved_discovery2": None
        if snapshot.get("approved_discovery2") is None
        # Content, not version: regenerating Discovery 2 into the same document is no new input.
        else {"content_hash": snapshot["approved_discovery2"]["content_hash"]},
        "use_cases": [[item["fact_id"], item.get("document_version"), item.get("corpus_version"), item.get("statement")] for item in snapshot["use_cases"]],
    }
    return hashlib.sha256(canonical_json(inputs).encode("utf-8")).hexdigest()


def build_snapshot(
    *,
    opportunity_id: str,
    presentation_id: str,
    base_presentation_version_id: str,
    approved_discovery: dict[str, Any],
    transcript: dict[str, Any],
    extraction: dict[str, Any],
    personal_notes: dict[str, Any],
    reviewed_items: dict[str, list[dict[str, Any]]],
    use_cases: list[dict[str, Any]],
    meeting_review: dict[str, Any],
    approved_discovery2: dict[str, Any],
    master: MasterDeck | None = None,
) -> dict[str, Any]:
    """Freeze the sources of one V2 generation. Pure: it reads nothing but its arguments.

    The meeting findings are the meeting items of the approved Discovery 2 (BAP-7), the
    authoritative post-meeting Discovery: what the owner approved there - including an edited
    wording or a removed item - is what V2 shows. A finding whose wording the owner changed or
    added is the owner's statement (source "owner"): it is presented as reviewed by the owner,
    never as a client statement and never as a personal note. The approved Discovery 2 also
    supplies the carried-forward opportunities the appendix may recommend and the open questions
    it lists, in their approved wording. Findings excluded in the meeting review stay excluded.
    """
    master = master or get_master(DEFAULT_MASTER_ID)
    paper = approved_discovery.get("paper_json")
    if str(approved_discovery.get("status")) != "approved":
        raise SnapshotError("Master Presentation V2 needs an approved Discovery version")
    if not isinstance(paper, dict) or paper.get("schema_version") != DISCOVERY_SCHEMA_VERSION:
        raise SnapshotError("Master Presentation V2 needs an approved Discovery analysis (schema 2.0)")
    if str(approved_discovery.get("opportunity_id")) != str(opportunity_id):
        raise SnapshotError("The approved Discovery version belongs to another opportunity")
    discovery2 = approved_discovery2.get("document_json")
    if str(approved_discovery2.get("status")) != "approved" or not isinstance(discovery2, dict):
        raise SnapshotError("Master Presentation V2 needs an approved Discovery 2")
    if str(approved_discovery2.get("opportunity_id")) != str(opportunity_id) or str(discovery2.get("opportunity_id")) != str(opportunity_id):
        raise SnapshotError("The approved Discovery 2 belongs to another opportunity")
    if discovery2_content_hash(discovery2) != str(approved_discovery2.get("content_hash")):
        raise SnapshotError("The approved Discovery 2 does not match its recorded content")
    if str((discovery2.get("sources") or {}).get("discovery1", {}).get("version_id")) != str(approved_discovery["id"]):
        raise SnapshotError("The approved Discovery 2 was generated from another Discovery version")
    findings: dict[str, list[dict[str, str]]] = {}
    for category, items in discovery2_meeting_items(discovery2).items():
        findings[category] = []
        for item in items:
            source = "owner" if item["origin"] == "owner" or item.get("edited") else str(item["source"])
            if source not in FINDING_SOURCES:
                # A finding whose origin cannot be shown is never presented as a fact.
                raise SnapshotError("A Discovery 2 meeting item has no verified source; generate Discovery 2 again")
            findings[category].append({"text": str(item["text"]), "source": source})
    shown = {item["text"] for items in findings.values() for item in items}
    excluded: list[str] = []
    for category in CATEGORIES:
        for item in reviewed_items.get(category, []):
            if item.get("status") != "confirmed" and str(item["text"]) not in shown:
                excluded.append(str(item["text"]))
    unresolved = [item["fact_id"] for item in use_cases if item.get("status") != "resolved"]
    if unresolved:
        raise SnapshotError(f"Selected use cases are no longer available: {', '.join(unresolved)}")
    brief = paper["presentation_brief"]
    return {
        "schema_version": "1.0",
        "kind": SNAPSHOT_KIND,
        "planner_version": PLANNER_VERSION,
        "opportunity_id": str(opportunity_id),
        "client_name": str(brief.get("client_name") or "").strip(),
        # BAP-4: the slide order (closing slide last). Part of the generation fingerprint, so a V2
        # built in the earlier order is generated again instead of being reused.
        "master": {**master.identity(), "slide_count": master.slide_count, "deck_layout": DECK_LAYOUT},
        "presentation": {"presentation_id": str(presentation_id), "base_presentation_version_id": str(base_presentation_version_id)},
        # BAP-13: the language of the appendix - the approved Discovery's (Discovery 2 matches it).
        "language": stored_language(paper.get("language")),
        "approved_discovery": {
            "version_id": str(approved_discovery["id"]),
            "document_id": str(approved_discovery.get("document_id") or paper.get("document_id") or ""),
            "version_number": approved_discovery.get("version_number"),
            "schema_version": DISCOVERY_SCHEMA_VERSION,
            "appendix_source_hash": appendix_source_hash(paper),
            "presentation_brief": copy.deepcopy(brief),
            "analysis": copy.deepcopy(paper["analysis"]),
        },
        "transcript": {
            "transcript_id": str(transcript["transcript_id"]),
            "revision": str(transcript["revision"]),
            "file_name": str(transcript.get("file_name") or ""),
        },
        "meeting_extraction": {
            "generated_at": str(extraction["generated_at"]),
            "execution_mode": extraction.get("execution_mode"),
            "personal_notes_updated_at": extraction.get("personal_notes_updated_at"),
        },
        "personal_notes": {"updated_at": personal_notes.get("updated_at"), "text": personal_notes.get("text")},
        "findings": findings,
        "excluded_findings": excluded,
        "use_cases": [
            {
                key: item.get(key)
                for key in ("fact_id", "statement", "service_key", "document_id", "document_version", "document_type", "corpus_id", "corpus_version")
            }
            for item in use_cases
        ],
        "meeting_review": {
            "confirmed_at": str(meeting_review["confirmed_at"]),
            "confirmed_by": str(meeting_review["confirmed_by"]),
            "review_fingerprint": str(meeting_review["review_fingerprint"]),
            "source_hash": str(meeting_review["source_hash"]),
        },
        "approved_discovery2": {
            "version_id": str(approved_discovery2["id"]),
            "document_id": str(approved_discovery2.get("document_id") or discovery2.get("document_id") or ""),
            "version_number": approved_discovery2.get("version_number"),
            "content_hash": str(approved_discovery2["content_hash"]),
            "basis_hash": str(approved_discovery2.get("basis_hash") or ""),
            "approved_at": None if approved_discovery2.get("approved_at") is None else str(approved_discovery2["approved_at"]),
            "generation_mode": (discovery2.get("generation") or {}).get("mode"),
            "content": discovery2_content(discovery2),
        },
    }


def discovery2_content(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """What the appendix takes from Discovery 2 besides the findings, in its approved wording.

    opportunities   the Discovery 1 opportunities Discovery 2 carries forward (as approved,
                    possibly edited); one removed in Discovery 2 is no longer offered
    open_questions  every question Discovery 2 lists as still open
    """
    sections = {section["key"]: section["items"] for section in document.get("sections") or []}
    pick = lambda item: {key: copy.deepcopy(item.get(key)) for key in DISCOVERY2_ITEM_FIELDS}  # noqa: E731
    return {
        "opportunities": [pick(item) for item in sections.get("opportunities", []) if item.get("origin") == "discovery1"],
        "open_questions": [pick(item) for item in sections.get("open_questions", [])],
    }


def generation_manifest(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Identity of everything one V2 generation is built from; stored with the version."""
    return {
        "schema_version": "1.0",
        "kind": MANIFEST_KIND_V2,
        "product_version": PRODUCT_VERSION,
        "product_stage": PRODUCT_STAGE,
        "planner_version": snapshot["planner_version"],
        "master_id": snapshot["master"]["master_id"],
        "master_version": snapshot["master"]["master_version"],
        "master_sha256": snapshot["master"]["master_sha256"],
        "master_slide_count": snapshot["master"]["slide_count"],
        # Absent only in snapshots frozen before the closing slide was placed last.
        **({"deck_layout": snapshot["master"]["deck_layout"]} if snapshot["master"].get("deck_layout") else {}),
        "presentation_id": snapshot["presentation"]["presentation_id"],
        "base_presentation_version_id": snapshot["presentation"]["base_presentation_version_id"],
        "approved_discovery_version_id": snapshot["approved_discovery"]["version_id"],
        "approved_discovery_document_id": snapshot["approved_discovery"]["document_id"],
        "discovery_schema_version": snapshot["approved_discovery"]["schema_version"],
        "appendix_source_hash": snapshot["approved_discovery"]["appendix_source_hash"],
        "transcript_id": snapshot["transcript"]["transcript_id"],
        "transcript_revision": snapshot["transcript"]["revision"],
        "meeting_extraction_generated_at": snapshot["meeting_extraction"]["generated_at"],
        "meeting_extraction_execution_mode": snapshot["meeting_extraction"]["execution_mode"],
        "personal_notes_updated_at": snapshot["personal_notes"]["updated_at"],
        "selected_use_case_ids": [item["fact_id"] for item in snapshot["use_cases"]],
        "meeting_review_confirmed_at": snapshot["meeting_review"]["confirmed_at"],
        "review_fingerprint": snapshot["meeting_review"]["review_fingerprint"],
        "source_hash": snapshot["meeting_review"]["source_hash"],
        "approved_discovery2_version_id": (snapshot.get("approved_discovery2") or {}).get("version_id"),
        "discovery2_content_hash": (snapshot.get("approved_discovery2") or {}).get("content_hash"),
        "generation_fingerprint": generation_fingerprint(snapshot),
        "snapshot_hash": snapshot_hash(snapshot),
        "confirmed_finding_count": len(confirmed_findings(snapshot)),
        "excluded_finding_count": len(snapshot["excluded_findings"]),
        # BAP-13: the appendix language; the canonical master slides stay in the master's language.
        "language": stored_language(snapshot.get("language")),
        "master_language": get_master(snapshot["master"]["master_id"]).language,
        # PR #13 review: confirmed findings shown in their own words although they read as the other
        # language - visible to the owner before review and finalization, never translated silently.
        "other_language_findings": sum(1 for item in confirmed_findings(snapshot) if in_other_language(item["text"], snapshot.get("language"))),
    }


_SAME_SOURCE_KEYS = ("kind", "master_id", "master_version", "master_sha256", "presentation_id", "generation_fingerprint", "planner_version")


def same_source(left: Any, right: Any) -> bool:
    """Two V2 manifests describe the same master and the same generation inputs.

    ``generation_fingerprint`` covers the content the deck is built from. Confirmation and
    analysis timestamps are recorded in each manifest for traceability but are not compared, so
    confirming unchanged findings again finds the earlier result instead of forcing a new one.
    """
    return (
        isinstance(left, dict)
        and isinstance(right, dict)
        and all(left.get(key) == right.get(key) and left.get(key) for key in _SAME_SOURCE_KEYS)
    )


def build_plan(snapshot: dict[str, Any], master: MasterDeck | None = None) -> dict[str, Any]:
    """The stored plan: the canonical slides, the finished V2 appendix, the closing slide last.

    A snapshot frozen before BAP-4 (no ``deck_layout``) keeps the order it was frozen with.
    """
    master = master or get_master(snapshot["master"]["master_id"])
    if master.identity() != {key: snapshot["master"][key] for key in ("master_id", "master_version", "master_sha256")}:
        raise SnapshotError("The snapshot was frozen for a different canonical master deck")
    canonical = verify_master(master)
    closing_last = snapshot["master"].get("deck_layout") == DECK_LAYOUT
    first_slide = first_appendix_position(master) if closing_last else master.slide_count + 1
    appendix = plan_v2_appendix(snapshot, first_order=first_slide)
    client = snapshot["client_name"]
    slides = arrange_slides(master, canonical["slides"], appendix, closing_last=closing_last)
    shown = shown_finding_references(appendix, snapshot)
    omitted = [
        f"finding:{category}:{position}"
        for category in CATEGORIES
        for position, _item in enumerate(snapshot["findings"][category])
        if f"finding:{category}:{position}" not in shown
    ]
    return {
        "schema_version": "1.0",
        "engine": PLAN_ENGINE,
        "deck_kind": MASTER_V2,
        "product_version": PRODUCT_VERSION,
        "prompt_version": snapshot["planner_version"],
        "lang": master.language.upper(),
        "content_language": stored_language(snapshot.get("language")),
        "title": f"Master Presentation — {client}" if client else "Master Presentation",
        "master": {**master.identity(), "slide_count": master.slide_count},
        **({"deck_layout": DECK_LAYOUT} if closing_last else {}),
        "appendix": {
            "first_slide": first_slide,
            "slide_count": len(appendix),
            "source_hash": snapshot_hash(snapshot),
            "omitted_findings": omitted,
        },
        "slides": slides,
    }


def verify_frozen_job(*, manifest: Any, snapshot: Any, plan_json: Any) -> None:
    """The job still carries exactly the sources it was created with, and the plan belongs to them."""
    if not isinstance(manifest, dict) or manifest.get("kind") != MANIFEST_KIND_V2:
        raise SnapshotError("The job has no Master Presentation V2 manifest")
    if not isinstance(snapshot, dict) or snapshot.get("kind") != SNAPSHOT_KIND:
        raise SnapshotError("The frozen source snapshot was not stored on this job")
    frozen = snapshot_hash(snapshot)
    if manifest.get("snapshot_hash") != frozen:
        raise SnapshotError("The frozen source snapshot does not match the generation manifest")
    if generation_manifest(snapshot) != manifest:
        raise SnapshotError("The generation manifest does not describe the frozen source snapshot")
    if not isinstance(plan_json, dict) or plan_json.get("deck_kind") != MASTER_V2:
        raise SnapshotError("The presentation no longer points at a Master Presentation V2 plan")
    if (plan_json.get("appendix") or {}).get("source_hash") != frozen:
        raise SnapshotError("The stored plan was built from other sources than the frozen snapshot")


__all__ = [
    "AppendixPlanError",
    "PRODUCT_STAGE",
    "PRODUCT_VERSION",
    "SnapshotError",
    "build_plan",
    "build_snapshot",
    "generation_fingerprint",
    "generation_manifest",
    "same_source",
    "snapshot_hash",
    "verify_frozen_job",
]
