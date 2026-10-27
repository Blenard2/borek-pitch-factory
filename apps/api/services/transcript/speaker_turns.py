"""ES-2 — split a normalized transcript into speaker turns.

Each turn is one utterance with a speaker label. Caption formats (.vtt / .srt)
use one cue as one turn. Plain text (.txt / .docx) uses ``Name:`` prefixes;
unlabeled lines continue the previous turn.

Meeting tools often put the time in front of the speaker::

    [02:15] Daniel: Requirement: Reduce support response time
    00:02:15 Daniel: Requirement: Reduce support response time

A time stamp in front of a ``Name:`` prefix is read as the start of that speaker's turn and is
not part of what was said. A time at the start of a line that carries no speaker ("10:30 works
for me") is conversation and stays. A line that starts with a finding label instead of a name
("Decision: ...") is a new statement of the speaker who is talking, not a speaker called
"Decision".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from services.transcript.ingestion import (
    TranscriptIngestionError,
    _decode_text_bytes,
    _HTML_TAG_RE,
    _SRT_INDEX_RE,
    _TIMESTAMP_RE,
    ingest_transcript,
)
from services.transcript.speaker_names import SPEAKER_NAME, clean_speaker, is_finding_label, normalize_text

UNKNOWN_SPEAKER = "unknown"

_VOICE_TAG_RE = re.compile(r"<v\s+([^>]+)>", re.IGNORECASE)
# Names in any script: "Dr. Jürgen Weiß", "Petra Schäfer", "Sarah O'Neill-Hughes" (see speaker_names).
_SPEAKER_PREFIX_RE = re.compile(rf"^(?P<speaker>{SPEAKER_NAME}):\s*(?P<text>.*)$")


# "[02:15]", "(00:02:15)", "00:02:15", "02:15.500", optionally followed by a dash.
_LEADING_TIMESTAMP_RE = re.compile(r"^\s*[\[(]?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?[\])]?\s*(?:[-\u2013\u2014]\s*)?(?=\S)")
_LIST_BULLET_RE = re.compile(r"^\s*[-*\u2022]\s+")
# "At 14:00 I have a call": the colon belongs to a clock time, not to a speaker called "At 14".
_CLOCK_TIME_SPLIT_RE = re.compile(r"\d$")


def _is_label(name: str) -> bool:
    """A finding label ("Decision", "Entscheidung", "Nächste Schritte"), not a speaker."""
    return is_finding_label(name.strip())


def _speaker_prefix(line: str) -> re.Match[str] | None:
    """The ``Name:`` prefix of a line, with or without a time stamp in front of it."""
    prefix = _SPEAKER_PREFIX_RE.match(line)
    if prefix is None:
        without_time = _LEADING_TIMESTAMP_RE.sub("", line, count=1)
        if without_time != line:
            prefix = _SPEAKER_PREFIX_RE.match(without_time)
    if prefix is None:
        return None
    speaker = prefix.group("speaker").strip()
    if _is_label(speaker):
        return None
    if _CLOCK_TIME_SPLIT_RE.search(speaker) and re.match(r"\d{2}(?!\d)", prefix.group("text")):
        return None
    return prefix


def _without_lead(line: str) -> str:
    """The line without a list bullet and a time stamp in front of it."""
    return _LEADING_TIMESTAMP_RE.sub("", _LIST_BULLET_RE.sub("", line, count=1), count=1)


def _is_statement_label(line: str) -> bool:
    label = _SPEAKER_PREFIX_RE.match(_without_lead(line))
    return label is not None and _is_label(label.group("speaker"))


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    turn_index: int
    speaker: str
    text: str


def split_speaker_turns(filename: str, content: bytes) -> list[SpeakerTurn]:
    ingested = ingest_transcript(filename, content)
    extension = ingested.extension

    if extension == ".vtt":
        raw_turns = _turns_from_cues(normalize_text(_decode_text_bytes(content)), skip_webvtt_header=True)
    elif extension == ".srt":
        raw_turns = _turns_from_cues(normalize_text(_decode_text_bytes(content)), skip_webvtt_header=False)
    else:
        raw_turns = _turns_from_plain_text(normalize_text(ingested.normalized_text))

    if not raw_turns:
        raise TranscriptIngestionError(
            "No speaker turns could be read from this transcript. "
            "Check the export and upload a .txt, .vtt, .srt, or .docx file."
        )

    return [
        SpeakerTurn(turn_index=index, speaker=speaker, text=text)
        for index, (speaker, text) in enumerate(raw_turns)
    ]


def _turns_from_cues(raw: str, *, skip_webvtt_header: bool) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    payload: list[str] = []
    in_payload = False

    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if skip_webvtt_header and stripped.upper().startswith("WEBVTT"):
            continue
        if stripped.upper().startswith("KIND:") or stripped.upper().startswith("LANGUAGE:"):
            continue
        if stripped.startswith("NOTE"):
            continue

        if not stripped:
            _flush_cue(turns, payload)
            payload = []
            in_payload = False
            continue

        if _TIMESTAMP_RE.match(stripped):
            _flush_cue(turns, payload)
            payload = []
            in_payload = True
            continue

        if not in_payload and _SRT_INDEX_RE.match(stripped):
            continue

        payload.append(stripped)
        in_payload = True

    _flush_cue(turns, payload)
    return turns


def _flush_cue(turns: list[tuple[str, str]], payload: list[str]) -> None:
    if not payload:
        return
    joined = " ".join(payload)
    speaker = UNKNOWN_SPEAKER
    voice = _VOICE_TAG_RE.search(joined)
    if voice:
        speaker = _clean_speaker(voice.group(1))
    plain = _HTML_TAG_RE.sub("", joined).strip()
    prefix = _speaker_prefix(plain)
    if prefix and prefix.group("text").strip():
        speaker = _clean_speaker(prefix.group("speaker"))
        plain = prefix.group("text").strip()
    if not plain:
        return
    turns.append((speaker, plain))


def _turns_from_plain_text(text: str) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    current_speaker = UNKNOWN_SPEAKER
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines
        body = " ".join(part for part in current_lines if part).strip()
        if body:
            turns.append((current_speaker, body))
        current_lines = []

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        prefix = _speaker_prefix(line)
        if prefix:
            flush()
            current_speaker = _clean_speaker(prefix.group("speaker"))
            remainder = prefix.group("text").strip()
            current_lines = [remainder] if remainder else []
        elif _is_statement_label(line):
            # "Decision: ..." on its own line: a new statement by the speaker who is talking.
            flush()
            current_lines = [_without_lead(line)]
        else:
            current_lines.append(line)

    flush()
    return turns


def _clean_speaker(label: str) -> str:
    cleaned = clean_speaker(label)
    return cleaned if cleaned else UNKNOWN_SPEAKER


# ---------------------------------------------------------------------------- merged speaker check
# Turns are split once, at upload, and stored. A transcript read with older rules (before names
# such as "Dr. Jürgen Weiß" were recognised), or a line holding several speakers, keeps another
# speaker's "Name:" inside one stored turn. Those statements are not lost from the text, but they
# are attributed to the wrong speaker and a labelled statement after them is not read on its own.
# The check below finds such places in stored turns, so the review can say so instead of reporting
# that the findings match the transcript.
_EMBEDDED_TIME = r"[\[(]?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?[\])]?"
# "… [00:03:05] Zoë Brandt: Decision: …" - a time stamp, then a name: strong evidence.
_TIMED_SPEAKER_RE = re.compile(rf"(?<!\S){_EMBEDDED_TIME}\s*(?:[-\u2013\u2014]\s*)?(?P<name>{SPEAKER_NAME}):\s+\S")
# "… by Friday. Petra Schäfer: Agreed …" - a sentence end, then something that looks like a name.
_SENTENCE_SPEAKER_RE = re.compile(rf"(?<=[.!?\u2026])[\"\u201d\u2019)]?\s+(?P<name>{SPEAKER_NAME}):\s+\S")
_TITLE_WORD_RE = re.compile(r"(?i)^(?:dr|prof|dipl|ing|mag|mr|mrs|ms|herr|frau)\b")
_NAME_PARTICLES = frozenset({"von", "van", "de", "der", "den", "zu", "di", "da", "le", "la", "del", "dos", "du"})
# Headings that are written like names but introduce a statement.
_HEADINGS = frozenset(
    {"next steps", "action items", "summary", "note", "notes", "agenda", "zusammenfassung", "notiz", "notizen", "tagesordnung", "fazit"}
)


# German capitalises nouns, so "Das Ziel:" or "Unser Vorschlag:" look like names; so do English
# headings like "The Plan:". A name does not start with an article, a pronoun or one of these words.
_NOT_A_NAME_START = frozenset(
    {
        "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "eines", "unser", "unsere", "mein", "meine", "ihr", "ihre",
        "wir", "ich", "sie", "es", "dieser", "diese", "dieses", "kein", "keine", "the", "a", "an", "our", "my", "your", "their",
        "this", "that", "these", "we", "i", "it", "key", "main", "open", "next", "action", "wichtig", "offene", "offener",
    }
)


def _looks_like_person(name: str, known: set[str]) -> bool:
    words = name.split()
    if name.casefold() in known:
        return True
    if not 2 <= len(words) <= 5 or words[0].casefold() in _NOT_A_NAME_START:
        return False
    return any(_TITLE_WORD_RE.match(word) for word in words) or all(
        word[0].isupper() or _TITLE_WORD_RE.match(word) or word.casefold() in _NAME_PARTICLES for word in words
    )


def merged_speaker_changes(sections: list[dict[str, object]]) -> int:
    """How many speaker changes stored turns hold inside their text (0 when turns are clean).

    ``sections`` are stored speaker turns (``speaker_role``, ``content``). Finding labels and
    headings ("Decision:", "Nächste Schritte:", "Summary:") are not speakers and are not counted.
    """
    known = {
        clean_speaker(str(section.get("speaker_role") or "")).casefold()
        for section in sections
        if str(section.get("speaker_role") or "") not in ("", UNKNOWN_SPEAKER)
    }
    count = 0
    for section in sections:
        text = normalize_text(str(section.get("content") or ""))
        # One speaker change per name: "Dr. Jürgen Weiß:" is not counted again as "Jürgen Weiß:".
        ends: set[int] = set()
        for match in _TIMED_SPEAKER_RE.finditer(text):
            name = clean_speaker(match.group("name"))
            if match.start() > 0 and not _is_label(name) and name.casefold() not in _HEADINGS:
                ends.add(match.end("name"))
        for match in _SENTENCE_SPEAKER_RE.finditer(text):
            name = clean_speaker(match.group("name"))
            if _is_label(name) or name.casefold() in _HEADINGS or not _looks_like_person(name, known):
                continue
            ends.add(match.end("name"))
        count += len(ends)
    return count
