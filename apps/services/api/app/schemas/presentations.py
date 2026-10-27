"""Presentation API schemas (AT-42 / AT-43 / AT-44 / v2 section 22.3–22.4)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.jobs import JobEnqueueResponse

_LAYOUT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[5]
    / "packages"
    / "contracts"
    / "layout_registry.json"
)
LAYOUT_REGISTRY = json.loads(_LAYOUT_REGISTRY_PATH.read_text(encoding="utf-8"))["layouts"]
VALID_LAYOUT_IDS = frozenset(LAYOUT_REGISTRY.keys())


class GeneratePresentationPlanRequest(BaseModel):
    framework_version_id: UUID | None = None
    auto_continue: bool = False
    journey_stage: str | None = None


class GeneratePresentationRequest(BaseModel):
    framework_version_id: UUID | None = None
    presentation_plan_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    journey_stage: str | None = None


class PresentationPlanResponse(BaseModel):
    id: UUID
    framework_version_id: UUID
    plan_json: dict[str, Any]
    created_at: datetime


class PresentationResponse(BaseModel):
    id: UUID
    presentation_plan_id: UUID
    name: str
    status: str
    created_at: datetime


class PresentationPlanGenerateResponse(JobEnqueueResponse):
    presentation_plan_id: UUID | None = None


class PresentationGenerateResponse(JobEnqueueResponse):
    presentation_id: UUID | None = None
    presentation_plan_id: UUID | None = None


class ChangeSlideLayoutRequest(BaseModel):
    layout_id: str = Field(..., min_length=1)


class SlideResponse(BaseModel):
    id: UUID
    presentation_version_id: UUID
    slide_index: int
    layout_id: str
    slide_spec: dict[str, Any]
    source_chapter_ids: list[str]
    created_at: datetime


class DeckSlidePreviewResponse(BaseModel):
    slide_id: UUID
    slide_index: int
    layout_id: str
    preview_url: str


class DeckSourceResponse(BaseModel):
    """What a Master Presentation version was built from. Identifiers only, never paths."""

    kind: str
    # Product stage ("V1", "pre_meeting") and the technical revision of this presentation.
    product_version: str
    product_stage: str
    revision: int
    master_id: str
    master_version: str
    canonical_slide_count: int
    appendix_slide_count: int
    approved_discovery_version_id: UUID
    discovery_schema_version: str
    # Master Presentation V2 only: the V1 version of the same presentation it follows.
    base_presentation_version_id: UUID | None = None
    # BAP-12: the appendix language (the approved Discovery's) and the canonical master's own
    # language - the master slides are never translated. Versions made before BAP-13 are English.
    language: Literal["en", "de"] = "en"
    master_language: Literal["en", "de"] = "en"
    # V2: confirmed findings shown in their own words that read as the other language.
    other_language_findings: int = 0


class PresentationVersionSummaryResponse(BaseModel):
    """One ready version of a presentation and where to read it. Identifiers only, never paths."""

    presentation_version_id: UUID
    version_number: int
    status: str
    journey_stage: str | None = None
    created_at: datetime | None = None
    is_latest: bool
    source: DeckSourceResponse | None = None
    deck_url: str
    pptx_download_url: str
    pdf_download_url: str


class DeckCenterResponse(BaseModel):
    presentation_id: UUID
    # Set only when one specific version was requested; the latest-deck payload is unchanged.
    presentation_version_id: UUID | None = None
    presentation_name: str
    version_number: int
    status: str
    source: DeckSourceResponse | None = None
    slides: list[DeckSlidePreviewResponse]
    pptx_download_url: str
    pdf_download_url: str
