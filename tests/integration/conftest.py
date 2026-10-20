"""Shared FastAPI test env for integration tests — must run before app imports."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

_RUN_SUPABASE = os.getenv("RUN_SUPABASE_INTEGRATION") == "1"
_PRESERVE_WHEN_SUPABASE = frozenset(
    {
        "DATABASE_URL",
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_JWT_SECRET",
        "SUPABASE_DB_PASSWORD",
        "SUPABASE_DB_POOLER_HOST",
        "RLS_TEST_USER_A_EMAIL",
        "RLS_TEST_USER_A_PASSWORD",
        "RLS_TEST_USER_B_EMAIL",
        "RLS_TEST_USER_B_PASSWORD",
    }
)

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
    "RENDERER_EXECUTION_MODE": "fixture",
    "SUPABASE_JWT_SECRET": "test-supabase-jwt-secret-with-32-byte-minimum-length",
    "REDIS_URL": "redis://localhost:6379/0",
    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/borek",
    "RENDERER_URL": "http://localhost:4000",
}

for key, value in _TEST_ENV.items():
    if _RUN_SUPABASE and key in _PRESERVE_WHEN_SUPABASE:
        continue
    os.environ[key] = value

# Generated decks and exports go to a temporary directory that is removed when the run ends,
# not to tmp/deck_assets in the repository (see tests/fixtures/artifact_root.py).
from tests.fixtures.artifact_root import use_temporary_artifact_root  # noqa: E402

use_temporary_artifact_root()

from app.services.data.memory_store import reset_memory_store
from app.services.job_service import reset_job_store
from tests.fixtures.stage_b_test_providers import install_stage_b_test_providers


@pytest.fixture(autouse=True)
def _reset_in_memory_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_memory_store()
    reset_job_store()
    install_stage_b_test_providers(monkeypatch)


_RLS_CREDENTIAL_KEYS = (
    "RLS_TEST_USER_A_EMAIL",
    "RLS_TEST_USER_A_PASSWORD",
    "RLS_TEST_USER_B_EMAIL",
    "RLS_TEST_USER_B_PASSWORD",
)


def _require_live_rls_users() -> None:
    if os.getenv("RUN_SUPABASE_INTEGRATION") != "1":
        pytest.skip("Set RUN_SUPABASE_INTEGRATION=1 to run live RLS isolation tests")
    missing = [key for key in _RLS_CREDENTIAL_KEYS if not os.getenv(key, "").strip()]
    if missing:
        pytest.skip(
            "Missing RLS test users in .env: " + ", ".join(missing)
        )
    if not os.getenv("SUPABASE_URL", "").strip() or not os.getenv("SUPABASE_ANON_KEY", "").strip():
        pytest.skip("SUPABASE_URL and SUPABASE_ANON_KEY are required for live RLS tests")


def _sign_in_supabase(*, email: str, password: str) -> str:
    import httpx

    supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
    anon_key = os.environ["SUPABASE_ANON_KEY"]
    response = httpx.post(
        f"{supabase_url}/auth/v1/token",
        params={"grant_type": "password"},
        headers={
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
            "Content-Type": "application/json",
        },
        json={"email": email, "password": password},
        timeout=30.0,
    )
    if response.status_code != 200:
        pytest.fail(f"Supabase password grant failed for {email}: HTTP {response.status_code}")
    token = response.json().get("access_token")
    if not token:
        pytest.fail(f"Supabase password grant for {email} returned no access_token")
    return str(token)


def _authed_client(access_token: str, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, "API_DATA_BACKEND", "supabase")
    client = TestClient(create_app())
    client.headers.update({"Authorization": f"Bearer {access_token}"})
    return client


@pytest.fixture(scope="session")
def _rls_access_tokens() -> dict[str, str]:
    _require_live_rls_users()
    return {
        "a": _sign_in_supabase(
            email=os.environ["RLS_TEST_USER_A_EMAIL"],
            password=os.environ["RLS_TEST_USER_A_PASSWORD"],
        ),
        "b": _sign_in_supabase(
            email=os.environ["RLS_TEST_USER_B_EMAIL"],
            password=os.environ["RLS_TEST_USER_B_PASSWORD"],
        ),
    }


@pytest.fixture
def client_user_a(_rls_access_tokens: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    """HTTP client authenticated as RLS user A with a real Supabase JWT."""
    return _authed_client(_rls_access_tokens["a"], monkeypatch)


@pytest.fixture
def client_user_b(_rls_access_tokens: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    """HTTP client authenticated as RLS user B with a real Supabase JWT."""
    return _authed_client(_rls_access_tokens["b"], monkeypatch)


@pytest.fixture
def confirmed_framework_id(client_user_a, _rls_access_tokens: dict[str, str]) -> str:
    import json
    from uuid import UUID

    from app.auth import decode_access_token
    from app.services.data.supabase_store import SupabaseDataStore

    created = client_user_a.post(
        "/opportunities",
        json={
            "client_name": "AT-38 Client A",
            "opportunity_name": "AT-38 Isolated Framework",
            "department": "Finance",
        },
    )
    assert created.status_code == 201, created.text
    opportunity_id = created.json()["id"]
    fixture_path = (
        ROOT / "packages" / "contracts" / "fixtures" / "framework_object.minimal.json"
    )
    framework_json = json.loads(fixture_path.read_text(encoding="utf-8"))
    framework_json["opportunity_id"] = opportunity_id
    framework_json["status"] = "confirmed"
    user = decode_access_token(_rls_access_tokens["a"])
    store = SupabaseDataStore(os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    row = store.create_framework_version(
        opportunity_id=UUID(opportunity_id),
        user_id=user.id,
        framework_json=framework_json,
        status="confirmed",
    )
    return str(row["id"])


@pytest.fixture
def user_a_presentation_id(
    client_user_a, confirmed_framework_id, _rls_access_tokens: dict[str, str]
) -> str:
    from uuid import UUID

    from app.auth import decode_access_token
    from app.services.data.supabase_store import SupabaseDataStore

    user = decode_access_token(_rls_access_tokens["a"])
    store = SupabaseDataStore(_rls_access_tokens["a"])
    plan = store.create_presentation_plan(
        framework_version_id=UUID(confirmed_framework_id),
        user_id=user.id,
        plan_json={
            "schema_version": "1.0",
            "title": "AT-38 Isolation Plan",
            "slides": [],
        },
    )
    presentation = store.create_presentation(
        presentation_plan_id=plan["id"],
        user_id=user.id,
        name="AT-38 Isolated Presentation",
    )
    return str(presentation["id"])


@pytest.fixture
def opportunity_with_transcript(client_user_a) -> tuple[str, str]:
    created = client_user_a.post(
        "/opportunities",
        json={
            "client_name": "AT-38 Client A",
            "opportunity_name": "AT-38 Isolated Transcript",
            "department": "Finance",
        },
    )
    assert created.status_code == 201, created.text
    opportunity_id = created.json()["id"]
    transcript_path = ROOT / "tests" / "fixtures" / "transcripts" / "discovery_call.minimal.txt"
    upload = client_user_a.post(
        f"/opportunities/{opportunity_id}/transcripts",
        files={
            "file": (
                "discovery_call.minimal.txt",
                transcript_path.read_bytes(),
                "text/plain",
            )
        },
    )
    assert upload.status_code == 201, upload.text
    transcript_id = upload.json()["transcript"]["id"]
    return opportunity_id, transcript_id
