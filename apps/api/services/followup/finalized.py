"""Follow-up email from a finalized Master Presentation V2 package.

The email is written from the SAME frozen material the reviewed V2 was generated from: the
findings the owner confirmed, each with its source. Nothing is read from live state here and
nothing is added to it.

What a finding's source does and does not establish:

    source transcript / both   the statement was made IN THE MEETING. It does not say WHO made
                               it: a BOREK employee's suggestion is in the transcript as well.
                               The email therefore reports such statements neutrally, as points
                               noted in the meeting - never as "your requirement", "you said" or
                               "we agreed" - and the owner is asked to check who said what.
    source personal_notes      the owner's own observation. It is left out and flagged.
    source owner               a point the owner wrote or reworded in Discovery 2 (BAP-7): the
                               owner's wording, not a meeting statement. Left out and flagged.
    excluded findings          are not in the snapshot's findings at all.
    owners, dates, people      only what a confirmed finding itself states.

A draft never exceeds its word cap. Statements are left out whole, never shortened; when not
even one statement fits a length, generation fails with ``ContentDoesNotFit``.

The email reads as a letter, not as a report: a greeting, a word of thanks, the situation as it
was understood, where BOREK sees its role, the attached presentation, the next steps and a
closing. Confirmed statements are woven into sentences ("The core requirements, as we understood
them: ...; ...") instead of being listed under headings. The wording around them is fixed, names
the client only as the owner entered it, and claims nothing about the meeting: every fact in the
email is a confirmed statement. A statement is used in its own words; inside a sentence it loses
its final full stop and, when it starts with an ordinary word such as "The" or "Start", its
capital letter - nothing else. Where it is not clear how a first word is written inside a
sentence, the statements follow the lead-in unchanged. A sentence without a confirmed statement is not written, and the
presentation is only called attached when a file is selected for the email.

Pure functions: they read their arguments and return plain data.
"""

from __future__ import annotations

import re
from typing import Any

from services.followup.rendering import (
    EXTENSIVE_CONTENT_WORD_CAP,
    MEDIUM_CONTENT_WORD_CAP,
    SHORT_CONTENT_WORD_CAP,
    followup_content_word_count,
    greeting,
)

RENDERER_VERSION = "followup-finalized:v3"
# Said in the meeting. Not: said by the client (see the module text).
MEETING_SOURCES = frozenset({"transcript", "both"})
OWNER_SOURCE = "personal_notes"
OWNER_SOURCES = frozenset({OWNER_SOURCE, "owner"})
HEARD_CATEGORIES = ("requirements", "challenges", "priorities")
DISCUSSED_CATEGORIES = ("discussed_solutions", "opportunities")
WORD_CAPS = {"short": SHORT_CONTENT_WORD_CAP, "medium": MEDIUM_CONTENT_WORD_CAP, "extensive": EXTENSIVE_CONTENT_WORD_CAP}
LENGTHS = ("short", "medium", "extensive")

# Review flags. Each one names something the owner has to check before the email is confirmed.
FLAG_FIXTURE_EXTRACTION = "fixture_extraction"
FLAG_SPEAKER_UNVERIFIED = "speaker_unverified"
FLAG_OWNER_NOTES_OMITTED = "owner_notes_omitted"
FLAG_ACTION_OWNER_UNCONFIRMED = "action_owner_unconfirmed"
FLAG_ACTION_DATE_UNCONFIRMED = "action_date_unconfirmed"
FLAG_MEETING_DATE_UNCONFIRMED = "meeting_date_unconfirmed"
FLAG_NO_NEXT_STEPS = "no_next_steps_confirmed"
FLAG_CONTENT_TRIMMED = "content_trimmed"

_DATE = re.compile(r"\b\d{1,2}\.\d{1,2}\.(?:\d{2}|\d{4})\b|\b\d{4}-\d{2}-\d{2}\b")
_DEADLINE = re.compile(
    r"\b(?:by|until|before|on|next|this)\s+(?:the\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|month|end|"
    r"january|february|march|april|may|june|july|august|september|october|november|december|\d)|\b(?:tomorrow|today)\b",
    re.IGNORECASE,
)
# "Dana Weber will send ..." or "Borek: send ..." - an owner the finding itself states.
_OWNER = re.compile(r"^(?:[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3})\s+(?:will|sends?|shares?|provides?|prepares?|schedules?)\b|^[A-Z][\w .'()/-]{1,60}:\s+\S")


class NoConfirmedContent(ValueError):
    """The finalized package holds no confirmed meeting statement an email could report."""

    code = "FOLLOWUP_NO_CONFIRMED_CONTENT"


class ContentDoesNotFit(ValueError):
    """Not even one confirmed statement fits a draft's word cap without being shortened."""

    code = "FOLLOWUP_CONTENT_TOO_LONG"


def _texts(findings: dict[str, Any], categories: tuple[str, ...], sources: frozenset[str]) -> list[str]:
    seen: list[str] = []
    for category in categories:
        for item in findings.get(category) or []:
            text = " ".join(str(item.get("text") or "").split())
            if text and item.get("source") in sources and text not in seen:
                seen.append(text)
    return seen


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else f"{text}."


def has_deadline(text: str) -> bool:
    return bool(_DATE.search(text) or _DEADLINE.search(text))


def states_owner(text: str) -> bool:
    return bool(_OWNER.search(text))


def content_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """What the email may say, taken from the frozen V2 sources, and what the owner must check."""
    findings = snapshot.get("findings") or {}
    heard = _texts(findings, HEARD_CATEGORIES, MEETING_SOURCES)
    decisions = _texts(findings, ("decisions",), MEETING_SOURCES)
    discussed = _texts(findings, DISCUSSED_CATEGORIES, MEETING_SOURCES)
    by_category = {name: _texts(findings, (name,), MEETING_SOURCES) for name in (*HEARD_CATEGORIES, *DISCUSSED_CATEGORIES)}
    actions = _texts(findings, ("follow_ups",), MEETING_SOURCES)
    if not (heard or decisions or discussed or actions):
        raise NoConfirmedContent("The finalized package contains no confirmed statement from the meeting.")
    owner_only = _texts(findings, (*HEARD_CATEGORIES, *DISCUSSED_CATEGORIES, "decisions", "follow_ups"), OWNER_SOURCES)
    flags: list[str] = []
    if (snapshot.get("meeting_extraction") or {}).get("execution_mode") != "live":
        flags.append(FLAG_FIXTURE_EXTRACTION)
    # The package records that a statement was made in the meeting, not who made it or who agreed.
    flags.append(FLAG_SPEAKER_UNVERIFIED)
    # No source in the package states the day of the meeting, so the email does not name one.
    flags.append(FLAG_MEETING_DATE_UNCONFIRMED)
    if owner_only:
        flags.append(FLAG_OWNER_NOTES_OMITTED)
    if not actions:
        flags.append(FLAG_NO_NEXT_STEPS)
    if any(not states_owner(text) for text in actions):
        flags.append(FLAG_ACTION_OWNER_UNCONFIRMED)
    if any(not has_deadline(text) for text in actions):
        flags.append(FLAG_ACTION_DATE_UNCONFIRMED)
    return {
        "heard": heard,
        "decisions": decisions,
        "discussed": discussed,
        "by_category": by_category,
        "actions": actions,
        "owner_only_count": len(owner_only),
        "review_flags": flags,
    }


def _action_line(text: str) -> str:
    line = _sentence(text)
    return line if has_deadline(text) else f"{line[:-1]} (date to be confirmed)."


# Words a statement may start with that are safe to write in lower case inside a sentence. A
# name, an abbreviation or anything else keeps its capital.
_LOWER_SAFE = frozenset(
    "a an the every each all no any some our we it its this these that those there most many several both one two three "
    "data start run send share arrange use keep begin focus provide prepare schedule".split()
)


def ordinary_words(statements: list[str]) -> frozenset[str]:
    """Words the confirmed statements themselves write in lower case inside a sentence.

    "motor" in "... a pilot with motor claims ..." shows that "Motor claims must ..." does not
    start with a name.
    """
    words: set[str] = set()
    for text in statements:
        words.update(re.findall(r"\b[a-z][a-z'-]*\b", text.split(" ", 1)[1] if " " in text else ""))
    return frozenset(words)


def known_names(statements: list[str], statics: dict[str, Any]) -> frozenset[str]:
    """Words that are written with a capital inside a sentence, and the names the owner entered."""
    names: set[str] = {"Borek", "BOREK"}
    for text in statements:
        names.update(re.findall(r"\b[A-Z][A-Za-z]+\b", text.split(" ", 1)[1] if " " in text else ""))
    people = [*(statics.get("standard_recipients") or []), statics.get("sender_profile") or {}]
    entered = [statics.get("client_short"), *[item.get(key) for item in people if isinstance(item, dict) for key in ("first_name", "last_name", "name")]]
    for value in entered:
        names.update(re.findall(r"[A-Za-z]+", str(value or "")))
    return frozenset(names)


def _first_word(text: str) -> str:
    return " ".join(text.split()).split(" ", 1)[0].split("'", 1)[0].rstrip(",:")


def _starts_ordinary(text: str, vocabulary: frozenset[str]) -> bool:
    first = _first_word(text)
    return first.isalpha() and first[0].isupper() and (len(first) == 1 or first[1:].islower()) and (first.lower() in _LOWER_SAFE or first.lower() in vocabulary)


def _starts_with_name(text: str, names: frozenset[str]) -> bool:
    """A name, an abbreviation or a number: it keeps its form anywhere in a sentence."""
    first = _first_word(text)
    return first in names or not first.isalpha() or (len(first) > 1 and not first[1:].islower())


def inline_form(text: str, vocabulary: frozenset[str] = frozenset()) -> str:
    """A confirmed statement as part of a sentence: no final full stop, an ordinary first word in lower case."""
    text = " ".join(text.split()).rstrip(".")
    return text[0].lower() + text[1:] if _starts_ordinary(text, vocabulary) else text


def _fits_a_sentence(text: str) -> bool:
    """One clause or phrase. A statement of several sentences stays as it is."""
    body = text.rstrip(".")
    return not any(mark in body for mark in (". ", "? ", "! ", ";", "\n")) and not text.endswith(("?", "!"))


# One sentence of the email: how it begins (for one statement, for several) and the confirmed
# statements it holds. ``limit`` is how many statements this length shows at most. ``glue`` keeps
# the sentence in the paragraph before it. Next steps are a short list when there are several,
# because each one is something to do; everything else is running text.
Block = dict[str, Any]


def _block(lead: tuple[str, str], items: list[str], limit: int | None = None, *, glue: bool = False, steps: bool = False) -> Block:
    return {"lead": lead, "items": list(items), "limit": limit, "glue": glue, "steps": steps}


def _paragraph(block: Block, vocabulary: frozenset[str] = frozenset(), names: frozenset[str] = frozenset()) -> str:
    items = block["items"]
    lead = block["lead"][0 if len(items) == 1 else 1]
    if block["steps"]:
        return f"{lead} {items[0]}" if len(items) == 1 else "\n".join([lead, *[f"- {item}" for item in items]])
    # Woven into one sentence only when every statement is one clause and it is clear how its
    # first word is written there. Otherwise the statements follow the lead-in as they were confirmed.
    if all(_fits_a_sentence(item) and (_starts_ordinary(item, vocabulary) or _starts_with_name(item, names)) for item in items):
        return f"{lead} {'; '.join(inline_form(item, vocabulary) for item in items)}."
    return " ".join([lead, *[_sentence(item) for item in items]])


def _body(statics: dict[str, Any], blocks: list[Block], *, frame: dict[str, Any], attachment: bool) -> str:
    paragraphs = [frame["opening"]]
    vocabulary, names = frozenset(frame.get("vocabulary") or ()), frozenset(frame.get("names") or ())
    glued = False
    for block in [item for item in blocks if not item["steps"]]:
        if not block["items"]:
            continue
        if block["glue"] and glued:
            paragraphs[-1] = f"{paragraphs[-1]} {_paragraph(block, vocabulary, names)}"
        else:
            paragraphs.append(_paragraph(block, vocabulary, names))
        glued = True
    # What was understood, then the attached presentation, then what happens next.
    if attachment:
        paragraphs.append(frame["attachment"])
    paragraphs.extend(_paragraph(block) for block in blocks if block["steps"] and block["items"])
    paragraphs.append(frame["closing"])
    return "\n\n".join([greeting(statics), *paragraphs, signature(statics)])


def signature(statics: dict[str, Any]) -> str:
    """Closing formula and the sender the owner entered."""
    sender = statics.get("sender_profile") or {}
    return "\n".join(["Best regards", str(sender.get("name") or "").strip(), f"{sender.get('role') or ''} - BOREK".strip()])


_NO_ATTACHMENT = re.compile(r"\b(?:no|not|without|nothing|none|never)\b[^.!?\n]{0,30}?\battach\w*", re.IGNORECASE)
_ATTACHMENT_CLAIM = re.compile(r"\b(?:attach\w*|enclos\w*)\b", re.IGNORECASE)


def announces_attachment(body: str) -> bool:
    """The text says that something is attached ("attached", "attachment", "enclosed").

    "No attachment" and "not attached" say the opposite and do not count.
    """
    return bool(_ATTACHMENT_CLAIM.search(_NO_ATTACHMENT.sub(" ", body)))


def _fit(statics: dict[str, Any], plan: list[Block], *, frame: dict[str, Any], attachment: bool, cap: int, name: str) -> tuple[str, bool]:
    """Leave out whole statements until the content fits the cap. Never shortens a statement.

    A statement that could not fit this length even on its own is left out first, so that it does
    not push shorter ones out. Then sentences with several statements give way, so every sentence
    keeps its first statement as long as possible; after that the longest remaining statement goes,
    then the next, until the draft fits. If nothing is left, no statement fits this length.
    """
    def render(blocks: list[Block]) -> str:
        return _body(statics, blocks, frame=frame, attachment=attachment)

    trimmed = False
    blocks: list[Block] = []
    for block in plan:
        alone = [item for item in block["items"] if followup_content_word_count(render([{**block, "items": [item]}])) <= cap]
        trimmed = trimmed or len(alone) < len(block["items"])
        blocks.append({**block, "items": alone if block["limit"] is None else alone[: block["limit"]]})

    while followup_content_word_count(render(blocks)) > cap:
        several = [index for index, block in enumerate(blocks) if len(block["items"]) > 1]
        if several:
            index = max(several, key=lambda position: (len(blocks[position]["items"]), position))
            blocks[index]["items"].pop()
        else:
            single = [index for index, block in enumerate(blocks) if block["items"]]
            if not single:
                break
            index = max(single, key=lambda position: (len(blocks[position]["items"][0].split()), position))
            blocks[index]["items"].clear()
        trimmed = True
    if followup_content_word_count(render(blocks)) > cap or not any(block["items"] for block in blocks):
        raise ContentDoesNotFit(
            f"No confirmed statement fits the {name} draft ({cap} content words) without being shortened. "
            "A single confirmed finding is longer than this draft allows."
        )
    return render(blocks), trimmed


# The fixed wording. None of it states a fact about the meeting, names a person or a date, or says
# who said or decided something: that is what the confirmed statements are for. "As we understood
# them" says whose reading this is. Ideas are introduced as BOREK's view of its role and, in the
# same sentence, as not decided.
LEAD_BRIEF = ("In brief, the main point as we understood it:", "In brief, the main points as we understood them:")
LEAD_REQUIREMENTS = ("The core requirement, as we understood it:", "The core requirements, as we understood them:")
LEAD_CHALLENGES = ("What makes this difficult today:", "What makes this difficult today:")
LEAD_PRIORITIES = ("As for priorities:", "As for priorities:")
LEAD_DECISIONS = ("For the way forward, this is the direction that was discussed:", "For the way forward, this is the direction that was discussed:")
LEAD_ROLE = (
    "This is where we see our role. One idea we talked through, not decided yet:",
    "This is where we see our role. Among the ideas we talked through, none of them decided yet:",
)
LEAD_AHEAD = ("Looking further ahead, this also came up:", "Looking further ahead, these points also came up:")
LEAD_STEPS = ("As a next step:", "As next steps:")
ATTACHMENT_SENTENCE = "Please find attached the presentation, which we updated after our meeting to reflect these points."
SHORT_ATTACHMENT_SENTENCE = "The updated presentation is attached."
CLOSING = (
    "Should anything here differ from your understanding, just let me know and I will correct it. "
    "Many thanks again for your time; I look forward to continuing the conversation."
)


def _frames(statics: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Opening, attachment sentence and closing per length. The client is named as the owner entered it."""
    client = " ".join(str(statics.get("client_short") or "").split())
    picture = f"a clear picture of the situation at {client}" if client else "a clear picture of the current situation"
    return {
        "short": {
            "opening": f"Thank you for your time in our meeting. It gave us {picture}.",
            "attachment": SHORT_ATTACHMENT_SENTENCE,
            "closing": "Should anything differ from your understanding, just let me know.",
        },
        "medium": {
            "opening": f"Thank you for taking the time to speak with us. The conversation gave us {picture}, and I would like to reflect it back briefly.",
            "attachment": ATTACHMENT_SENTENCE,
            "closing": CLOSING,
        },
        "extensive": {
            "opening": f"Thank you for taking the time to speak with us. The conversation gave us {picture}, and I would like to reflect it back in some detail, so that we all work from the same picture.",
            "attachment": ATTACHMENT_SENTENCE,
            "closing": CLOSING,
        },
    }


def render_finalized_lengths(content: dict[str, Any], statics: dict[str, Any], *, attachment: bool = True) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Three emails from the same confirmed material: each longer one adds supported detail only.

    short      the first points in one sentence, and the next steps
    medium     requirements, challenges and priorities as one paragraph, the direction discussed,
               the ideas talked through, all next steps
    extensive  every confirmed statement, with opportunities apart from discussed solutions

    ``attachment`` is whether a file is selected for the email. Without one, no sentence says
    that anything is attached.

    Wording is neutral about who said what: no sentence says that the client stated, required or
    agreed something. The ideas that were discussed are introduced as where BOREK sees its role
    and, in the same sentence, as not decided.

    The extensive draft is longer only where the package holds more confirmed statements than the
    medium draft shows. With little material the two stay close; nothing is added to fill space.
    """
    heard, decisions, discussed = content["heard"], content["decisions"], content["discussed"]
    actions = [_action_line(text) for text in content["actions"]]
    grouped = {name: list(items) for name, items in content["by_category"].items()}
    seen: set[str] = set()
    for name in (*HEARD_CATEGORIES, *DISCUSSED_CATEGORIES):  # a statement confirmed twice is written once
        grouped[name] = [item for item in grouped[name] if not (item in seen or seen.add(item))]
    project = str(statics.get("project_name") or "").strip()
    subject = f"{project} — Follow-up to our meeting" if project else "Follow-up to our meeting"
    plans = {
        "short": [_block(LEAD_BRIEF, [*heard, *decisions], 3), _block(LEAD_STEPS, actions, 5, steps=True)],
        "medium": [
            _block(LEAD_REQUIREMENTS, grouped["requirements"], 2),
            _block(LEAD_CHALLENGES, grouped["challenges"], 2, glue=True),
            _block(LEAD_PRIORITIES, grouped["priorities"], 2, glue=True),
            _block(LEAD_DECISIONS, decisions),
            _block(LEAD_ROLE, discussed, 3),
            _block(LEAD_STEPS, actions, steps=True),
        ],
        "extensive": [
            _block(LEAD_REQUIREMENTS, grouped["requirements"]),
            _block(LEAD_CHALLENGES, grouped["challenges"]),
            _block(LEAD_PRIORITIES, grouped["priorities"], glue=True),
            _block(LEAD_DECISIONS, decisions),
            _block(LEAD_ROLE, grouped["discussed_solutions"]),
            _block(LEAD_AHEAD, grouped["opportunities"], glue=True),
            _block(LEAD_STEPS, actions, steps=True),
        ],
    }
    frames = _frames(statics)
    confirmed = [*heard, *decisions, *discussed, *content["actions"]]
    vocabulary, names = ordinary_words(confirmed), known_names(confirmed, statics)
    lengths: dict[str, dict[str, Any]] = {}
    flags = list(content["review_flags"])
    for name in LENGTHS:
        body, trimmed = _fit(statics, plans[name], frame={**frames[name], "vocabulary": vocabulary, "names": names}, attachment=attachment, cap=WORD_CAPS[name], name=name)
        if trimmed and name == "extensive" and FLAG_CONTENT_TRIMMED not in flags:
            flags.append(FLAG_CONTENT_TRIMMED)
        lengths[name] = {"subject": subject, "body": body, "word_count": max(1, followup_content_word_count(body)), "edited": False}
    return lengths, flags
