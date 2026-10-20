"""BT-34: additive Stage 1 sales intake (not meeting transcript input)."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Stage1Intake(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    client_web_page: str | None = Field(default=None, max_length=2048)
    poc_name: str | None = Field(default=None, max_length=200)
    poc_position: str | None = Field(default=None, max_length=200)
    sales_topic_description: str | None = Field(default=None, max_length=20_000)
    about_company: str | None = Field(default=None, max_length=20_000)

    @field_validator("*")
    @classmethod
    def normalize_empty(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("Stage 1 intake must not contain NUL characters")
        return value if value is not None and value.strip() else None

    @field_validator("client_web_page")
    @classmethod
    def validate_web_page(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            url = urlsplit(value)
            port = url.port
        except ValueError as exc:
            raise ValueError("client_web_page must be an absolute HTTP(S) URL") from exc
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or any(char.isspace() or ord(char) < 32 for char in value)
            or port == 0
        ):
            raise ValueError(
                "client_web_page must be an absolute HTTP(S) URL without credentials"
            )
        return value


class AdditionalInformationUpdate(BaseModel):
    """BAP-10: the "Additional information" of Client Information (``stage1_intake.about_company``).

    Updated on its own, so a live session can add company context after a failed website
    analysis without sending - and possibly overwriting - the other client fields. Same rules as
    ``Stage1Intake.about_company``: at most 20,000 characters, no NUL, blank clears it.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    additional_information: str | None = Field(default=None, max_length=20_000)

    @field_validator("additional_information")
    @classmethod
    def normalize_empty(cls, value: str | None) -> str | None:
        return Stage1Intake.normalize_empty(value)
