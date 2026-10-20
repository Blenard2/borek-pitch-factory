"""Shared test env for FastAPI unit tests — must run before app imports."""

from __future__ import annotations

import os

import pytest

_TEST_ENV = {
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "OPENAI_API_KEY": "test-openai-key",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": "test-supabase-anon-key",
    "SUPABASE_SERVICE_ROLE_KEY": "test-supabase-service-role",
    "API_DATA_BACKEND": "memory",
    "AUTH_BYPASS": "false",
    "AI_EXECUTION_MODE": "fixture",
    # BAP-5: no real website is read in tests; research tests use fixture sites.
    "WEBSITE_RESEARCH_ENABLED": "false",
    "SUPABASE_JWT_SECRET": "test-supabase-jwt-secret-with-32-byte-minimum-length",
    "REDIS_URL": "redis://localhost:6379/0",
    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/borek",
    "RENDERER_URL": "http://localhost:4000",
    "RENDERER_EXECUTION_MODE": "fixture",
}

for key, value in _TEST_ENV.items():
    os.environ[key] = value

# Generated decks and exports go to a temporary directory that is removed when the run ends,
# not to tmp/deck_assets in the repository (see tests/fixtures/artifact_root.py).
from tests.fixtures.artifact_root import use_temporary_artifact_root  # noqa: E402

use_temporary_artifact_root()

from app.services.data.memory_store import reset_memory_store
from app.services.job_service import reset_job_store
from tests.fixtures.master_render_double import install_master_render_double
from tests.fixtures.stage_b_test_providers import install_stage_b_test_providers


@pytest.fixture(autouse=True)
def _reset_in_memory_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_memory_store()
    reset_job_store()
    install_stage_b_test_providers(monkeypatch)
    # Only where LibreOffice is not installed; inside the images the real renderer is used.
    install_master_render_double(monkeypatch)
