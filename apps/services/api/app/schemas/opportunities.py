"""Opportunity API schemas (AT-40 / v2 §22.1)."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.stage1 import Stage1Intake
from services.language import ContentLanguage

_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class ClientContact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=100)


class AdditionalClientInformation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_requirements: list[str] = Field(default_factory=list, max_length=100)
    constraints: list[str] = Field(default_factory=list, max_length=100)
    contacts: list[ClientContact] = Field(default_factory=list, max_length=100)
    priorities: list[str] = Field(default_factory=list, max_length=100)
    notes: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def reject_blank_list_values(self) -> AdditionalClientInformation:
        for field_name in ("location_requirements", "constraints", "priorities"):
            values = getattr(self, field_name)
            if any(not value.strip() for value in values):
                raise ValueError(f"{field_name} cannot contain blank values")
        return self


class FollowupRecipient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=3, max_length=320)
    first_name: str | None = Field(default=None, max_length=200)
    last_name: str | None = Field(default=None, max_length=200)
    salutation: str | None = Field(default=None, max_length=100)
    kind: Literal["to", "cc"] = "to"
    primary: bool = False

    @model_validator(mode="after")
    def validate_recipient(self) -> FollowupRecipient:
        if not _EMAIL_PATTERN.fullmatch(self.email.strip()) or any(
            value is not None and not value.strip()
            for value in (self.first_name, self.last_name, self.salutation)
        ):
            raise ValueError("recipient fields must contain valid non-blank values")
        return self


class FollowupSenderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    role: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., min_length=3, max_length=320)

    @model_validator(mode="after")
    def validate_sender(self) -> FollowupSenderProfile:
        if (
            not self.name.strip()
            or not self.role.strip()
            or not _EMAIL_PATTERN.fullmatch(self.email.strip())
        ):
            raise ValueError("sender profile must contain a name, role, and valid email")
        return self


class FollowupProjectStatics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_name: str = Field(..., min_length=1, max_length=200)
    client_short: str = Field(..., min_length=1, max_length=200)
    salutation_style: Literal["informal", "formal"]
    standard_recipients: list[FollowupRecipient] = Field(min_length=1, max_length=20)
    sender_profile: FollowupSenderProfile

    @model_validator(mode="after")
    def validate_project_statics(self) -> FollowupProjectStatics:
        if not self.project_name.strip() or not self.client_short.strip():
            raise ValueError("project_name and client_short cannot be blank")
        primary = [
            recipient
            for recipient in self.standard_recipients
            if recipient.kind == "to" and recipient.primary
        ]
        if len(primary) != 1:
            raise ValueError("exactly one primary to recipient is required")
        if self.salutation_style == "informal" and not primary[0].first_name:
            raise ValueError("informal salutation requires the primary recipient first_name")
        if self.salutation_style == "formal" and not (
            primary[0].salutation and primary[0].last_name
        ):
            raise ValueError("formal salutation requires salutation and last_name")
        return self


class PitchOwnerRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["employee"] = "employee"
    employee_id: UUID


class OpportunityCreateRequest(BaseModel):
    # User-entered names are optional: an empty string means "not provided" and is never replaced
    # by a made-up value. The columns stay NOT NULL, so missing names are stored as "".
    client_name: str = Field(default="", max_length=500)
    opportunity_name: str = Field(default="", max_length=500)
    department: str = Field(..., min_length=1)
    language: ContentLanguage = "en"
    pii_redaction_enabled: bool = True
    pitch_description: str | None = Field(default=None, max_length=20_000)
    team_members_text: str | None = Field(default=None, max_length=2_000)
    additional_client_information: AdditionalClientInformation | None = None
    followup_statics: FollowupProjectStatics | None = None
    stage1_intake: Stage1Intake | None = None


class OpportunityUpdateRequest(BaseModel):
    client_name: str | None = Field(default=None, max_length=500)
    opportunity_name: str | None = Field(default=None, max_length=500)
    department: str | None = Field(default=None, min_length=1)
    language: ContentLanguage | None = None
    status: str | None = Field(default=None, min_length=1)
    pii_redaction_enabled: bool | None = None
    additional_client_information: AdditionalClientInformation | None = None
    followup_statics: FollowupProjectStatics | None = None
    stage1_intake: Stage1Intake | None = None


class OpportunityResponse(BaseModel):
    id: UUID
    client_name: str
    opportunity_name: str
    department: str
    language: str
    status: str
    pii_redaction_enabled: bool = True
    additional_client_information: AdditionalClientInformation | None = None
    followup_statics: FollowupProjectStatics | None = None
    stage1_intake: Stage1Intake | None = None
    pitch_owner: PitchOwnerRef | None = None
    team_members: list[str] = Field(default_factory=list)
    pitch_description: str | None = None
    demo_marker: str | None = None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


class ClientLogoMetadata(BaseModel):
    id: UUID
    opportunity_id: UUID
    file_name: str
    mime_type: str
    size_bytes: int
    width_px: int | None = None
    height_px: int | None = None
    demo_marker: str | None = None
    uploaded_at: datetime


class RecentWorkJob(BaseModel):
    job_type: str
    status: str
    current_stage: str | None = None
    auto_continue: bool = False


class RecentWorkDeck(BaseModel):
    pptx_download_url: str


class RecentWorkOpportunity(BaseModel):
    id: UUID
    client_name: str
    opportunity_name: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime


class RecentWorkSnapshot(BaseModel):
    opportunity: RecentWorkOpportunity
    transcript_count: int
    framework_status: str | None = None
    has_plan: bool = False
    presentation_id: UUID | None = None
    presentation_name: str | None = None
    deck: RecentWorkDeck | None = None
    resource_load_failed: bool = False
    activity_at: datetime | str | None = None
    job: RecentWorkJob | None = None


class FiledArtifactResponse(BaseModel):
    idempotency_key: str
    opportunity_id: UUID
    presentation_id: UUID
    presentation_version_id: UUID
    framework_version_id: UUID
    artifact_kind: str
    content_type: str
    provider: str
    destination_path: str
    repository_ref: str | None = None
    status: str
    approved_by: UUID
    approved_at: datetime
    corpus_versions: list[str] = Field(default_factory=list)
    filed_at: datetime | None = None
    error_code: str | None = None
    error_retryable: bool | None = None
    demo_marker: str | None = None
    updated_at: datetime | None = None
