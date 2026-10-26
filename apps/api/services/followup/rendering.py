"""MS-32 / BT-33 — JJ-32 JSON + project statics → follow-up email text."""

from __future__ import annotations

import re
from typing import Any

SHORT_CONTENT_WORD_CAP = 150
MEDIUM_CONTENT_WORD_CAP = 300
EXTENSIVE_CONTENT_WORD_CAP = 500
DATE_TOKEN = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
PLACEHOLDER = re.compile(r"\{\{[^}]+\}\}")


def primary_recipient(statics: dict[str, Any]) -> dict[str, Any]:
    recipients = statics.get("standard_recipients") or []
    for recipient in recipients:
        if recipient.get("kind") == "to" and recipient.get("primary"):
            return recipient
    return recipients[0] if recipients else {}


_MALE = frozenset({"herr", "mr", "mr.", "sir"})
_FEMALE = frozenset({"frau", "ms", "ms.", "mrs", "mrs.", "miss", "madam"})


def greeting(statics: dict[str, Any], language: str = "en") -> str:
    recipient = primary_recipient(statics)
    if language == "de":
        # BAP-13: German forms of address. The salutation is free text; a form we cannot read as
        # Herr or Frau gets the neutral "Guten Tag", never a guessed gender.
        if statics.get("salutation_style") == "formal":
            salutation = str(recipient.get("salutation") or "").strip()
            last = recipient.get("last_name")
            if salutation.lower() in _MALE:
                return f"Sehr geehrter Herr {last},"
            if salutation.lower() in _FEMALE:
                return f"Sehr geehrte Frau {last},"
            return f"Guten Tag {salutation} {last},"
        return f"Hallo {recipient.get('first_name')},"
    if statics.get("salutation_style") == "formal":
        return f"Dear {recipient.get('salutation')} {recipient.get('last_name')},"
    return f"Hi {recipient.get('first_name')},"


def action_due(due: str) -> str:
    return "date to be confirmed" if due == "TBD" else due


def followup_content_word_count(body: str) -> int:
    without_greeting = re.sub(r"^\s*[^\n]+,\s*\n", "", body, count=1)
    parts = re.split(r"\n\s*(?:Best regards|Viele Grüße|Mit freundlichen Grüßen)\s*\n", without_greeting, maxsplit=1, flags=re.IGNORECASE)
    content = parts[0].strip()
    return len(content.split()) if content else 0


def render_followup_draft(
    extraction: dict[str, Any],
    statics: dict[str, Any],
    *,
    attachment_name: str | None = None,
    include_participants: bool = False,
    include_review_flags: bool = False,
    protocol_overflow: bool = False,
    max_key_points: int = 3,
    max_decisions: int | None = None,
    max_actions: int = 5,
    include_decisions: bool = True,
) -> dict[str, Any]:
    """Render the MS-32 follow-up template (plain text body + subject)."""
    lines = [
        greeting(statics),
        "",
        f"thank you for your time on {extraction['meeting_date']}. Below is a short summary of what we agreed, so we all work from the same picture.",
    ]
    if include_participants:
        names = [
            str(person.get("name") or "").strip()
            for person in extraction.get("participants") or []
            if str(person.get("name") or "").strip()
        ]
        if names:
            lines.extend(["", f"Participants: {', '.join(names)}."])

    key_points = list(extraction.get("key_points") or [])[:max_key_points]
    if key_points:
        lines.extend(["", "Key points", *[f"- {point}" for point in key_points]])

    decisions = list(extraction.get("decisions") or [])
    if max_decisions is not None:
        decisions = decisions[:max_decisions]
    if include_decisions and decisions:
        lines.extend(
            [
                "",
                "Decisions",
                *[f"- {item} (agreed {extraction['meeting_date']})" for item in decisions],
            ]
        )

    actions = list(extraction.get("action_items") or [])[:max_actions]
    if actions:
        lines.extend(
            [
                "",
                "Next steps",
                *[
                    f"- {item['action']} — {item['owner']}, by {action_due(str(item.get('due') or 'TBD'))}"
                    for item in actions
                ],
            ]
        )
    else:
        lines.extend(["", "No action items were agreed for now."])

    open_questions = list(extraction.get("open_questions") or [])
    if open_questions:
        lines.extend(["", "Open from our side", *[f"- {item}" for item in open_questions]])

    next_meeting = extraction.get("next_meeting")
    if isinstance(next_meeting, dict) and next_meeting.get("date") and next_meeting.get("time"):
        lines.extend(
            [
                "",
                f"Next session: {next_meeting['date']}, {next_meeting['time']}.",
            ]
        )

    if attachment_name:
        lines.extend(["", f"Attached you will find {attachment_name} with the full detail."])

    if protocol_overflow and "action_overflow_see_protocol" in (extraction.get("review_flags") or []):
        lines.extend(
            [
                "",
                "Additional actions and detail are recorded in the meeting protocol.",
            ]
        )

    if include_review_flags:
        flags = [str(flag).strip() for flag in extraction.get("review_flags") or [] if str(flag).strip()]
        if flags:
            lines.extend(["", "Review flags", *[f"- {flag}" for flag in flags]])

    lines.extend(
        [
            "",
            "If anything here does not match your understanding, just let me know and I will correct it.",
            "",
            "Best regards",
            str((statics.get("sender_profile") or {}).get("name") or "").strip(),
            f"{(statics.get('sender_profile') or {}).get('role') or ''} - BOREK".strip(),
        ]
    )

    project_name = str(statics.get("project_name") or extraction.get("project_name") or "Project").strip()
    subject = f"{project_name} — Follow-up {extraction['meeting_topic']} ({extraction['meeting_date']})"
    body = "\n".join(line for line in lines if line is not None)
    return {
        "subject": subject,
        "body": body,
        "review_flags": list(extraction.get("review_flags") or []),
    }


def _clip_content(body: str, word_cap: int) -> str:
    match = re.search(r"\n\s*Best regards\s*\n", body, flags=re.IGNORECASE)
    if not match:
        words = body.split()
        return " ".join(words[:word_cap])
    prefix = body[: match.start()]
    suffix = body[match.start() :]
    greeting_line, _, remainder = prefix.partition("\n\n")
    if not remainder.strip():
        return body
    words = remainder.split()
    clipped = " ".join(words[:word_cap])
    return f"{greeting_line}\n\n{clipped}{suffix}"


def render_three_lengths(
    extraction: dict[str, Any],
    statics: dict[str, Any],
    *,
    attachment_name: str | None = None,
) -> dict[str, dict[str, Any]]:
    short_draft = render_followup_draft(
        extraction,
        statics,
        attachment_name=attachment_name,
        max_key_points=2,
        max_decisions=0,
        max_actions=3,
        include_decisions=False,
    )
    short_body = _clip_content(short_draft["body"], SHORT_CONTENT_WORD_CAP)

    medium_draft = render_followup_draft(
        extraction,
        statics,
        attachment_name=attachment_name,
        include_participants=True,
    )
    medium_body = _clip_content(medium_draft["body"], MEDIUM_CONTENT_WORD_CAP)

    extensive_draft = render_followup_draft(
        extraction,
        statics,
        attachment_name=attachment_name,
        include_participants=True,
        include_review_flags=True,
        protocol_overflow=True,
    )
    extensive_body = _clip_content(extensive_draft["body"], EXTENSIVE_CONTENT_WORD_CAP)

    subject = short_draft["subject"]
    return {
        "short": {
            "subject": subject,
            "body": short_body,
            "word_count": followup_content_word_count(short_body),
        },
        "medium": {
            "subject": subject,
            "body": medium_body,
            "word_count": followup_content_word_count(medium_body),
        },
        "extensive": {
            "subject": subject,
            "body": extensive_body,
            "word_count": followup_content_word_count(extensive_body),
        },
    }


def render_first_contact_draft(
    statics: dict[str, Any],
    *,
    topic: str,
    meeting_date: str,
) -> dict[str, dict[str, Any]]:
    """Optional pre-meeting note — MS-32 tone, no JJ-32 extraction."""
    extraction = {
        "meeting_topic": " ".join(topic.split()[:4]) or "First contact",
        "meeting_date": meeting_date,
        "key_points": [
            "We received your client documents and will not invent company facts.",
            f"Our first conversation will focus on {topic}.",
        ],
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "next_meeting": None,
        "participants": [],
        "review_flags": [],
    }
    lines = [
        greeting(statics),
        "",
        f"thank you for sharing background material ahead of our conversation on {meeting_date}.",
        "",
        "Key points",
        *[f"- {point}" for point in extraction["key_points"]],
        "",
        "Next steps",
        "- Borek prepares discovery questions grounded in the documents you provided.",
        "",
        "If anything here does not match your understanding, just let me know and I will correct it.",
        "",
        "Best regards",
        str((statics.get("sender_profile") or {}).get("name") or "").strip(),
        f"{(statics.get('sender_profile') or {}).get('role') or ''} - BOREK".strip(),
    ]
    project_name = str(statics.get("project_name") or "Project").strip()
    subject = f"{project_name} — First contact {extraction['meeting_topic']} ({meeting_date})"
    body = "\n".join(lines)
    short_body = _clip_content(body, SHORT_CONTENT_WORD_CAP)
    medium_body = _clip_content(body, MEDIUM_CONTENT_WORD_CAP)
    extensive_body = _clip_content(
        body.replace(
            "Next steps",
            "Next steps\n- We align the agenda to your stated sales topic before the meeting.",
            1,
        ),
        EXTENSIVE_CONTENT_WORD_CAP,
    )
    return {
        "short": {"subject": subject, "body": short_body, "word_count": followup_content_word_count(short_body)},
        "medium": {"subject": subject, "body": medium_body, "word_count": followup_content_word_count(medium_body)},
        "extensive": {
            "subject": subject,
            "body": extensive_body,
            "word_count": followup_content_word_count(extensive_body),
        },
    }


def draft_has_placeholders(text: str) -> bool:
    return PLACEHOLDER.search(text) is not None
