"""BAP-7 Discovery 2: the post-meeting Discovery document.

Discovery 2 is a document of its own, next to the approved pre-meeting Discovery (Discovery 1),
which it never changes. It is built from four sources:

    approved Discovery 1    what was established before the meeting (carried forward)
    selected transcript     what was said in the meeting
    personal notes          the owner's own observations
    confirmed findings      the meeting findings the owner confirmed in the Post Meeting review

Every item keeps its origin visible. Items carried forward from Discovery 1 quote Discovery 1;
meeting items quote the transcript or the notes. Nothing else is added:

* fixture mode is deterministic: Discovery 1 content plus the confirmed findings, word for word;
* live mode asks the configured model for the meeting items and keeps only those whose evidence
  quote occurs in the transcript or the notes. Carried-forward items always use the Discovery 1
  wording, and every confirmed finding is part of the document even when the model left it out.

The module is pure: it reads only its arguments.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import FormatChecker

from services.meeting.extraction import _comparable, _evidence_source

SCHEMA_VERSION = "1.0"
KIND = "post_meeting_discovery"
GENERATOR_VERSION = "discovery2:v1"
PROMPT_VERSION = "discovery2-generation:v1"
STAGE_DISCOVERY2 = "discovery2_generation"
CATEGORIES = ("requirements", "challenges", "priorities", "opportunities", "discussed_solutions", "decisions", "follow_ups")
SECTION_TITLES = {
    "context": "Context",
    "requirements": "Requirements",
    "challenges": "Challenges",
    "priorities": "Priorities",
    "opportunities": "Opportunities",
    "discussed_solutions": "Discussed solutions",
    "decisions": "Decisions",
    "follow_ups": "Follow-ups",
    "open_questions": "Open questions",
}
SECTION_KEYS = tuple(SECTION_TITLES)
MEETING_SOURCES = frozenset({"transcript", "personal_notes", "both"})
# Long enough for any statement made in a meeting; content is never cut, an over-long edit is refused.
MAX_ITEM_CHARACTERS = 4000

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCHEMA_PATH = _REPO_ROOT / "packages" / "contracts" / "post_meeting_discovery.schema.json"
_PROMPT_PATH = _REPO_ROOT / "apps" / "api" / "llm" / "claude" / "prompts" / "discovery2_generation_v1.txt"

Complete = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class Discovery2Error(ValueError):
    """The inputs or the document violate the Discovery 2 contract. ``code`` is API-safe."""

    def __init__(self, message: str, code: str = "DISCOVERY2_INVALID") -> None:
        super().__init__(message)
        self.code = code
        self.user_message = message


# ----------------------------------------------------------------------------- contract


def load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_discovery2(document: dict[str, Any]) -> dict[str, Any]:
    errors = sorted(
        jsonschema.Draft202012Validator(load_schema(), format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path) or "<root>"
        raise Discovery2Error(f"{location}: {first.message.splitlines()[0]}")
    keys = [section["key"] for section in document["sections"]]
    if document["status"] == "ready" and keys != list(SECTION_KEYS):
        raise Discovery2Error("A ready Discovery 2 has every section once, in order")
    ids = [item["id"] for section in document["sections"] for item in section["items"]]
    if len(ids) != len(set(ids)):
        raise Discovery2Error("Discovery 2 item ids must be unique")
    return document


def empty_discovery2(opportunity: dict[str, Any]) -> dict[str, Any]:
    return validate_discovery2(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "opportunity_id": str(opportunity["id"]),
            "document_id": None,
            "status": "not_generated",
            "generated_at": None,
            "error": None,
            "generation": None,
            "title": "",
            "client_name": str(opportunity.get("client_name") or "").strip(),
            "sources": None,
            "summary": None,
            "sections": [],
        }
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_hash(document: dict[str, Any]) -> str:
    """Identity of what the document says: title, summary and every item with its origin."""
    return sha256_text(canonical_json({key: document.get(key) for key in ("title", "summary", "sections")}))


def discovery1_content_hash(paper: dict[str, Any]) -> str:
    return sha256_text(canonical_json({"analysis": paper.get("analysis"), "presentation_brief": paper.get("presentation_brief")}))


def meeting_items(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The meeting and owner items of the seven finding categories: what V2 presents from the meeting."""
    by_key = {section["key"]: section for section in document.get("sections") or []}
    return {
        category: [item for item in (by_key.get(category) or {"items": []})["items"] if item["origin"] in {"meeting", "owner"}]
        for category in CATEGORIES
    }


# ----------------------------------------------------------------------------- inputs


def carried_forward_items(paper: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """What Discovery 1 established, as Discovery 2 items in Discovery 1's own wording."""
    if paper.get("schema_version") != "2.0" or not isinstance(paper.get("analysis"), dict):
        raise Discovery2Error(
            "Discovery 2 needs an approved Discovery analysis (schema 2.0). Generate and approve the Discovery again.",
            "DISCOVERY2_DISCOVERY1_UNSUPPORTED",
        )
    analysis = paper["analysis"]
    brief = paper.get("presentation_brief") or {}
    items: dict[str, list[dict[str, Any]]] = {key: [] for key in SECTION_KEYS}

    def carry(section: str, ref: str, text: Any) -> None:
        value = " ".join(str(text or "").split())
        if value and value.casefold() not in {item["text"].casefold() for item in items[section]}:
            items[section].append(_item(f"d1-{len(items[section]) + 1}-{section}", value, origin="discovery1", status="carried_forward", source="discovery1", discovery1_ref=ref))

    carry("context", "analysis.research.core_thesis", analysis["research"]["core_thesis"]["text"])
    purpose = brief.get("meeting_purpose") or (paper.get("intake_context") or {}).get("meeting_purpose")
    if purpose:
        carry("context", "intake_context.meeting_purpose", f"Meeting purpose: {purpose}")
    target = (((analysis.get("framing") or {}).get("chapters") or {}).get("target") or {}).get("key_message")
    if target:
        carry("context", "analysis.framing.chapters.target.key_message", target)
    # Every opportunity of Discovery 1, the brief's priorities first: Discovery 2 is the complete
    # post-meeting basis, and Master Presentation V2 recommends only what Discovery 2 carries.
    areas = analysis.get("areas") or []
    by_name = {area["name"]: area for area in areas}
    ordered: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for priority in brief.get("priority_opportunities") or []:
        area = by_name.get(priority.get("area"))
        if area and area.get("opportunities"):
            ordered.append((area, area["opportunities"][0]))
    for area in areas:
        for opportunity in area.get("opportunities") or []:
            if (area, opportunity) not in ordered:
                ordered.append((area, opportunity))
    for area, opportunity in ordered:
        ref = f"area:{area['id']}/opportunity:{opportunity['id']}"
        carry("opportunities", ref, f"{area['name']}: {opportunity['title']} — {opportunity['result']}")
        # One open question per opportunity: the first full question Discovery 1 asks about it.
        questions = [str(question) for question in opportunity.get("discovery_questions") or []]
        question = next((text for text in questions if text.strip().endswith("?") and len(text.split()) >= 5), questions[0] if questions else None)
        if question:
            carry("open_questions", f"{ref}/question", question)
    return items


def discovery1_reference_texts(paper: dict[str, Any]) -> dict[str, str]:
    """Every Discovery 1 element a live item may refer to, by reference."""
    references = {item["discovery1_ref"]: item["text"] for items in carried_forward_items(paper).values() for item in items}
    for area in paper["analysis"].get("areas") or []:
        for opportunity in area.get("opportunities") or []:
            references.setdefault(f"area:{area['id']}/opportunity:{opportunity['id']}", f"{area['name']}: {opportunity['title']} — {opportunity['result']}")
    return references


def basis_hash(
    *,
    discovery1_version_id: str,
    transcript_id: str,
    transcript_revision: str,
    personal_notes_text: str | None,
    selected_use_case_ids: list[str],
    reviewed_items: dict[str, list[dict[str, Any]]],
) -> str:
    """Content identity of the meeting basis a Discovery 2 is generated from.

    Leaves out timestamps: confirming unchanged findings again, or re-analysing a transcript into
    the same findings, keeps an approved Discovery 2 current.
    """
    notes = (personal_notes_text or "").strip()
    return sha256_text(
        canonical_json(
            {
                "discovery1_version_id": discovery1_version_id,
                "transcript": [transcript_id, transcript_revision],
                "personal_notes_sha256": sha256_text(notes) if notes else None,
                "selected_use_case_ids": list(selected_use_case_ids),
                "reviewed": {
                    category: [[str(item["text"]), str(item.get("source")), str(item.get("status"))] for item in reviewed_items.get(category, [])]
                    for category in CATEGORIES
                },
            }
        )
    )


# ----------------------------------------------------------------------------- generation


def generate_discovery2(
    *,
    opportunity: dict[str, Any],
    document_id: str,
    generated_at: str,
    discovery1: dict[str, Any],
    transcript: dict[str, Any],
    personal_notes: dict[str, Any],
    extraction: dict[str, Any],
    confirmation: dict[str, Any],
    basis: str,
    live: bool,
    complete: Complete | None = None,
) -> dict[str, Any]:
    """A complete ready Discovery 2. ``complete`` is required in live mode (the model call)."""
    paper = discovery1.get("paper_json")
    if str(discovery1.get("status")) != "approved" or not isinstance(paper, dict):
        raise Discovery2Error("Approve the Discovery document before generating Discovery 2.", "DISCOVERY_NOT_APPROVED")
    sections_text = transcript.get("text") or ""
    if not str(sections_text).strip():
        raise Discovery2Error("The selected transcript has no readable text. Upload the transcript again.", "TRANSCRIPT_EMPTY")
    reviewed = confirmation.get("items") or {}
    confirmed = {
        category: [item for item in reviewed.get(category, []) if item.get("status") == "confirmed"]
        for category in CATEGORIES
    }
    if not any(confirmed.values()):
        raise Discovery2Error("Confirm at least one meeting finding before generating Discovery 2.", "MEETING_FINDINGS_NONE_CONFIRMED")
    carried = carried_forward_items(paper)
    notes_text = personal_notes.get("text")
    meeting = _fixture_meeting_items(confirmed)
    if live:
        if complete is None:
            raise Discovery2Error("No AI model is configured for live Discovery 2 generation.", "DISCOVERY2_GENERATION_FAILED")
        meeting = _live_meeting_items(
            complete,
            paper=paper,
            transcript_text=str(sections_text),
            notes_text=notes_text,
            confirmed=confirmed,
            references=discovery1_reference_texts(paper),
        )
    sections = []
    for key in SECTION_KEYS:
        items = list(carried.get(key, [])) + list(meeting.get(key, []))
        sections.append({"key": key, "title": SECTION_TITLES[key], "items": items})
    client = str(opportunity.get("client_name") or (paper.get("presentation_brief") or {}).get("client_name") or "").strip()
    document = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "opportunity_id": str(opportunity["id"]),
        "document_id": document_id,
        "status": "ready",
        "generated_at": generated_at,
        "error": None,
        "generation": {"mode": "live" if live else "fixture", "generator_version": GENERATOR_VERSION},
        "title": f"Discovery 2 — {client}" if client else "Discovery 2",
        "client_name": client,
        "sources": {
            "discovery1": {
                "version_id": str(discovery1["id"]),
                "document_id": str(discovery1.get("document_id") or paper.get("document_id") or ""),
                "version_number": discovery1.get("version_number"),
                "approved_at": _iso(discovery1.get("approved_at")),
                "content_hash": discovery1_content_hash(paper),
            },
            "transcript": {
                "transcript_id": str(transcript["transcript_id"]),
                "revision": str(transcript["revision"]),
                "file_name": transcript.get("file_name"),
            },
            "personal_notes": {
                "updated_at": _iso(personal_notes.get("updated_at")),
                "sha256": sha256_text(notes_text.strip()) if isinstance(notes_text, str) and notes_text.strip() else None,
            },
            "meeting_extraction": {"generated_at": _iso(extraction.get("generated_at")), "execution_mode": extraction.get("execution_mode")},
            "meeting_review": {
                "confirmed_at": _iso(confirmation.get("confirmed_at")),
                "confirmed_finding_count": sum(len(items) for items in confirmed.values()),
            },
            "basis_hash": basis,
        },
        "summary": None,
        "sections": sections,
    }
    document["summary"] = summarize(document, previous=str((paper.get("presentation_brief") or {}).get("core_thesis") or paper["analysis"]["research"]["core_thesis"]["text"]))
    return validate_discovery2(document)


def summarize(document: dict[str, Any], *, previous: str) -> dict[str, str]:
    """Deterministic in both modes: counted from the items, so the summary can never claim more."""
    counts: list[str] = []
    for section in document["sections"]:
        fresh = [item for item in section["items"] if item["origin"] in {"meeting", "owner"}]
        if fresh and section["key"] in CATEGORIES:
            counts.append(f"{len(fresh)} {section['title'].lower()}")
    meeting = (
        "From the first meeting: " + ", ".join(counts) + ". Items from Discovery 1 are marked as carried forward."
        if counts
        else "The first meeting added no new items. Items from Discovery 1 are marked as carried forward."
    )
    return {"previous": previous, "meeting": meeting}


def _item(
    item_id: str,
    text: str,
    *,
    origin: str,
    status: str,
    source: str,
    evidence: str | None = None,
    discovery1_ref: str | None = None,
    edited: bool = False,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "text": text,
        "origin": origin,
        "status": status,
        "source": source,
        "evidence": evidence,
        "discovery1_ref": discovery1_ref,
        "edited": edited,
    }


def _fixture_meeting_items(confirmed: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Every confirmed finding, word for word, with its verified source."""
    items: dict[str, list[dict[str, Any]]] = {key: [] for key in SECTION_KEYS}
    for category in CATEGORIES:
        for item in confirmed.get(category, []):
            source = str(item.get("source"))
            if source not in MEETING_SOURCES:
                raise Discovery2Error("A confirmed finding has no verified source; analyse the meeting again.", "MEETING_EXTRACTION_STALE")
            text = str(item["text"]).strip()
            items[category].append(_item(f"m-{category}-{len(items[category]) + 1}", text, origin="meeting", status="new", source=source, evidence=text))
    return items


def _live_tool_schema() -> dict[str, Any]:
    item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["text", "status", "evidence", "discovery1_ref"],
        "properties": {
            "text": {"type": "string", "minLength": 1},
            "status": {"enum": ["new", "updated"]},
            "evidence": {"type": "string", "minLength": 1},
            "discovery1_ref": {"type": ["string", "null"]},
        },
    }
    keys = [*CATEGORIES, "open_questions"]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": keys,
        "properties": {key: {"type": "array", "items": item} for key in keys},
    }


def build_live_message(
    *,
    paper: dict[str, Any],
    transcript_text: str,
    notes_text: str | None,
    confirmed: dict[str, list[dict[str, Any]]],
    references: dict[str, str],
) -> str:
    """The four sources, labelled and kept apart."""
    brief = paper.get("presentation_brief") or {}
    lines = [
        "DISCOVERY 1 (approved before the meeting):",
        f"Core thesis: {brief.get('core_thesis') or paper['analysis']['research']['core_thesis']['text']}",
        "Discovery 1 references (use a reference exactly as written when an item updates it):",
        *[f"- [{ref}] {text}" for ref, text in sorted(references.items())],
        "",
        "CONFIRMED MEETING FINDINGS (confirmed by the owner):",
        *[f"- {category}: {item['text']}" for category in CATEGORIES for item in confirmed.get(category, [])],
        "",
        "TRANSCRIPT:",
        transcript_text.strip() or "(none)",
        "",
        "OWNER PERSONAL NOTES:",
        (notes_text or "").strip() or "(none)",
    ]
    return "\n".join(lines)


def _live_meeting_items(
    complete: Complete,
    *,
    paper: dict[str, Any],
    transcript_text: str,
    notes_text: str | None,
    confirmed: dict[str, list[dict[str, Any]]],
    references: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    system = _PROMPT_PATH.read_text(encoding="utf-8")
    user = build_live_message(paper=paper, transcript_text=transcript_text, notes_text=notes_text, confirmed=confirmed, references=references)
    raw = complete(system, user, _live_tool_schema())
    if not isinstance(raw, dict):
        raise Discovery2Error("The AI model did not return a Discovery 2 document.", "DISCOVERY2_GENERATION_FAILED")
    transcript = _comparable(transcript_text)
    notes = _comparable(notes_text or "")
    items: dict[str, list[dict[str, Any]]] = {key: [] for key in SECTION_KEYS}
    seen: dict[str, set[str]] = {key: set() for key in SECTION_KEYS}

    def add(section: str, text: str, *, status: str, source: str, evidence: str, ref: str | None) -> None:
        key = _comparable(text).rstrip(".")
        if not key or key in seen[section]:
            return
        seen[section].add(key)
        items[section].append(
            _item(f"m-{section}-{len(items[section]) + 1}", text, origin="meeting", status=status, source=source, evidence=evidence, discovery1_ref=ref)
        )

    for section in [*CATEGORIES, "open_questions"]:
        for entry in raw.get(section) or []:
            if not isinstance(entry, dict):
                continue
            text = " ".join(str(entry.get("text") or "").split())
            evidence = str(entry.get("evidence") or "").strip()
            source = _evidence_source(evidence, text, transcript, notes) if evidence else None
            if not text or source is None:
                continue  # supported by neither the transcript nor the notes: dropped
            ref = entry.get("discovery1_ref")
            ref = ref if isinstance(ref, str) and ref in references else None
            status = "updated" if entry.get("status") == "updated" and ref else "new"
            add(section, text, status=status, source=source, evidence=evidence, ref=ref)
    # Every confirmed finding is part of Discovery 2, in its confirmed wording, unless the model
    # already carried the same statement.
    for category in CATEGORIES:
        for finding in confirmed.get(category, []):
            text = str(finding["text"]).strip()
            covered = any(
                _comparable(text) in {_comparable(item["text"]), _comparable(item["evidence"] or "")}
                for item in items[category]
            )
            if not covered:
                add(category, text, status="new", source=str(finding["source"]), evidence=text, ref=None)
    return items


# ----------------------------------------------------------------------------- editing


def apply_section_edits(document: dict[str, Any], sections: list[dict[str, Any]]) -> dict[str, Any]:
    """The owner's edit of the sections: changed wording, removed items and added items.

    ``sections`` lists, per section key, the items in their new order as ``{id, text}``; an item
    without an id is new. Origins never change: an edited meeting item stays a meeting item with
    ``edited`` set, so it is no longer presented as a word-for-word client statement.
    """
    if document.get("status") != "ready":
        raise Discovery2Error("Generate Discovery 2 before editing it.", "DISCOVERY2_NOT_READY")
    edited = copy.deepcopy(document)
    by_key = {section["key"]: section for section in edited["sections"]}
    seen_keys: set[str] = set()
    used_ids = {item["id"] for section in edited["sections"] for item in section["items"]}
    for change in sections:
        key = str(change.get("key"))
        if key not in by_key or key in seen_keys:
            raise Discovery2Error(f"Unknown or repeated Discovery 2 section: {key}")
        seen_keys.add(key)
        current = {item["id"]: item for item in by_key[key]["items"]}
        result: list[dict[str, Any]] = []
        for index, entry in enumerate(change.get("items") or []):
            text = " ".join(str(entry.get("text") or "").split())
            if not text:
                continue  # an emptied item is removed
            if len(text) > MAX_ITEM_CHARACTERS:
                raise Discovery2Error(f"An item in {SECTION_TITLES[key]} is longer than {MAX_ITEM_CHARACTERS} characters.")
            item_id = entry.get("id")
            if item_id:
                if item_id not in current:
                    raise Discovery2Error(f"Item {item_id} does not belong to {SECTION_TITLES[key]}.")
                item = copy.deepcopy(current[item_id])
                if text != item["text"]:
                    item["text"] = text
                    item["edited"] = True
                    if item["status"] == "carried_forward":
                        item["status"] = "updated"
                result.append(item)
            else:
                new_id = f"o-{key}-{index + 1}"
                suffix = 1
                while new_id in used_ids:
                    suffix += 1
                    new_id = f"o-{key}-{index + 1}-{suffix}"
                used_ids.add(new_id)
                result.append(_item(new_id, text, origin="owner", status="new", source="owner", edited=True))
        by_key[key]["items"] = result
    edited["summary"] = summarize(edited, previous=(edited.get("summary") or {}).get("previous") or "")
    return validate_discovery2(edited)


def has_meeting_content(document: dict[str, Any]) -> bool:
    return any(meeting_items(document).values())


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    text = value.isoformat() if hasattr(value, "isoformat") else str(value).strip()
    return text.replace("+00:00", "Z") or None


_SAFE_NAME_RE = re.compile(r"[^\w .()-]+", re.UNICODE)


def safe_file_name(value: str, fallback: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("-", value.replace("—", "-").replace("–", "-"))
    return re.sub(r"\s*-[\s-]*", " - ", cleaned).strip(" -.") or fallback
