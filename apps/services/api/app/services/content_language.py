"""BAP-13: which language an artifact is generated in, and why a request may be refused.

Discovery 1 sets the language (request, else the opportunity's language, else English). Every later
artifact is built from an approved version and inherits its language:

    Master Presentation V1   <- approved Discovery 1
    Discovery 2              <- approved Discovery 1
    Master Presentation V2   <- approved Discovery 2
    follow-up email          <- the finalized V2 package (its Discovery 2)

A request may name the language explicitly; when it differs from the approved source, it is refused
with ``LANGUAGE_MISMATCH`` instead of producing a document in two languages. To change the
language, Discovery 1 is generated and approved again in the new language - a new version; the
earlier, approved versions stay as they are.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.services.api_errors import conflict
from services.language import LANGUAGE_NAMES, normalize_language, stored_language


def require_source_language(requested: str | None, source_language: Any, *, artifact: str, source: str) -> str:
    """The language to generate ``artifact`` in: the source's, or the requested one when it matches."""
    language = stored_language(source_language)
    if requested is None:
        return language
    wanted = normalize_language(requested)
    if wanted != language:
        raise conflict(
            "LANGUAGE_MISMATCH",
            f"{artifact} is generated in the language of the approved {source} it builds on "
            f"({LANGUAGE_NAMES[language]}). Generate and approve the {source} in {LANGUAGE_NAMES[wanted]} first.",
        )
    return language


def approved_discovery1_language(store: Any, *, opportunity_id: UUID, user_id: UUID) -> str | None:
    """The language of the latest approved Discovery 1, or None when nothing is approved yet."""
    approved = store.get_latest_approved_discovery_paper(opportunity_id=opportunity_id, user_id=user_id)
    if approved is None:
        return None
    paper = approved.get("paper_json") if isinstance(approved.get("paper_json"), dict) else {}
    return stored_language(paper.get("language"))


def approved_discovery2_language(store: Any, *, opportunity_id: UUID, user_id: UUID) -> str | None:
    """The language of the latest approved Discovery 2, or None when nothing is approved yet."""
    approved = [row for row in store.list_discovery2_versions(opportunity_id=opportunity_id, user_id=user_id) if row.get("status") == "approved"]
    if not approved:
        return None
    document = approved[-1].get("document_json") if isinstance(approved[-1].get("document_json"), dict) else {}
    return stored_language(document.get("language"))
