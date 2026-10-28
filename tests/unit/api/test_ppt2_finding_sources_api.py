"""Legacy PPT #2 through the API: findings without a source record are refused (PR #16 review, BUG-02)."""

from __future__ import annotations

import copy
import json
from uuid import UUID

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.data.memory_store import get_memory_store, reset_memory_store
from tests.unit.api.test_jj35_post_meeting_ppt2 import (
    approve_discovery,
    create_opportunity,
    extract,
    headers,
    latest_version,
    put_notes,
    upload_document,
    upload_transcript,
)

SAID = "Keep slotting manual"


def test_a_legacy_extraction_without_sources_is_refused_with_an_actionable_error() -> None:
    """Through the API: 409, nothing stored or rendered, the earlier PPT #2 version unchanged."""
    reset_memory_store()
    with TestClient(create_app()) as client:
        opportunity_id = create_opportunity(client)
        upload_document(client, opportunity_id)
        approve_discovery(client, opportunity_id)
        put_notes(client, opportunity_id, "Requirement: The COO decides alone.")
        transcript_id = upload_transcript(client, opportunity_id, b"Ada: Requirement: Keep slotting manual.\n")
        extract(client, opportunity_id, transcript_id)
        first = client.post(f"/opportunities/{opportunity_id}/ppt2/generate", headers=headers())
        assert first.status_code == 200, first.text
        presentation_id = UUID(first.json()["presentation_id"])
        store = get_memory_store()
        version = latest_version(store, presentation_id)
        rendered = json.dumps(version["slides_json"])
        assert SAID in rendered and "COO" not in rendered, "a verified finding is shown, the notes-only one is not"
        before = copy.deepcopy(version)
        counts = (len(store.presentation_plans), len(store.generation_jobs), len(store.presentation_versions))

        # The stored extraction loses its source record, as in a record made before item_sources existed.
        store.opportunities[UUID(opportunity_id)]["meeting_extraction"].pop("item_sources")
        refused = client.post(f"/opportunities/{opportunity_id}/ppt2/{presentation_id}/regenerate", headers=headers())
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert error["code"] == "PPT2_FINDING_SOURCES_UNKNOWN"
        assert "Analyse the meeting again" in error["message"] and "requirements" in error["message"]
        assert (len(store.presentation_plans), len(store.generation_jobs), len(store.presentation_versions)) == counts
        assert latest_version(store, presentation_id) == before, "the earlier version is unchanged"

        # Analysing again records the sources, and generation works again.
        extract(client, opportunity_id, transcript_id)
        again = client.post(f"/opportunities/{opportunity_id}/ppt2/{presentation_id}/regenerate", headers=headers())
        assert again.status_code == 200, again.text
        assert "COO" not in json.dumps(latest_version(store, presentation_id)["slides_json"])
