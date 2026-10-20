# Borek AI Suite API (AT-34 / AT-35)

FastAPI service for the Framework & Presentation Pipeline. LLM calls and background jobs are **server-side only** — the web app never holds provider API keys.

## Run locally

From the repository root (recommended — loads `.env` from repo root):

```bash
# From the repository root (required so `app` and `services` both resolve)
py -3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 --app-dir apps/services/api
```

The API reads `borek-ai-suite/.env` automatically regardless of which subdirectory you start from.

Environment variables (see `app/config.py`):

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY` (required only when `AI_EXECUTION_MODE=live`)
- `OPENAI_PRESENTATION_MODEL` (optional; defaults to `gpt-4.1-mini`)
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `REDIS_URL`
- `DATABASE_URL`
- `RENDERER_URL`
- `WEBSITE_RESEARCH_ENABLED` (optional, opt-in; default `false`): `true` lets Discovery read the client's public website. Needs outbound HTTPS; see [docs/website-research.md](../../../docs/website-research.md)

Missing required vars cause **fail-fast** startup errors from Pydantic settings validation.

## Health check

```http
GET /health
→ 200 {"status": "ok"}
```

No authentication. No database connectivity check (infrastructure liveness only).

## Error response format

Every API error uses this JSON shape:

```json
{
  "error": {
    "code": "OPPORTUNITY_NOT_FOUND",
    "message": "Human readable message",
    "detail": {}
  }
}
```

HTTP status codes:

| Code | Usage |
|------|--------|
| 400 | Bad request |
| 404 | Not found |
| 422 | Validation error |
| 500 | Internal server error |

Raw Python tracebacks are **never** returned to clients.

## Add a new router

1. Create `app/routers/my_feature.py`:

```python
from fastapi import APIRouter

router = APIRouter()

@router.get("")
def list_items():
    return []
```

2. Register it in `app/main.py`:

```python
from app.routers import my_feature

app.include_router(my_feature.router, prefix="/my-feature", tags=["my-feature"])
```

3. Add unit tests under `tests/unit/api/`.

## Add a new `Depends()` function

1. Define the dependency in `app/dependencies.py`:

```python
async def get_service() -> MyService:
    return MyService()

ServiceDep = Annotated[MyService, Depends(get_service)]
```

2. Inject it in route handlers:

```python
def handler(service: ServiceDep):
    ...
```

Shared dependencies (database session, auth) live in `dependencies.py` — do not duplicate `Depends()` wiring per router.

## Celery worker (AT-35)

Same codebase, different entrypoint:

```bash
cd apps/services/api
celery -A app.worker worker --loglevel=info
```

Wiring test endpoints (dev only):

```http
POST /jobs/health-check  → {"job_id": "...", "status": "queued"}
GET  /jobs/{job_id}      → {"job_id": "...", "status": "SUCCESS", ...}
```

Integration test (requires running Redis):

```bash
pytest tests/integration/api -m integration -v
```

Integration tests are excluded from `scripts/validate_all.py`.
