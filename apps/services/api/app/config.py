"""Application settings — env-only configuration (AT-34)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.database_url import resolve_database_url

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEV_AUTH_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class Settings(BaseSettings):
    """All required backend configuration from environment variables."""

    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ANTHROPIC_API_KEY: str = Field(..., min_length=1)
    RUNTIME_PROFILE: Literal["development", "test", "production"] = Field(
        default="development",
        description="Deployment safety profile; production refuses demo-data installation",
    )
    DEMO_DATA_ENABLED: bool = Field(
        default=False,
        description="Explicit opt-in for installing the MS-30 demo fixture pack",
    )
    AUTH_BYPASS: bool = Field(
        default=False,
        description=(
            "Development-only auth bypass: skip JWT auth and act as DEV_AUTH_USER_ID / DEV_AUTH_EMAIL. "
            "Requires API_DATA_BACKEND=memory; ignored when RUNTIME_PROFILE=production"
        ),
    )
    DEV_AUTH_USER_ID: str = Field(
        default="",
        description="UUID of the development user; required when AUTH_BYPASS is active",
    )
    DEV_AUTH_EMAIL: str = Field(
        default="",
        description="Email of the development user; required when AUTH_BYPASS is active",
    )
    STAGE_B_LLM_PROVIDER: Literal["anthropic", "openai"] = Field(
        default="anthropic",
        description="Live LLM provider for Stage B planning, slide generation and compression",
    )
    OPENAI_API_KEY: str = Field(
        default="",
        description="Required only when AI_EXECUTION_MODE=live",
    )
    OPENAI_PRESENTATION_MODEL: str = Field(default="gpt-4.1-mini", min_length=1)
    SUPABASE_URL: str = Field(..., min_length=1)
    SUPABASE_ANON_KEY: str = Field(..., min_length=1)
    SUPABASE_SERVICE_ROLE_KEY: str = Field(..., min_length=1)
    SUPABASE_JWT_SECRET: str = Field(
        default="",
        description="Optional legacy HS256 secret; real tokens verify via SUPABASE_URL JWKS",
    )
    SUPABASE_DB_PASSWORD: str = Field(
        default="",
        description="Supabase Postgres password; builds DATABASE_URL when set",
    )
    SUPABASE_DB_POOLER_HOST: str = Field(
        default="",
        description="Optional pooler host (IPv4) from Dashboard > Database > Connection pooling",
    )
    REDIS_URL: str = Field(..., min_length=1)
    DATABASE_URL: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/borek",
        min_length=1,
    )
    RENDERER_URL: str = Field(..., min_length=1)
    RENDERER_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0)
    RENDERER_EXECUTION_MODE: Literal["fixture", "live"] = Field(
        default="live",
        description="fixture only for isolated tests; live invokes the renderer service",
    )
    DECK_PDF_ENGINE: Literal["preview", "libreoffice"] = Field(
        default="preview",
        description=(
            "PDF of the pre-/post-meeting decks (Borek deck generator): preview builds it from the "
            "slide renders and needs no extra software; libreoffice uses soffice for selectable text "
            "and falls back to preview when soffice is missing"
        ),
    )
    ARTIFACT_ROOT: str = Field(
        default="tmp/deck_assets",
        min_length=1,
        description="Private filesystem shared by API and worker for generated artifacts",
    )
    FILING_DESTINATION: Literal["fixture", "in_app", "live"] = Field(
        default="in_app",
        description="in_app stores under Borek control; live waits for the O2 repository",
    )
    ENTERPRISE_REPOSITORY_URL: str = Field(
        default="",
        description="Enterprise repository base URL; required only when FILING_DESTINATION=live",
    )
    ENTERPRISE_REPOSITORY_TOKEN: str = Field(
        default="",
        description="Enterprise repository credential; required only when FILING_DESTINATION=live",
    )
    ENTERPRISE_REPOSITORY_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0)
    API_DATA_BACKEND: str = Field(
        default="supabase",
        description="memory for unit tests; supabase for PostgREST with caller JWT",
    )
    DEFAULT_EMPLOYEE_ROLE: Literal["consultant", "reviewer", "releaser", "admin"] = Field(
        default="consultant",
        description="Role assigned on first employee login when an admin already exists",
    )
    AI_EXECUTION_MODE: Literal["fixture", "live"] = Field(
        default="fixture",
        description="fixture for deterministic local/test runs; live invokes configured LLM providers",
    )
    WEBSITE_RESEARCH_ENABLED: bool = Field(
        default=True,
        description=(
            "BAP-5: read the client's public website (homepage and a few relevant pages, bounded and "
            "SSRF-protected) for the Discovery analysis. Off in unit tests; when off, Discovery uses the "
            "user's input only and says so"
        ),
    )
    FOLLOWUP_AI_WRITER_ENABLED: bool = Field(
        default=False,
        description=(
            "Follow-up email: let the configured model write the drafts (with the grounding checks). "
            "Only honoured when AI_EXECUTION_MODE=live. Off by default: the rule-based writer is used"
        ),
    )

    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    @property
    def dev_auth_active(self) -> bool:
        """True only when the bypass is requested outside the production profile."""
        return self.AUTH_BYPASS and self.RUNTIME_PROFILE != "production"

    @model_validator(mode="after")
    def validate_dev_auth(self) -> Self:
        """Fail at startup rather than bypass row-level security or assume an identity."""
        if not self.dev_auth_active:
            return self
        if self.API_DATA_BACKEND != "memory":
            raise ValueError(
                "AUTH_BYPASS=true is only supported with API_DATA_BACKEND=memory. "
                f"With API_DATA_BACKEND={self.API_DATA_BACKEND} it would skip row-level security; "
                "use real Microsoft/Supabase sign-in for Supabase-backed development."
            )
        try:
            UUID(self.DEV_AUTH_USER_ID.strip())
        except ValueError as exc:
            raise ValueError(
                "AUTH_BYPASS=true requires DEV_AUTH_USER_ID to be a valid UUID "
                "(see .env.example); there is no default development identity."
            ) from exc
        if not _DEV_AUTH_EMAIL_PATTERN.fullmatch(self.DEV_AUTH_EMAIL.strip()):
            raise ValueError(
                "AUTH_BYPASS=true requires DEV_AUTH_EMAIL to be a valid email address "
                "(see .env.example); there is no default development identity."
            )
        return self

    @model_validator(mode="after")
    def apply_resolved_database_url(self) -> Self:
        self.DATABASE_URL = resolve_database_url(
            database_url=self.DATABASE_URL,
            supabase_url=self.SUPABASE_URL,
            supabase_db_password=self.SUPABASE_DB_PASSWORD,
            supabase_db_pooler_host=self.SUPABASE_DB_POOLER_HOST,
        )
        return self


def load_settings() -> Settings:
    """Load settings and fail fast when required env vars are missing."""
    return Settings()


settings = load_settings()
