"""Plan and frozen source manifest of a Master Presentation V1.

Master Presentation V1 = the canonical master deck (immutable) + an appendix planned from one
approved Discovery analysis. The plan carries every slide in finished form, so what was planned
is what is rendered; the manifest records exactly which master and which approved version the
deck was built from. Both are stored with the generation job and never re-derived on a retry.
"""

from __future__ import annotations

import copy
from typing import Any

from services.language import stored_language
from services.presentation.borek_deck.deck_plan import PLAN_ENGINE
from services.presentation.master_deck.appendix import (
    DISCOVERY_SCHEMA_VERSION,
    appendix_source_hash,
    approved_analysis,
    plan_appendix,
)
from services.presentation.master_deck.registry import (
    DECK_LAYOUT,
    DEFAULT_MASTER_ID,
    MasterDeck,
    deck_order,
    first_appendix_position,
    get_master,
    verify_master,
)

MASTER_V1 = "master_v1"
MANIFEST_KIND = "master_presentation_v1"
# Product stage of this deck. The post-meeting presentation (V2) is a later stage of the same
# presentation; a new pre-meeting revision stays V1 whatever its version number is.
PRODUCT_VERSION = "V1"
PRODUCT_STAGE = "pre_meeting"
CANONICAL_LAYOUT = "CANONICAL"
PLAN_VERSION = "master-presentation:v1"


# Master Presentation V2 (post-meeting) is a later version of the same presentation: the same
# canonical deck with a new appendix. Its plan and manifest are built in ``master_deck/v2.py``.
MASTER_V2 = "master_v2"
MASTER_DECK_KINDS = frozenset({MASTER_V1, MASTER_V2})
MANIFEST_KIND_V2 = "master_presentation_v2"
MASTER_MANIFEST_KINDS = frozenset({MANIFEST_KIND, MANIFEST_KIND_V2})


def is_master_plan(plan_json: Any) -> bool:
    """A Master Presentation of either product version: canonical deck plus an appendix."""
    return isinstance(plan_json, dict) and plan_json.get("deck_kind") in MASTER_DECK_KINDS


def is_master_manifest(manifest: Any) -> bool:
    return isinstance(manifest, dict) and manifest.get("kind") in MASTER_MANIFEST_KINDS


def generation_manifest(version_row: dict[str, Any], master: MasterDeck | None = None) -> dict[str, Any]:
    """Identity of everything one V1 generation is built from."""
    master = master or get_master(DEFAULT_MASTER_ID)
    paper = approved_analysis(version_row)
    return {
        "schema_version": "1.0",
        "kind": MANIFEST_KIND,
        "product_version": PRODUCT_VERSION,
        "product_stage": PRODUCT_STAGE,
        **master.identity(),
        "master_slide_count": master.slide_count,
        "approved_discovery_version_id": str(version_row["id"]),
        "approved_discovery_document_id": str(version_row.get("document_id") or paper.get("document_id") or ""),
        "discovery_schema_version": DISCOVERY_SCHEMA_VERSION,
        "appendix_source_hash": appendix_source_hash(paper),
        "deck_layout": DECK_LAYOUT,
        # BAP-13: the appendix is written in the approved Discovery's language; the canonical
        # master slides stay in the master's own language (``master_language``).
        "language": stored_language(paper.get("language")),
        "master_language": master.language,
    }


def same_source(left: Any, right: Any) -> bool:
    """Two manifests describe the same master, the same approved Discovery content and slide order.

    A deck generated before the closing slide was placed last (no ``deck_layout``) is never the
    same source, so it is generated again instead of being reused.
    """
    keys = ("kind", "master_id", "master_version", "master_sha256", "approved_discovery_version_id", "appendix_source_hash", "deck_layout")
    return isinstance(left, dict) and isinstance(right, dict) and all(left.get(key) == right.get(key) and left.get(key) for key in keys)


def arrange_slides(
    master: MasterDeck, canonical: list[dict[str, Any]], appendix: list[dict[str, Any]], *, closing_last: bool = True
) -> list[dict[str, Any]]:
    """Plan slides in deck order (``registry.deck_order``): canonical, appendix, closing slide last.

    ``canonical`` holds the master's slide entries (``number``, ``title``), ``appendix`` the
    planned appendix slides, numbered from the first appendix position. ``closing_last=False``
    is the order used before BAP-4 (all canonical slides, then the appendix).
    """
    by_number = {slide["number"]: slide for slide in canonical}
    order = (
        deck_order(master, len(appendix))
        if closing_last
        else [("canonical", number) for number in range(1, master.slide_count + 1)] + [("appendix", index) for index in range(len(appendix))]
    )
    slides: list[dict[str, Any]] = []
    for position, (kind, ref) in enumerate(order, start=1):
        if kind == "canonical":
            slide = by_number[ref]
            slides.append(
                {
                    "order": position,
                    "purpose": f"canonical: {slide['title']}",
                    "layoutId": CANONICAL_LAYOUT,
                    "frameworkReferences": [],
                    "borekSlide": {"layout": "canonical", "master_id": master.master_id, "master_slide": slide["number"], "title": slide["title"]},
                }
            )
        else:
            slide = appendix[ref]
            if slide["order"] != position:
                raise ValueError(f"Appendix slide {ref + 1} is planned at {slide['order']}, its place in the deck is {position}")
            slides.append(
                {
                    "order": position,
                    "purpose": slide["purpose"],
                    "layoutId": slide["layout_id"],
                    "frameworkReferences": list(slide["source_references"]),
                    "borekSlide": copy.deepcopy(slide["content"]),
                }
            )
    return slides


def build_master_plan(version_row: dict[str, Any], master: MasterDeck | None = None) -> dict[str, Any]:
    """The stored plan: the canonical slides, the finished appendix slides, the closing slide last."""
    master = master or get_master(DEFAULT_MASTER_ID)
    manifest = verify_master(master)
    paper = approved_analysis(version_row)
    first_slide = first_appendix_position(master)
    appendix = plan_appendix(paper, first_order=first_slide)
    client = str(paper["presentation_brief"].get("client_name") or "").strip()
    slides = arrange_slides(master, manifest["slides"], appendix)
    return {
        "schema_version": "1.0",
        "engine": PLAN_ENGINE,
        "deck_kind": MASTER_V1,
        "prompt_version": PLAN_VERSION,
        "lang": master.language.upper(),
        "content_language": stored_language(paper.get("language")),
        "title": f"Master Presentation — {client}" if client else "Master Presentation",
        "master": {**master.identity(), "slide_count": master.slide_count},
        "deck_layout": DECK_LAYOUT,
        "appendix": {"first_slide": first_slide, "slide_count": len(appendix), "source_hash": appendix_source_hash(paper)},
        "slides": slides,
    }
