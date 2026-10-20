"""Env for AT-53 tests that import the API data stores."""

from __future__ import annotations

import os

_TEST_ENV = {
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "OPENAI_API_KEY": "test-openai-key",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": "test-supabase-anon-key",
    "SUPABASE_SERVICE_ROLE_KEY": "test-supabase-service-role",
    "API_DATA_BACKEND": "memory",
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
    os.environ.setdefault(key, value)

# Generated decks and exports go to a temporary directory that is removed when the run ends,
# not to tmp/deck_assets in the repository (see tests/fixtures/artifact_root.py).
from tests.fixtures.artifact_root import use_temporary_artifact_root  # noqa: E402

use_temporary_artifact_root()
