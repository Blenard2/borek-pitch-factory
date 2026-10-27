"""Deck center helpers (AT-49)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid5

from services.language import stored_language

from app.services.api_errors import conflict, not_found
from app.services.data import DataStore
from app.services.deck_assets import (
    list_preview_image_paths,
    resolve_gamma_artifact_path,
    resolve_pdf_path,
    resolve_pptx_path,
    resolve_preview_image_path,
)


def build_deck_center_payload(
    store: DataStore,
    *,
    presentation_id: UUID,
    user_id: UUID,
    presentation_version_id: UUID | None = None,
) -> dict[str, object]:
    """The latest ready deck, or one specific ready version of the presentation."""
    presentation = store.get_presentation(presentation_id=presentation_id, user_id=user_id)
    version = _version_assets(
        store,
        presentation_id=presentation_id,
        user_id=user_id,
        presentation_version_id=presentation_version_id,
    )
    _require_ready(version)
    slides = _version_slides(
        store,
        presentation_id=presentation_id,
        user_id=user_id,
        presentation_version_id=presentation_version_id,
    )
    presentation_id_str = _url_prefix(presentation_id, presentation_version_id)
    preview_paths = list_preview_image_paths(
        version_id=version["id"],
        stored_paths=list(version.get("preview_image_paths") or []),
        slide_count=len(slides),
    )

    slide_items: list[dict[str, object]] = []
    if preview_paths:
        for index, _path in enumerate(preview_paths):
            slide = slides[index] if index < len(slides) else None
            slide_items.append(
                {
                    "slide_id": slide["id"] if slide else _preview_placeholder_id(version["id"], index),
                    "slide_index": index,
                    "layout_id": slide["layout_id"] if slide else _fallback_layout_id(slides),
                    "preview_url": (
                        f"/presentations/{presentation_id_str}/preview/slides/{index}.png"
                    ),
                }
            )
    else:
        for slide in slides:
            slide_items.append(
                {
                    "slide_id": slide["id"],
                    "slide_index": slide["slide_index"],
                    "layout_id": slide["layout_id"],
                    "preview_url": (
                        f"/presentations/{presentation_id_str}/preview/slides/{slide['slide_index']}.png"
                    ),
                }
            )

    payload: dict[str, object] = {
        "presentation_id": presentation_id,
        "presentation_name": presentation["name"],
        "version_number": version["version_number"],
        "status": version["status"],
        "slides": slide_items,
        "pptx_download_url": f"/presentations/{presentation_id_str}/download/pptx",
        "pdf_download_url": f"/presentations/{presentation_id_str}/download/pdf",
    }
    if presentation_version_id is not None:
        payload["presentation_version_id"] = version["id"]
    source = _deck_source(version, slide_count=len(slides))
    if source is not None:
        payload["source"] = source
    return payload


def list_ready_versions(
    store: DataStore,
    *,
    presentation_id: UUID,
    user_id: UUID,
) -> list[dict[str, object]]:
    """Every ready version of one presentation, newest first, with its own URLs."""
    opportunity_id = store.get_presentation_opportunity_id(presentation_id=presentation_id, user_id=user_id)
    rows = [
        row
        for row in store.list_presentation_versions_for_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        if str(row["presentation_id"]) == str(presentation_id) and row.get("status") == "ready"
    ]
    rows.sort(key=lambda row: int(row["version_number"]), reverse=True)
    latest = store.get_latest_presentation_version(presentation_id=presentation_id, user_id=user_id)["id"]
    items: list[dict[str, object]] = []
    for row in rows:
        prefix = f"/presentations/{_url_prefix(presentation_id, row['id'])}"
        item: dict[str, object] = {
            "presentation_version_id": row["id"],
            "version_number": row["version_number"],
            "status": row["status"],
            "journey_stage": row.get("journey_stage"),
            "created_at": row.get("created_at"),
            "is_latest": str(row["id"]) == str(latest),
            "deck_url": f"{prefix}/deck",
            "pptx_download_url": f"{prefix}/download/pptx",
            "pdf_download_url": f"{prefix}/download/pdf",
        }
        source = _deck_source(row, slide_count=len(row.get("slides_json") or []))
        if source is not None:
            item["source"] = source
        items.append(item)
    return items


def list_version_slides(
    store: DataStore,
    *,
    presentation_id: UUID,
    user_id: UUID,
    presentation_version_id: UUID,
) -> list[dict]:
    """Slide rows of one specific version; the latest-version route keeps its own behaviour."""
    return _version_slides(
        store,
        presentation_id=presentation_id,
        user_id=user_id,
        presentation_version_id=presentation_version_id,
    )


def _url_prefix(presentation_id: UUID, presentation_version_id: object | None) -> str:
    if presentation_version_id is None:
        return str(presentation_id)
    return f"{presentation_id}/versions/{presentation_version_id}"


def _version_assets(
    store: DataStore,
    *,
    presentation_id: UUID,
    user_id: UUID,
    presentation_version_id: UUID | None,
) -> dict:
    # The latest-version call stays exactly as it was; a version is only named when requested.
    if presentation_version_id is None:
        return store.get_presentation_version_assets(presentation_id=presentation_id, user_id=user_id)
    return store.get_presentation_version_assets(
        presentation_id=presentation_id,
        user_id=user_id,
        presentation_version_id=presentation_version_id,
    )


def _version_slides(
    store: DataStore,
    *,
    presentation_id: UUID,
    user_id: UUID,
    presentation_version_id: UUID | None,
) -> list[dict]:
    if presentation_version_id is None:
        return store.list_slides(presentation_id=presentation_id, user_id=user_id)
    return store.list_slides(
        presentation_id=presentation_id,
        user_id=user_id,
        presentation_version_id=presentation_version_id,
    )


def _deck_source(version: dict[str, object], *, slide_count: int) -> dict[str, object] | None:
    """Master and approved Discovery version of a Master Presentation (V1 or V2); None for other decks."""
    manifest = version.get("generation_source_manifest")
    if not isinstance(manifest, dict) or manifest.get("kind") not in ("master_presentation_v1", "master_presentation_v2"):
        return None
    canonical = int(manifest.get("master_slide_count") or 0)
    lineage = (
        # V2 names the V1 version it follows; the product version is never derived from the revision number.
        {"base_presentation_version_id": manifest["base_presentation_version_id"]}
        if manifest["kind"] == "master_presentation_v2"
        else {}
    )
    return {
        **lineage,
        "kind": manifest["kind"],
        "product_version": str(manifest.get("product_version") or "V1"),
        "product_stage": str(manifest.get("product_stage") or "pre_meeting"),
        "revision": int(version["version_number"]),
        "master_id": manifest["master_id"],
        "master_version": manifest["master_version"],
        "canonical_slide_count": canonical,
        "appendix_slide_count": max(slide_count - canonical, 0),
        "approved_discovery_version_id": manifest["approved_discovery_version_id"],
        "discovery_schema_version": manifest["discovery_schema_version"],
        # BAP-12: the appendix language and the canonical master's own language, so the deck can
        # say which part is in which language. Versions made before BAP-13 are English.
        "language": stored_language(manifest.get("language")),
        "master_language": stored_language(manifest.get("master_language")),
        "other_language_findings": int(manifest.get("other_language_findings") or 0),
    }


def resolve_deck_file_path(
    store: DataStore,
    *,
    presentation_id: UUID,
    user_id: UUID,
    kind: str,
    presentation_version_id: UUID | None = None,
) -> Path:
    version = _version_assets(
        store,
        presentation_id=presentation_id,
        user_id=user_id,
        presentation_version_id=presentation_version_id,
    )
    _require_ready(version)
    version_id = version["id"]
    if kind not in ("pptx", "pdf"):
        raise not_found("DECK_FILE_NOT_FOUND", f"Unknown deck file type: {kind}")

    if kind == "pptx":
        path = Path(version["pptx_storage_path"]) if version.get("pptx_storage_path") else resolve_pptx_path(version_id=version_id)
    else:
        path = Path(version["pdf_storage_path"]) if version.get("pdf_storage_path") else resolve_pdf_path(version_id=version_id)

    if path.is_file():
        return path

    # Historical decks filed before the internal renderer was the only engine.
    # Read-only: new renders do not write these files.
    legacy_path = _resolve_legacy_gamma_deck_file(
        store,
        presentation_id=presentation_id,
        user_id=user_id,
        version_id=version_id,
        kind=kind,
    )
    if legacy_path is not None:
        return legacy_path

    raise not_found("DECK_FILE_NOT_FOUND", f"Deck {kind} file is not available")


def _resolve_legacy_gamma_deck_file(
    store: DataStore,
    *,
    presentation_id: UUID,
    user_id: UUID,
    version_id: object,
    kind: str,
) -> Path | None:
    try:
        opportunity_id = store.get_presentation_opportunity_id(
            presentation_id=presentation_id,
            user_id=user_id,
        )
    except Exception:
        return None
    return resolve_gamma_artifact_path(
        opportunity_id=opportunity_id,
        version_id=version_id,
        kind=kind,
    )


def resolve_deck_preview_image_path(
    store: DataStore,
    *,
    presentation_id: UUID,
    user_id: UUID,
    slide_index: int,
    presentation_version_id: UUID | None = None,
) -> Path:
    version = _version_assets(
        store,
        presentation_id=presentation_id,
        user_id=user_id,
        presentation_version_id=presentation_version_id,
    )
    _require_ready(version)
    preview_paths = list_preview_image_paths(
        version_id=version["id"],
        stored_paths=list(version.get("preview_image_paths") or []),
        slide_count=len(version.get("preview_image_paths") or []) or 0,
    )
    if slide_index < len(preview_paths):
        path = Path(str(preview_paths[slide_index]))
        if path.is_file():
            return path

    path = resolve_preview_image_path(version_id=version["id"], slide_index=slide_index)
    if not path.is_file():
        raise not_found(
            "SLIDE_PREVIEW_NOT_FOUND",
            f"Preview image for slide {slide_index + 1} was not found",
        )
    return path


def _fallback_layout_id(slides: list[dict]) -> str:
    if slides:
        return str(slides[-1]["layout_id"])
    return "COVER_01"


def _preview_placeholder_id(version_id: object, index: int) -> UUID:
    return uuid5(UUID(str(version_id)), f"preview-page-{index}")


def _require_ready(version: dict) -> None:
    if version.get("status") != "ready":
        raise conflict(
            "PRESENTATION_NOT_READY",
            "Presentation artifacts are not ready for preview or download",
        )
