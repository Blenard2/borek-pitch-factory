"""Post-meeting follow-up email: finalized sources, saved edits, review, confirmation, export.

Nothing in this module sends mail. A confirmed draft is a reviewed draft; the export is a file
the owner opens and sends from their own mail program after their own check.

For a Master Presentation opportunity the email is written from the package that was finalized:

    finalization snapshot   names the reviewed V2 version and carries its generation manifest
    frozen V2 snapshot      the confirmed findings that version was generated from, kept with the
                            generation job and identified by the manifest's snapshot checksum
    project statics         recipient, salutation and sender the owner entered

Later changes to the transcript, the notes, the Discovery draft or the findings do not reach the
email: they are not part of the finalized package. Earlier opportunities with a standalone
PPT #2 keep the transcript-based draft unchanged.

Every write names the draft revision it is based on and is refused when the draft has moved on,
so two people cannot overwrite each other. Editing a confirmed draft withdraws the confirmation.

Who wrote the text is recorded with the draft and shown to the owner:

    ai             the configured model wrote it from the confirmed findings and every sentence
                   passed the grounding checks (``services.followup.ai_writer``)
    fallback       AI writing is enabled, but the model was unavailable or its draft was rejected;
                   the rule-based writer was used, and the reason is kept
    deterministic  AI writing is not enabled in this environment (it needs
                   FOLLOWUP_AI_WRITER_ENABLED=true and AI_EXECUTION_MODE=live); the rule-based
                   writer was used
"""

from __future__ import annotations

import hashlib
import uuid
import logging
import os
import re
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, formataddr, make_msgid
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from app.config import settings
from app.services import deck_center
from app.services.api_errors import bad_request, conflict, not_found
from llm.claude.client import ClaudeClientError, claude_credentials_problem, sonnet_model, structured_complete
from services.followup import ai_writer
from services.followup.finalized import (
    LENGTHS,
    RENDERER_VERSION,
    WORD_CAPS,
    ContentDoesNotFit,
    NoConfirmedContent,
    announces_attachment,
    content_from_snapshot,
    render_finalized_lengths,
)
from services.followup.rendering import draft_has_placeholders, followup_content_word_count
from services.observability.llm_logger import run_logged_llm_call
from services.presentation.master_deck.plan import MANIFEST_KIND_V2, MASTER_MANIFEST_KINDS
from services.presentation.master_deck.v2 import snapshot_hash
from services.followup.attachments import (
    MAX_EMAIL_BYTES,
    MAX_EXTERNAL_ATTACHMENTS,
    ExternalAttachmentError,
    attachments_fit,
    encoded_size,
    unique_names,
    validate_external_attachment,
)

SOURCE_KIND_V2 = "master_presentation_v2"
REVIEW_CHECKS = ("recipients", "dates_and_owners", "supported_statements", "review_flags", "tone", "attachments")
# BAP-7: the four generated documents of the package, each pinned to the version the finalized
# Master Presentation V2 was built from. key -> (artifact, file format, product label).
# "pptx" and "pdf" keep naming the V2 files, as drafts stored before BAP-7 do.
ATTACHMENTS: dict[str, tuple[str, str, str]] = {
    "pptx": ("master_presentation_v2", "pptx", "V2"),
    "pdf": ("master_presentation_v2", "pdf", "V2"),
    "v1_pptx": ("master_presentation_v1", "pptx", "V1"),
    "v1_pdf": ("master_presentation_v1", "pdf", "V1"),
    "discovery1_docx": ("discovery_1", "docx", "Discovery 1"),
    "discovery2_docx": ("discovery_2", "docx", "Discovery 2"),
}
ATTACHMENT_KEYS = tuple(ATTACHMENTS)
# BAP-3: a file the owner uploaded is selected under "external:<attachment id>" - a stable id,
# never the file name.
EXTERNAL_PREFIX = "external:"


def is_external_key(key: str) -> bool:
    return isinstance(key, str) and key.startswith(EXTERNAL_PREFIX)


def external_key(attachment_id: Any) -> str:
    return f"{EXTERNAL_PREFIX}{attachment_id}"
# The generated text says "Please find attached the presentation, which we updated after our
# meeting": true only while a Master Presentation V2 file is attached, not for another document.
PRESENTATION_KEYS = ("pptx", "pdf")


def presentation_attached(selection: dict[str, Any] | None) -> bool:
    return any(bool((selection or {}).get(key)) for key in PRESENTATION_KEYS)
MEDIA_TYPES = {
    "pptx": ("application", "vnd.openxmlformats-officedocument.presentationml.presentation"),
    "pdf": ("application", "pdf"),
    "docx": ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document"),
}
MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 20000
WRITER_AI = "ai"
WRITER_FALLBACK = "fallback"
WRITER_RULES = "deterministic"
FLAG_AI_WRITTEN = "ai_written"
FLAG_AI_FALLBACK = "ai_fallback"
FLAG_CONTENT_TRIMMED = "content_trimmed"
AI_MAX_TOKENS = 8000
VARIANTS = (("attached", True), ("plain", False))

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text.replace("+00:00", "Z")


def revision_of(stored: dict[str, Any]) -> int:
    """A draft written before revisions existed is revision 1."""
    return int(stored.get("revision") or 1)


# ----------------------------------------------------------------------------- finalized sources


def _is_master_journey(store: Any, *, opportunity_id: UUID, user_id: UUID) -> bool:
    for row in store.list_presentation_versions_for_opportunity(opportunity_id=opportunity_id, user_id=user_id):
        manifest = row.get("generation_source_manifest")
        if isinstance(manifest, dict) and manifest.get("kind") in MASTER_MANIFEST_KINDS:
            return True
    return False


def _unavailable(message: str) -> HTTPException:
    return conflict("FOLLOWUP_SOURCE_UNAVAILABLE", message)


def resolve_finalized_source(store: Any, *, opportunity: dict[str, Any], opportunity_id: UUID, user_id: UUID) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The reviewed V2 version of a finalized Master Presentation package and its frozen sources.

    Returns None for an opportunity that never had a Master Presentation (standalone PPT #2).
    """
    package = opportunity.get("finalization_snapshot")
    finalized = bool(opportunity.get("finalized_at")) and isinstance(package, dict)
    manifest = package.get("ppt2_generation_source_manifest") if finalized else None
    if not finalized or not isinstance(manifest, dict) or manifest.get("kind") != MANIFEST_KIND_V2:
        if finalized and isinstance(manifest, dict):
            return None  # finalized with a standalone PPT #2
        if _is_master_journey(store, opportunity_id=opportunity_id, user_id=user_id):
            raise bad_request(
                "FOLLOWUP_FINALIZATION_REQUIRED",
                "Review and finalize Master Presentation V2 before preparing the follow-up email.",
            )
        return None
    version_id = str(package["ppt2_version_id"])
    try:
        version = store.get_presentation_version(presentation_version_id=UUID(version_id), user_id=user_id)
    except HTTPException as exc:
        raise _unavailable("The finalized presentation version is no longer available.") from exc
    stored_manifest = version.get("generation_source_manifest")
    status = version.get("status")
    if (
        str(getattr(status, "value", status)) != "ready"
        or not isinstance(stored_manifest, dict)
        or stored_manifest.get("snapshot_hash") != manifest.get("snapshot_hash")
        or str(version.get("presentation_id")) != str(package["ppt2_presentation_id"])
    ):
        raise _unavailable("The finalized presentation version no longer matches the finalized package.")
    frozen = None
    for job in store.list_generation_jobs_for_opportunity(opportunity_id):
        result = job.get("result_json") if isinstance(job.get("result_json"), dict) else {}
        candidate = (result.get("_enqueue") or {}).get("master_v2_snapshot")
        if isinstance(candidate, dict) and snapshot_hash(candidate) == manifest.get("snapshot_hash"):
            frozen = candidate
            break
    if frozen is None or str(frozen.get("opportunity_id")) != str(opportunity_id):
        # Without the frozen findings the email could only be written from live state. It is not.
        raise _unavailable("The confirmed findings of the finalized presentation could not be recovered.")
    source = {
        "kind": SOURCE_KIND_V2,
        "product_version": "V2",
        "finalized_at": str(package["captured_at"]),
        "presentation_id": str(package["ppt2_presentation_id"]),
        "presentation_version_id": version_id,
        "version_number": int(version.get("version_number") or 0),
        "snapshot_hash": str(manifest["snapshot_hash"]),
        "generation_fingerprint": manifest.get("generation_fingerprint"),
        "approved_discovery_version_id": manifest.get("approved_discovery_version_id"),
        "approved_discovery2_version_id": manifest.get("approved_discovery2_version_id"),
        "base_presentation_version_id": manifest.get("base_presentation_version_id"),
        "transcript_id": manifest.get("transcript_id"),
        "transcript_revision": manifest.get("transcript_revision"),
        "meeting_review_confirmed_at": manifest.get("meeting_review_confirmed_at"),
        "extraction_execution_mode": manifest.get("meeting_extraction_execution_mode"),
        "confirmed_finding_count": manifest.get("confirmed_finding_count"),
        "excluded_finding_count": manifest.get("excluded_finding_count"),
        "renderer_version": RENDERER_VERSION,
    }
    return source, frozen


def require_complete_statics(statics: Any) -> dict[str, Any]:
    """Recipient, salutation and sender must be entered by the owner; nothing is guessed."""
    if not isinstance(statics, dict) or not statics.get("project_name"):
        raise bad_request(
            "FOLLOWUP_STATICS_REQUIRED",
            "Enter the project name, recipient and sender before generating the follow-up email.",
        )
    recipients = [item for item in statics.get("standard_recipients") or [] if isinstance(item, dict)]
    primary = next((item for item in recipients if item.get("kind") == "to" and item.get("primary")), recipients[0] if recipients else None)
    sender = statics.get("sender_profile") or {}
    formal = statics.get("salutation_style") == "formal"
    named = primary is not None and (
        (primary.get("salutation") and primary.get("last_name")) if formal else primary.get("first_name")
    )
    if not named or not sender.get("name") or not sender.get("email"):
        raise bad_request(
            "FOLLOWUP_STATICS_INCOMPLETE",
            "The recipient needs a first name (informal) or a salutation and last name (formal), and the sender a name and address.",
        )
    return statics


def ai_writing_enabled() -> bool:
    """AI writing is switched on explicitly, and only where live AI calls are allowed at all.

    Without FOLLOWUP_AI_WRITER_ENABLED the rule-based writer writes every draft, whatever the
    environment's AI mode: a live environment does not start writing emails with a model by
    itself. Fixture mode never calls a model, whatever the switch says.
    """
    return bool(settings.FOLLOWUP_AI_WRITER_ENABLED) and settings.AI_EXECUTION_MODE == "live"


# Fingerprints of keys Anthropic has refused in this process. A refused key is not tried again:
# it will be refused again, and every attempt is a request with client findings in it.
_REFUSED_KEYS: set[str] = set()


def _key_fingerprint() -> str:
    return hashlib.sha256(os.environ.get("ANTHROPIC_API_KEY", "").strip().encode("utf-8")).hexdigest()


def model_preflight() -> tuple[str, str] | None:
    """Why no model request should be made right now, or None. Contacts nobody.

    Missing, placeholder and malformed keys are recognised locally; a key the provider refused
    earlier in this process is remembered until a different key is configured.
    """
    problem = claude_credentials_problem()
    if problem:
        return problem
    if _key_fingerprint() in _REFUSED_KEYS:
        return ("CLAUDE_AUTH", "The configured ANTHROPIC_API_KEY was rejected earlier, so no model was called. Configure a valid key.")
    return None


def _model_call(*, stage: str, prompt_version: str, tool_name: str, description: str, opportunity_id: UUID) -> ai_writer.Complete:
    """One logged call through the shared Claude client (egress policy and generation log included).

    Nothing is sent when the preflight already knows the request cannot succeed.
    """

    def complete(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        problem = model_preflight()
        if problem:
            raise ClaudeClientError(problem[1], code=problem[0], retryable=False)

        def invoke() -> dict[str, Any]:
            return structured_complete(system, user, schema, tool_name=tool_name, tool_description=description, max_tokens=AI_MAX_TOKENS)

        try:
            return run_logged_llm_call(
                stage=stage, prompt_version=prompt_version, model=sonnet_model(), attempt=1, opportunity_id=str(opportunity_id), invoke=invoke
            )
        except ClaudeClientError as exc:
            if getattr(exc, "code", "") == "CLAUDE_AUTH":
                _REFUSED_KEYS.add(_key_fingerprint())
            raise

    return complete


def write_variants(
    content: dict[str, Any],
    statics: dict[str, Any],
    *,
    live: bool,
    opportunity_id: UUID,
    writer: ai_writer.Complete | None = None,
    verifier: ai_writer.Complete | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Both texts of the email - with and without the attachment sentence - and who wrote them.

    The rule-based drafts are always written first: they are what the owner gets when AI writing
    is off, when the model cannot be reached and when its draft does not pass the grounding
    checks. A draft that did not pass is never stored, shown or mixed into the fallback.
    """
    rules: dict[str, dict[str, Any]] = {}
    for variant, attached in VARIANTS:
        lengths, flags = render_finalized_lengths(content, statics, attachment=attached)
        rules[variant] = {"lengths": lengths, "review_flags": list(flags)}
    if not live:
        return rules, {"mode": WRITER_RULES, "model": None, "prompt_version": RENDERER_VERSION, "verified": False, "fallback_reason": None, "verification": None}
    model = sonnet_model()
    try:
        result = ai_writer.write_with_ai(
            content,
            statics,
            writer=writer or _model_call(
                stage=ai_writer.STAGE_WRITER, prompt_version=ai_writer.WRITER_PROMPT_VERSION, tool_name="submit_followup_email",
                description="Submit the short, medium and extensive email bodies as paragraphs of sentences with the fact ids each sentence rests on.",
                opportunity_id=opportunity_id,
            ),
            verifier=verifier or _model_call(
                stage=ai_writer.STAGE_VERIFIER, prompt_version=ai_writer.VERIFIER_PROMPT_VERSION, tool_name="submit_grounding_verdicts",
                description="Submit one verdict per sentence: supported by its cited facts or not.",
                opportunity_id=opportunity_id,
            ),
        )
    except ai_writer.AiWriterRejected as exc:
        reason = {"code": exc.code, "message": str(exc)}
        issues = [{key: str(item.get(key) or "")[:300] for key in ("length", "code", "sentence", "detail")} for item in exc.issues[:20]]
    except ClaudeClientError as exc:
        reason = {"code": str(getattr(exc, "code", "") or "AI_UNAVAILABLE"), "message": exc.user_message}
        issues = []
    except Exception:  # noqa: BLE001 - whatever goes wrong in the AI stage, the owner still gets a draft
        logger.exception("Follow-up email AI writer failed for opportunity %s", opportunity_id)
        reason = {"code": "AI_WRITER_FAILED", "message": "The AI writer failed unexpectedly."}
        issues = []
    else:
        subject = rules["attached"]["lengths"]["short"]["subject"]
        # What the rule-based writer left out says nothing about this text.
        flags = [flag for flag in content["review_flags"] if flag != FLAG_CONTENT_TRIMMED] + [FLAG_AI_WRITTEN]
        variants = {
            variant: {
                "lengths": {
                    name: {"subject": subject, "body": body, "word_count": max(1, followup_content_word_count(body)), "edited": False}
                    for name, body in result["variants"][variant].items()
                },
                "review_flags": list(flags),
            }
            for variant, _attached in VARIANTS
        }
        return variants, {
            "mode": WRITER_AI, "model": model, "prompt_version": ai_writer.WRITER_PROMPT_VERSION, "verified": True, "fallback_reason": None,
            "verification": result["verification"],
        }
    for variant in rules.values():
        variant["review_flags"].append(FLAG_AI_FALLBACK)
    return rules, {
        "mode": WRITER_FALLBACK, "model": model, "prompt_version": RENDERER_VERSION, "verified": False, "fallback_reason": reason,
        "verification": {"rejected": issues} if issues else None,
    }


def public_writer(stored: dict[str, Any]) -> dict[str, Any] | None:
    """Who wrote the draft, for the owner. Sentence-level records stay with the stored draft."""
    writer = stored.get("writer")
    if not isinstance(writer, dict) or writer.get("mode") not in (WRITER_AI, WRITER_FALLBACK, WRITER_RULES):
        return None
    verification = writer.get("verification") if isinstance(writer.get("verification"), dict) else {}
    reason = writer.get("fallback_reason") if isinstance(writer.get("fallback_reason"), dict) else None
    return {
        "mode": writer["mode"],
        "model": writer.get("model"),
        "prompt_version": str(writer.get("prompt_version") or ""),
        "verified": bool(writer.get("verified")),
        "fallback_reason": {"code": str(reason.get("code") or ""), "message": str(reason.get("message") or "")} if reason else None,
        "checked_sentences": int(verification["sentence_count"]) if writer["mode"] == WRITER_AI and "sentence_count" in verification else None,
    }


DEFAULT_ATTACHMENT_SELECTION = {key: key == "pdf" for key in ATTACHMENT_KEYS}


def attachment_selection(existing: dict[str, Any] | None, source: dict[str, Any]) -> dict[str, bool]:
    """The owner's selection for this presentation version, or the default for a new one."""
    previous = (existing or {}).get("source") or {}
    stored = (existing or {}).get("attachment_selection") or {}
    # Uploaded files belong to the draft, not to a presentation version: their selection stays.
    uploaded = {key: bool(value) for key, value in stored.items() if is_external_key(key)}
    if previous.get("presentation_version_id") == source["presentation_version_id"]:
        # A selection saved before BAP-7 names the V2 files only; the other documents start unselected.
        return {**{key: bool(stored.get(key, False)) for key in ATTACHMENT_KEYS}, **uploaded}
    return {**DEFAULT_ATTACHMENT_SELECTION, **uploaded}


def master_generation(
    store: Any,
    *,
    opportunity: dict[str, Any],
    opportunity_id: UUID,
    user_id: UUID,
    journey_stage: str = "deepening",
    attachment: bool | None = None,
    live: bool | None = None,
    writer: ai_writer.Complete | None = None,
    verifier: ai_writer.Complete | None = None,
) -> dict[str, Any] | None:
    """Draft content from the finalized V2 package, or None for a standalone PPT #2 opportunity.

    The text only says that the presentation is attached when a file is selected: ``attachment``,
    or the selection the stored draft holds for this presentation version.
    """
    resolved = resolve_finalized_source(store, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id)
    if resolved is None:
        return None
    source, frozen = resolved
    statics = require_complete_statics(opportunity.get("followup_statics"))
    if attachment is None:
        existing = store.get_email_draft(opportunity_id=opportunity_id, user_id=user_id, journey_stage=journey_stage)
        attachment = presentation_attached(attachment_selection(existing, source))
    try:
        content = content_from_snapshot(frozen)
        variants, written_by = write_variants(
            content, statics, live=ai_writing_enabled() if live is None else live, opportunity_id=opportunity_id, writer=writer, verifier=verifier
        )
    except (NoConfirmedContent, ContentDoesNotFit) as exc:
        raise bad_request(exc.code, str(exc)) from exc
    over = [name for variant in variants.values() for name in LENGTHS if variant["lengths"][name]["word_count"] > WORD_CAPS[name]]
    if over:  # both writers guarantee this; a draft over its cap is never stored
        raise bad_request("FOLLOWUP_CONTENT_TOO_LONG", f"The generated {over[0]} draft exceeds its word limit.")
    chosen = variants["attached" if attachment else "plain"]
    return {"lengths": chosen["lengths"], "review_flags": chosen["review_flags"], "source": source, "writer": written_by, "variants": variants}


# ----------------------------------------------------------------------------- stored draft


def _recipients(statics: Any) -> dict[str, Any] | None:
    if not isinstance(statics, dict):
        return None
    sender = statics.get("sender_profile") or {}
    people: dict[str, list[dict[str, Any]]] = {"to": [], "cc": []}
    ordered = sorted(statics.get("standard_recipients") or [], key=lambda item: not item.get("primary"))
    for item in ordered:
        name = " ".join(part for part in (item.get("first_name"), item.get("last_name")) if part)
        people["cc" if item.get("kind") == "cc" else "to"].append({"name": name or None, "email": str(item.get("email") or "").strip()})
    if not people["to"] or not sender.get("email"):
        return None
    return {
        **people,
        "sender": {"name": str(sender.get("name") or "").strip(), "role": sender.get("role"), "email": str(sender.get("email")).strip()},
    }


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^\w .()-]+", "-", value.replace("—", "-").replace("–", "-"), flags=re.UNICODE)
    return re.sub(r"\s*-[\s-]*", " - ", cleaned).strip(" -.") or "Master Presentation"


def attachments_for(
    store: Any, *, stored: dict[str, Any], user_id: UUID, opportunity_id: UUID, read_uploads: bool = False
) -> list[dict[str, Any]]:
    """The generated documents and the uploaded files of the draft, with the owner's selection.

    BAP-3: after the six generated files come the files the owner uploaded for this email, in
    upload order. ``read_uploads`` reads the stored bytes of the selected uploads (confirmation and
    export): a file whose bytes cannot be read is reported as unavailable.

    Generated files - the four generated documents of the finalized package:

    Discovery 1, Discovery 2, Master Presentation V1 and V2 - each exactly the version the
    finalized V2 was built from, never a newer one. A document that cannot be resolved for this
    opportunity is listed as unavailable. Files are read (or, for Discovery, rendered) only from
    those pinned versions.
    """
    source = stored.get("source")
    if not isinstance(source, dict) or source.get("kind") != SOURCE_KIND_V2:
        return []
    selection = stored.get("attachment_selection") or {}
    presentation_id = UUID(str(source["presentation_id"]))
    try:
        title = str(store.get_presentation(presentation_id=presentation_id, user_id=user_id).get("name") or "")
    except HTTPException:
        title = ""
    presentation_name = _safe_name(title or "Master Presentation")
    pinned = {
        "master_presentation_v2": source.get("presentation_version_id"),
        "master_presentation_v1": source.get("base_presentation_version_id"),
        "discovery_1": source.get("approved_discovery_version_id"),
        "discovery_2": source.get("approved_discovery2_version_id"),
    }
    items = []
    for key, (artifact, kind, label) in ATTACHMENTS.items():
        version_id = pinned[artifact]
        item: dict[str, Any] = {
            "key": key,
            "artifact": artifact,
            "format": kind,
            "file_name": f"{presentation_name} - {label}.{kind}",
            "selected": bool(selection.get(key)),
            "available": False,
            "size_bytes": None,
            "presentation_id": str(presentation_id) if artifact.startswith("master_") else None,
            "presentation_version_id": None,
            "artifact_version_id": str(version_id) if version_id else None,
            "version_number": source.get("version_number") if artifact == "master_presentation_v2" else None,
            "product_version": label,
            "download_path": None,
            "origin": "generated",
            "uploaded_at": None,
        }
        if version_id:
            try:
                if artifact.startswith("master_"):
                    _presentation_file(store, item, presentation_id=presentation_id, version_id=UUID(str(version_id)), kind=kind, user_id=user_id)
                else:
                    _discovery_file(store, item, artifact=artifact, version_id=UUID(str(version_id)), opportunity_id=opportunity_id, user_id=user_id)
            except (HTTPException, OSError, ValueError, KeyError):
                item["available"] = False
                item.pop("_path", None)
                item.pop("_bytes", None)
        items.append(item)
    items.extend(_uploaded_items(store, stored=stored, user_id=user_id, opportunity_id=opportunity_id, read=read_uploads))
    return items


def _uploaded_items(store: Any, *, stored: dict[str, Any], user_id: UUID, opportunity_id: UUID, read: bool) -> list[dict[str, Any]]:
    selection = stored.get("attachment_selection") or {}
    items = []
    for row in store.list_email_attachments(opportunity_id=opportunity_id, user_id=user_id, journey_stage=stored["journey_stage"]):
        key = external_key(row["id"])
        item: dict[str, Any] = {
            "key": key,
            "artifact": "external",
            "format": str(row["extension"]).removeprefix("."),
            "file_name": str(row["file_name"]),
            "selected": bool(selection.get(key)),
            "available": True,
            "size_bytes": int(row["size_bytes"]),
            "presentation_id": None,
            "presentation_version_id": None,
            "artifact_version_id": str(row["id"]),
            "version_number": None,
            "product_version": "Uploaded",
            "download_path": f"/opportunities/{opportunity_id}/email-drafts/{stored['id']}/attachments/{row['id']}/download",
            "origin": "external",
            "uploaded_at": _iso(row.get("created_at")),
            "_mime": str(row["mime_type"]),
            "_sha256": str(row["sha256"]),
        }
        if read and item["selected"]:
            try:
                data = store.read_email_attachment_content(opportunity_id=opportunity_id, user_id=user_id, attachment_id=row["id"])
                if hashlib.sha256(data).hexdigest() != item["_sha256"]:
                    raise ValueError("stored bytes do not match the upload")
                item["_bytes"] = data
            except (HTTPException, ValueError, OSError):
                item["available"] = False
        items.append(item)
    return items


def selected_sizes(files: list[dict[str, Any]]) -> list[int]:
    return [int(item.get("size_bytes") or 0) for item in files if item["selected"]]


def _too_large(estimate: int) -> Any:
    return bad_request(
        "EMAIL_ATTACHMENTS_TOO_LARGE",
        f"With the selected attachments the email would be about {estimate / (1024 * 1024):.1f} MB once encoded; "
        f"it can be at most {MAX_EMAIL_BYTES // (1024 * 1024)} MB. Deselect a file.",
    )


def _require_total_size(files: list[dict[str, Any]], extra: int | None = None) -> None:
    """The 25 MB policy applies to the email, so attachments count with their encoded size."""
    sizes = selected_sizes(files) + ([extra] if extra is not None else [])
    if not attachments_fit(sizes):
        raise _too_large(sum(encoded_size(size) for size in sizes))


def _presentation_file(store: Any, item: dict[str, Any], *, presentation_id: UUID, version_id: UUID, kind: str, user_id: UUID) -> None:
    item["presentation_version_id"] = str(version_id)
    if item["artifact"] == "master_presentation_v1":
        version = store.get_presentation_version(presentation_version_id=version_id, user_id=user_id)
        if str(version.get("presentation_id")) != str(presentation_id):
            raise ValueError("The V1 version belongs to another presentation")
        item["version_number"] = version.get("version_number")
    path = deck_center.resolve_deck_file_path(store, presentation_id=presentation_id, user_id=user_id, kind=kind, presentation_version_id=version_id)
    item["available"] = True
    item["size_bytes"] = path.stat().st_size
    item["download_path"] = f"/presentations/{presentation_id}/versions/{version_id}/download/{kind}"
    item["_path"] = path


def _discovery_file(store: Any, item: dict[str, Any], *, artifact: str, version_id: UUID, opportunity_id: UUID, user_id: UUID) -> None:
    from app.services.post_meeting_discovery import discovery1_docx, discovery2_docx

    if artifact == "discovery_1":
        exported = discovery1_docx(store, opportunity_id=opportunity_id, version_id=version_id, user_id=user_id)
        row = store.get_discovery_paper_version(version_id=version_id, user_id=user_id)
        item["download_path"] = f"/opportunities/{opportunity_id}/discovery-paper/versions/{version_id}/download/docx"
    else:
        exported = discovery2_docx(store, opportunity_id=opportunity_id, version_id=version_id, user_id=user_id)
        row = store.get_discovery2_version(version_id=version_id, user_id=user_id)
        item["download_path"] = f"/opportunities/{opportunity_id}/post-meeting-discovery/versions/{version_id}/download/docx"
    if str(row.get("status")) != "approved":
        raise ValueError("Only an approved Discovery version is attached")
    item["version_number"] = row.get("version_number")
    item["file_name"] = exported["file_name"]
    item["available"] = True
    item["size_bytes"] = len(exported["content"])
    item["_bytes"] = exported["content"]


def source_status(store: Any, *, stored: dict[str, Any], opportunity: dict[str, Any], opportunity_id: UUID, user_id: UUID) -> str:
    source = stored.get("source")
    if not isinstance(source, dict) or source.get("kind") != SOURCE_KIND_V2:
        return "not_applicable"
    try:
        resolved = resolve_finalized_source(store, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id)
    except HTTPException:
        return "changed"
    if resolved is None:
        return "changed"
    current = resolved[0]
    same = all(current[key] == source.get(key) for key in ("presentation_id", "presentation_version_id", "snapshot_hash"))
    return "valid" if same else "changed"


def public_fields(store: Any, *, stored: dict[str, Any], opportunity: dict[str, Any], opportunity_id: UUID, user_id: UUID) -> dict[str, Any]:
    """Everything the envelope shows beyond the three texts."""
    confirmed = stored.get("status") == "confirmed"
    attachments = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in attachments_for(store, stored=stored, user_id=user_id, opportunity_id=opportunity_id)
    ]
    return {
        "revision": revision_of(stored),
        "confirmed_revision": stored.get("confirmed_revision") if confirmed else None,
        "confirmed_by": str(stored["confirmed_by"]) if confirmed and stored.get("confirmed_by") else None,
        "source": stored.get("source"),
        "source_status": source_status(store, stored=stored, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id),
        "review_flags": list(stored.get("review_flags") or []),
        "review": stored.get("review") if confirmed else None,
        "recipients": (stored.get("recipients") if confirmed else None) or _recipients(opportunity.get("followup_statics")),
        "attachments": attachments,
        "word_limits": dict(WORD_CAPS),
        "writer": public_writer(stored),
    }


def envelope(store: Any, *, opportunity_id: UUID, user_id: UUID, journey_stage: str, stored: dict[str, Any] | None, opportunity: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.services.journey_generation import envelope_from_stored

    if stored is None:
        return envelope_from_stored(opportunity_id, journey_stage, None)
    opportunity = opportunity or store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    extra = public_fields(store, stored=stored, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id)
    return envelope_from_stored(opportunity_id, journey_stage, stored, extra=extra)


# ----------------------------------------------------------------------------- generate


def has_owner_work(stored: dict[str, Any] | None) -> bool:
    """Edited text, or a confirmed draft: regenerating would discard something the owner did."""
    if not stored:
        return False
    edited = any(bool((stored.get("lengths") or {}).get(name, {}).get("edited")) for name in LENGTHS)
    return edited or (stored.get("status") == "confirmed" and isinstance(stored.get("source"), dict))


def existing_draft_for_generation(store: Any, *, opportunity_id: UUID, user_id: UUID, journey_stage: str, overwrite_edits: bool) -> dict[str, Any] | None:
    """The stored draft a generation would replace. Refused while it holds the owner's work.

    Checked before anything is generated, so that no model is called for a draft that will not
    be stored.
    """
    existing = store.get_email_draft(opportunity_id=opportunity_id, user_id=user_id, journey_stage=journey_stage)
    if has_owner_work(existing) and not overwrite_edits:
        raise conflict(
            "EMAIL_DRAFT_HAS_EDITS",
            "This draft contains saved edits or a confirmation. Regenerating replaces all three lengths.",
        )
    return existing


def store_generated(
    store: Any,
    *,
    opportunity: dict[str, Any],
    opportunity_id: UUID,
    user_id: UUID,
    journey_stage: str,
    lengths: dict[str, dict[str, Any]],
    source: dict[str, Any] | None,
    review_flags: list[str],
    overwrite_edits: bool,
    writer: dict[str, Any] | None = None,
    variants: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = existing_draft_for_generation(store, opportunity_id=opportunity_id, user_id=user_id, journey_stage=journey_stage, overwrite_edits=overwrite_edits)
    selection = attachment_selection(existing, source) if source is not None else {}
    stored = store.save_email_draft(
        opportunity_id=opportunity_id,
        user_id=user_id,
        journey_stage=journey_stage,
        draft={
            "status": "draft",
            "selected_length": existing.get("selected_length") if existing else None,
            "lengths": {name: {**lengths[name], "edited": False} for name in LENGTHS},
            "confirmed_at": None,
            "confirmed_revision": None,
            "confirmed_by": None,
            "source": source,
            "review_flags": list(review_flags),
            "attachment_selection": selection,
            "review": None,
            "recipients": None,
            # Who wrote the text, and both generated texts (with and without the attachment
            # sentence), so that a change of the selection needs no new generation.
            "writer": writer,
            "writer_variants": variants,
        },
        expected_revision=None if existing is None else revision_of(existing),
    )
    return envelope(store, opportunity_id=opportunity_id, user_id=user_id, journey_stage=journey_stage, stored=stored)


# ----------------------------------------------------------------------------- edit


def _checked_text(name: str, subject: Any, body: Any) -> dict[str, Any]:
    raw = str(subject or "")
    subject = None if "\n" in raw or "\r" in raw else " ".join(raw.split())
    body = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not subject or len(subject) > MAX_SUBJECT_CHARS:
        raise bad_request("EMAIL_SUBJECT_INVALID", f"The subject must be one line of 1 to {MAX_SUBJECT_CHARS} characters.")
    if not body or len(body) > MAX_BODY_CHARS:
        raise bad_request("EMAIL_BODY_INVALID", f"The message must contain text and stay under {MAX_BODY_CHARS} characters.")
    if draft_has_placeholders(subject) or draft_has_placeholders(body):
        raise bad_request("EMAIL_PLACEHOLDER_UNRESOLVED", "The email still contains a template placeholder such as {{name}}.")
    words = followup_content_word_count(body)
    if words > WORD_CAPS[name]:
        raise bad_request(
            "EMAIL_LENGTH_EXCEEDED",
            f"The {name} draft has {words} content words; the limit is {WORD_CAPS[name]}. Shorten it or use a longer draft.",
        )
    return {"subject": subject, "body": body, "word_count": max(1, words)}


def _require_revision(stored: dict[str, Any], expected_revision: int) -> None:
    if revision_of(stored) != expected_revision:
        raise conflict("EMAIL_DRAFT_CONFLICT", "The email draft was changed in the meantime. Reload it before saving.")


def _carry(stored: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "status", "selected_length", "lengths", "confirmed_at", "confirmed_revision", "confirmed_by", "source", "review_flags", "attachment_selection",
        "review", "recipients", "writer", "writer_variants",
    )
    return {key: stored.get(key) for key in keys}


def update_draft(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    draft_id: UUID,
    expected_revision: int,
    selected_length: str | None,
    lengths: dict[str, dict[str, Any]] | None,
    attachments: dict[str, bool] | None,
) -> dict[str, Any]:
    """Save edited texts, the chosen length and the attachment selection of one draft revision."""
    stored = store.get_email_draft_by_id(opportunity_id=opportunity_id, user_id=user_id, draft_id=draft_id)
    journey_stage = stored["journey_stage"]
    _require_revision(stored, expected_revision)
    draft = _carry(stored)
    draft["lengths"] = {name: dict(stored["lengths"][name]) for name in LENGTHS}
    changed = False
    for name, text in (lengths or {}).items():
        if name not in LENGTHS:
            raise bad_request("INVALID_EMAIL_LENGTH", "lengths may contain short, medium and extensive only")
        checked = _checked_text(name, text.get("subject"), text.get("body"))
        current = draft["lengths"][name]
        if checked["subject"] != current["subject"] or checked["body"] != str(current["body"]).strip():
            draft["lengths"][name] = {**checked, "edited": True}
            changed = True
    if selected_length is not None and selected_length != draft.get("selected_length"):
        draft["selected_length"] = selected_length
        changed = True
    if attachments is not None:
        master = isinstance(stored.get("source"), dict) and stored["source"].get("kind") == SOURCE_KIND_V2
        if not master and any(attachments.values()):
            raise bad_request("EMAIL_ATTACHMENT_UNAVAILABLE", "This draft has no approved presentation version to attach.")
        uploads = (
            {external_key(row["id"]) for row in store.list_email_attachments(opportunity_id=opportunity_id, user_id=user_id, journey_stage=journey_stage)}
            if master
            else set()
        )
        unknown = sorted(set(attachments) - set(ATTACHMENT_KEYS) - uploads)
        if unknown:
            raise bad_request("INVALID_EMAIL_ATTACHMENT", f"Unknown attachment: {', '.join(unknown)}.")
        previous = stored.get("attachment_selection") or {}
        selection = (
            {
                key: bool(attachments[key]) if attachments.get(key) is not None else bool(previous.get(key, False))
                for key in (*ATTACHMENT_KEYS, *sorted(uploads))
            }
            if master
            else {}
        )
        if master:
            _require_total_size(attachments_for(store, stored={**stored, "attachment_selection": selection}, user_id=user_id, opportunity_id=opportunity_id))
        if selection != (stored.get("attachment_selection") or {}):
            was_attached = presentation_attached(stored.get("attachment_selection"))
            draft["attachment_selection"] = selection
            changed = True
            if master and presentation_attached(selection) != was_attached:
                _follow_attachment_selection(store, draft=draft, stored=stored, opportunity_id=opportunity_id, user_id=user_id, attached=presentation_attached(selection))
    if not changed:
        return envelope(store, opportunity_id=opportunity_id, user_id=user_id, journey_stage=journey_stage, stored=stored)
    # What was reviewed no longer exists in this form: the confirmation is withdrawn.
    draft.update({"status": "draft", "confirmed_at": None, "confirmed_revision": None, "confirmed_by": None, "review": None, "recipients": None})
    saved = store.save_email_draft(
        opportunity_id=opportunity_id, user_id=user_id, journey_stage=journey_stage, draft=draft, expected_revision=expected_revision
    )
    return envelope(store, opportunity_id=opportunity_id, user_id=user_id, journey_stage=journey_stage, stored=saved)


def _follow_attachment_selection(store: Any, *, draft: dict[str, Any], stored: dict[str, Any], opportunity_id: UUID, user_id: UUID, attached: bool) -> None:
    """Keep the generated text true to the selection: it only says "attached" when a file is.

    Lengths the owner has not edited take the text that was generated for this selection, with
    or without the attachment sentence. An edited length is the owner's text and is never changed
    here; a message that still announces a file that is not selected cannot be confirmed until
    the owner resolves it.
    """
    variants = stored.get("writer_variants")
    if isinstance(variants, dict) and isinstance(variants.get("attached" if attached else "plain"), dict):
        # The text generated for this selection, kept since the draft was written. No model is called.
        chosen = variants["attached" if attached else "plain"]
        fresh = {"lengths": chosen["lengths"], "review_flags": chosen["review_flags"]}
    else:
        # A draft from before both texts were kept: it was written by the rule-based writer.
        opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
        if source_status(store, stored=stored, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id) != "valid":
            return
        try:
            fresh = master_generation(
                store, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id, journey_stage=stored["journey_stage"], attachment=attached, live=False
            )
        except HTTPException:
            return
        if fresh is None:
            return
    for name in LENGTHS:
        if not draft["lengths"][name].get("edited"):
            draft["lengths"][name] = {**fresh["lengths"][name], "edited": False}
    # Whether confirmed points were left out can change with the room the sentence takes. The
    # flag describes the generated extensive draft; an edited one keeps the flag it had.
    if not draft["lengths"]["extensive"].get("edited"):
        draft["review_flags"] = list(fresh["review_flags"])


# ----------------------------------------------------------------------------- confirm


def confirm_draft(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    draft_id: UUID,
    selected_length: str,
    expected_revision: int | None,
    review_checks: list[str] | None,
    acknowledged_flags: list[str] | None,
) -> dict[str, Any]:
    """Record the owner's review of one saved draft revision. Never sends anything."""
    if selected_length not in LENGTHS:
        raise bad_request("INVALID_EMAIL_LENGTH", "selected_length must be short, medium, or extensive")
    stored = store.get_email_draft_by_id(opportunity_id=opportunity_id, user_id=user_id, draft_id=draft_id)
    journey_stage = stored["journey_stage"]
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    master = isinstance(stored.get("source"), dict) and stored["source"].get("kind") == SOURCE_KIND_V2
    if expected_revision is None:
        if master:
            raise bad_request("EMAIL_REVISION_REQUIRED", "Name the saved draft revision that was reviewed.")
        expected_revision = revision_of(stored)
    _require_revision(stored, expected_revision)
    review = None
    recipients = _recipients(opportunity.get("followup_statics"))
    if master:
        missing = [check for check in REVIEW_CHECKS if check not in set(review_checks or [])]
        if missing:
            raise bad_request("EMAIL_REVIEW_INCOMPLETE", f"Complete the review checklist first: {', '.join(missing)}.")
        # Each flag has to be acknowledged by name. Nothing is assumed for the owner.
        open_flags = [flag for flag in stored.get("review_flags") or [] if flag not in set(acknowledged_flags or [])]
        if open_flags:
            raise bad_request("EMAIL_REVIEW_FLAGS_OPEN", f"Acknowledge every review flag first: {', '.join(open_flags)}.")
        if source_status(store, stored=stored, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id) != "valid":
            raise conflict("FOLLOWUP_SOURCE_CHANGED", "The finalized presentation package changed. Regenerate the email from the current package.")
        require_complete_statics(opportunity.get("followup_statics"))
        text = stored["lengths"][selected_length]
        _checked_text(selected_length, text["subject"], text["body"])
        files = attachments_for(store, stored=stored, user_id=user_id, opportunity_id=opportunity_id, read_uploads=True)
        missing_files = [item["file_name"] for item in files if item["selected"] and not item["available"]]
        if missing_files:
            raise conflict("EMAIL_ATTACHMENT_UNAVAILABLE", f"A selected attachment is not available: {', '.join(missing_files)}.")
        _require_total_size(files)
        # An email that announces a file it does not carry is wrong whoever wrote the sentence.
        if not any(item["selected"] for item in files) and announces_attachment(str(text["body"])):
            raise bad_request(
                "EMAIL_ATTACHMENT_CONTRADICTION",
                "The message says that something is attached, but no file is selected. Select the approved file or change that sentence, then save and review again.",
            )
        review = {"checks": list(REVIEW_CHECKS), "acknowledged_flags": list(stored.get("review_flags") or [])}
    draft = _carry(stored)
    draft.update(
        {
            "status": "confirmed",
            "selected_length": selected_length,
            "confirmed_at": _now(),
            "confirmed_revision": expected_revision + 1,
            "confirmed_by": str(user_id),
            "review": review,
            "recipients": recipients,
        }
    )
    saved = store.save_email_draft(
        opportunity_id=opportunity_id, user_id=user_id, journey_stage=journey_stage, draft=draft, expected_revision=expected_revision
    )
    return envelope(store, opportunity_id=opportunity_id, user_id=user_id, journey_stage=journey_stage, stored=saved, opportunity=opportunity)


# ----------------------------------------------------------------------------- uploaded attachments (BAP-3)


def _uploads_allowed(stored: dict[str, Any]) -> None:
    source = stored.get("source")
    if not isinstance(source, dict) or source.get("kind") != SOURCE_KIND_V2:
        raise bad_request("EMAIL_ATTACHMENT_UNAVAILABLE", "Files can be attached to the follow-up email of a finalized Master Presentation package only.")


def _withdrawn(draft: dict[str, Any]) -> dict[str, Any]:
    """What was reviewed no longer exists in this form: the confirmation is withdrawn."""
    draft.update({"status": "draft", "confirmed_at": None, "confirmed_revision": None, "confirmed_by": None, "review": None, "recipients": None})
    return draft


def upload_attachment(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    draft_id: UUID,
    expected_revision: int,
    file_name: str | None,
    mime_type: str | None,
    content: bytes,
) -> dict[str, Any]:
    """Store one uploaded file and select it for the email. The draft's revision moves on.

    Checked before anything is stored: the revision the upload was made on, the number of
    uploads, the file itself and the total size of the selected attachments. If the draft
    changed in the meantime, the stored file is removed again.
    """
    stored = store.get_email_draft_by_id(opportunity_id=opportunity_id, user_id=user_id, draft_id=draft_id)
    _require_revision(stored, expected_revision)
    _uploads_allowed(stored)
    existing = store.list_email_attachments(opportunity_id=opportunity_id, user_id=user_id, journey_stage=stored["journey_stage"])
    if len(existing) >= MAX_EXTERNAL_ATTACHMENTS:
        raise bad_request("EMAIL_ATTACHMENT_LIMIT", f"An email can carry at most {MAX_EXTERNAL_ATTACHMENTS} uploaded files. Remove one first.")
    try:
        checked = validate_external_attachment(file_name, mime_type, content)
    except ExternalAttachmentError as exc:
        raise bad_request(exc.code, exc.user_message) from exc
    _require_total_size(attachments_for(store, stored=stored, user_id=user_id, opportunity_id=opportunity_id), extra=checked.size_bytes)
    attachment_id = uuid.uuid4()
    store.create_email_attachment(
        opportunity_id=opportunity_id,
        user_id=user_id,
        journey_stage=stored["journey_stage"],
        attachment_id=attachment_id,
        file_name=checked.file_name,
        extension=checked.extension,
        mime_type=checked.mime_type,
        size_bytes=checked.size_bytes,
        sha256=checked.sha256,
        # The stored name is the id, never the uploaded name: no path can be smuggled in.
        storage_path=f"{opportunity_id}/{attachment_id}{checked.extension}",
        content=content,
    )
    draft = _withdrawn(_carry(stored))
    draft["attachment_selection"] = {**(stored.get("attachment_selection") or {}), external_key(attachment_id): True}
    try:
        saved = store.save_email_draft(
            opportunity_id=opportunity_id, user_id=user_id, journey_stage=stored["journey_stage"], draft=draft, expected_revision=expected_revision
        )
    except Exception:
        store.delete_email_attachment(opportunity_id=opportunity_id, user_id=user_id, attachment_id=attachment_id)
        raise
    return envelope(store, opportunity_id=opportunity_id, user_id=user_id, journey_stage=stored["journey_stage"], stored=saved)


def remove_attachment(
    store: Any,
    *,
    opportunity_id: UUID,
    user_id: UUID,
    draft_id: UUID,
    attachment_id: UUID,
    expected_revision: int,
) -> dict[str, Any]:
    """Remove an uploaded file. The draft no longer names it; a confirmation is withdrawn."""
    stored = store.get_email_draft_by_id(opportunity_id=opportunity_id, user_id=user_id, draft_id=draft_id)
    _require_revision(stored, expected_revision)
    row = store.get_email_attachment(opportunity_id=opportunity_id, user_id=user_id, attachment_id=attachment_id)
    if row["journey_stage"] != stored["journey_stage"]:
        raise not_found("EMAIL_ATTACHMENT_NOT_FOUND", f"Attachment {attachment_id} was not found")
    draft = _withdrawn(_carry(stored))
    draft["attachment_selection"] = {key: value for key, value in (stored.get("attachment_selection") or {}).items() if key != external_key(attachment_id)}
    # The draft stops naming the file first, so it never points at a file that is gone.
    saved = store.save_email_draft(
        opportunity_id=opportunity_id, user_id=user_id, journey_stage=stored["journey_stage"], draft=draft, expected_revision=expected_revision
    )
    store.delete_email_attachment(opportunity_id=opportunity_id, user_id=user_id, attachment_id=attachment_id)
    return envelope(store, opportunity_id=opportunity_id, user_id=user_id, journey_stage=stored["journey_stage"], stored=saved)


def list_attachments(store: Any, *, opportunity_id: UUID, user_id: UUID, draft_id: UUID) -> list[dict[str, Any]]:
    stored = store.get_email_draft_by_id(opportunity_id=opportunity_id, user_id=user_id, draft_id=draft_id)
    return [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in attachments_for(store, stored=stored, user_id=user_id, opportunity_id=opportunity_id)
    ]


def download_attachment(store: Any, *, opportunity_id: UUID, user_id: UUID, draft_id: UUID, attachment_id: UUID) -> dict[str, Any]:
    stored = store.get_email_draft_by_id(opportunity_id=opportunity_id, user_id=user_id, draft_id=draft_id)
    row = store.get_email_attachment(opportunity_id=opportunity_id, user_id=user_id, attachment_id=attachment_id)
    if row["journey_stage"] != stored["journey_stage"]:
        raise not_found("EMAIL_ATTACHMENT_NOT_FOUND", f"Attachment {attachment_id} was not found")
    content = store.read_email_attachment_content(opportunity_id=opportunity_id, user_id=user_id, attachment_id=attachment_id)
    if hashlib.sha256(content).hexdigest() != row["sha256"]:
        raise conflict("EMAIL_ATTACHMENT_UNAVAILABLE", f"The stored file of {row['file_name']} is not the uploaded one.")
    return {"file_name": str(row["file_name"]), "media_type": str(row["mime_type"]), "content": content}


# ----------------------------------------------------------------------------- export


def _address(person: dict[str, Any]) -> str:
    return formataddr((str(person.get("name") or ""), str(person["email"])))


def export_eml(store: Any, *, opportunity_id: UUID, user_id: UUID, draft_id: UUID, revision: int) -> dict[str, Any]:
    """The confirmed draft as an unsent RFC 5322 message with the selected approved files.

    It is a file for the owner's own mail program. No message leaves the application.
    """
    stored = store.get_email_draft_by_id(opportunity_id=opportunity_id, user_id=user_id, draft_id=draft_id)
    opportunity = store.get_opportunity(opportunity_id=opportunity_id, user_id=user_id)
    if stored.get("status") != "confirmed" or not stored.get("selected_length"):
        raise conflict("EMAIL_NOT_CONFIRMED", "Review and confirm the saved draft before exporting it.")
    current = revision_of(stored)
    confirmed_revision = stored.get("confirmed_revision") or current
    if revision != current or confirmed_revision != current:
        raise conflict("EMAIL_EXPORT_STALE", "The draft changed after it was confirmed. Review and confirm it again.")
    if source_status(store, stored=stored, opportunity=opportunity, opportunity_id=opportunity_id, user_id=user_id) == "changed":
        raise conflict("FOLLOWUP_SOURCE_CHANGED", "The finalized presentation package changed. Regenerate the email from the current package.")
    recipients = stored.get("recipients") or _recipients(opportunity.get("followup_statics"))
    if not recipients:
        raise bad_request("FOLLOWUP_STATICS_REQUIRED", "Recipient and sender are required to export the email.")
    text = stored["lengths"][stored["selected_length"]]
    sender = recipients["sender"]
    message = EmailMessage(policy=SMTP)
    message["From"] = _address(sender)
    message["To"] = ", ".join(_address(person) for person in recipients["to"])
    if recipients.get("cc"):
        message["Cc"] = ", ".join(_address(person) for person in recipients["cc"])
    message["Subject"] = text["subject"]
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = make_msgid(domain=str(sender["email"]).rpartition("@")[2] or "localhost")
    # Mail programs open a message marked like this as a draft to be sent by the user.
    message["X-Unsent"] = "1"
    message["X-Borek-Draft-Id"] = str(stored["id"])
    message["X-Borek-Draft-Revision"] = str(current)
    message.set_content(str(text["body"]) + "\n", subtype="plain", charset="utf-8", cte="quoted-printable")
    attached = []
    selected = [item for item in attachments_for(store, stored=stored, user_id=user_id, opportunity_id=opportunity_id, read_uploads=True) if item["selected"]]
    missing = [item["file_name"] for item in selected if not item["available"]]
    if missing:
        raise conflict("EMAIL_ATTACHMENT_UNAVAILABLE", f"A selected attachment is not available: {', '.join(missing)}.")
    _require_total_size(selected)
    # Two files of the same name stay two files: the later one is renamed "name (2).ext".
    for item, file_name in zip(selected, unique_names([item["file_name"] for item in selected]), strict=True):
        data = item["_bytes"] if "_bytes" in item else item["_path"].read_bytes()
        maintype, subtype = (item["_mime"].split("/", 1) if "_mime" in item else MEDIA_TYPES[item["format"]])
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=file_name)
        attached.append(
            {
                "format": item["format"],
                "file_name": file_name,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "key": item["key"],
                "artifact": item["artifact"],
                "artifact_version_id": item["artifact_version_id"],
                "presentation_version_id": item["presentation_version_id"],
            }
        )
    presentations = [item["presentation_version_id"] for item in attached if item["artifact"] == "master_presentation_v2"]
    if presentations:
        message["X-Borek-Presentation-Version-Id"] = presentations[0]
    project = str((opportunity.get("followup_statics") or {}).get("project_name") or "follow-up")
    content = message.as_bytes()
    # The estimate decided the selection; the exported message itself is the final check.
    if len(content) > MAX_EMAIL_BYTES:
        raise _too_large(len(content))
    return {
        "file_name": f"{_safe_name(project)} - follow-up email.eml",
        "content": content,
        "revision": current,
        "attachments": attached,
    }
