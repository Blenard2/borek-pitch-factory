"""Speaker turns for names in any script, and the check for speaker changes left inside one turn.

Audit 2026-10-27 (BUG-01): "Dr. Jürgen Weiß:" and "Zoë Brandt:" were not recognised as speakers.
Their lines were appended to the previous speaker's turn, five of seven findings were lost and the
review still said the findings matched the transcript.
"""

from __future__ import annotations

import io
import unicodedata
from pathlib import Path

import pytest
from docx import Document

from services.meeting.extraction import extract_meeting_findings
from services.transcript.speaker_names import clean_speaker, is_finding_label
from services.transcript.speaker_turns import UNKNOWN_SPEAKER, merged_speaker_changes, split_speaker_turns

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "transcripts"
GERMAN = FIXTURES / "de_erstgespraech_mueller_praezision.txt"
ENGLISH = FIXTURES / "en_first_meeting_harborview.txt"


def turns_of(name: str, text: str) -> list[tuple[str, str]]:
    return [(turn.speaker, turn.text) for turn in split_speaker_turns(name, text.encode("utf-8"))]


def sections(turns) -> list[dict]:
    return [{"section_index": turn.turn_index, "speaker_role": turn.speaker, "content": turn.text} for turn in turns]


def findings(turns, notes: str | None = None) -> dict[str, list[str]]:
    return extract_meeting_findings(sections=sections(turns), personal_notes=notes, live=False)[0]


# ------------------------------------------------------------------------ names in any script


@pytest.mark.parametrize(
    "name",
    [
        "Dr. Jürgen Weiß",
        "Petra Schäfer",
        "Müller",
        "Sarah O'Neill-Hughes",
        "Prof. Dr. Øyvind Bjørnstad",
        "Dipl.-Ing. Kühn",
        "Zoë Brandt",
        "José Núñez",
        "Łukasz Żółć",
        "Ödön Erdős",
        "Änne Müller-Lüdenscheidt",
        "Speaker 2 (Vertrieb)",
    ],
)
def test_a_speaker_name_in_any_script_starts_its_own_turn(name: str) -> None:
    assert turns_of("call.txt", f"Anna: Guten Morgen.\n{name}: Danke für die Einladung.\nAnna: Gern.") == [
        ("Anna", "Guten Morgen."),
        (name, "Danke für die Einladung."),
        ("Anna", "Gern."),
    ]


@pytest.mark.parametrize("stamp", ["[00:01:35] ", "00:01:35 ", "(01:35) ", "[01:35.250] - "])
def test_a_time_stamp_in_front_of_a_german_name_is_not_part_of_what_was_said(stamp: str) -> None:
    assert turns_of("call.txt", f"{stamp}Petra Schäfer: Die Preisdaten liegen in drei Systemen.") == [
        ("Petra Schäfer", "Die Preisdaten liegen in drei Systemen."),
    ]


def test_decomposed_accents_typographic_apostrophes_and_no_break_spaces_give_one_speaker() -> None:
    decomposed = unicodedata.normalize("NFD", "Jürgen Weiß")
    text = (
        f"{decomposed}: Erster Punkt.\n"
        "Jürgen Weiß: Zweiter Punkt.\n"
        "Sarah O’Neill-Hughes: Third point.\n"
        "Sarah O'Neill-Hughes: Fourth point.\n"
        "Lena Großmann: Fünfter Punkt.\n"
    )
    speakers = [speaker for speaker, _ in turns_of("call.txt", text)]
    assert speakers == ["Jürgen Weiß", "Jürgen Weiß", "Sarah O'Neill-Hughes", "Sarah O'Neill-Hughes", "Lena Großmann"]
    assert all(unicodedata.is_normalized("NFC", speaker) for speaker in speakers)
    assert clean_speaker("Sarah O’Neill_Hughes") == "Sarah O'Neill Hughes"


def test_umlaut_speakers_work_in_word_and_caption_files() -> None:
    document = Document()
    for paragraph in ("Dr. Jürgen Weiß: Wir brauchen schnellere Angebote.", "Petra Schäfer: Die Daten liegen verteilt."):
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    assert [turn.speaker for turn in split_speaker_turns("call.docx", buffer.getvalue())] == ["Dr. Jürgen Weiß", "Petra Schäfer"]
    srt = "1\n00:00:01,000 --> 00:00:03,000\nDr. Jürgen Weiß: Angebote in einem Tag.\n\n2\n00:00:04,000 --> 00:00:06,000\nZoë Brandt: Einverstanden.\n"
    assert turns_of("call.srt", srt) == [("Dr. Jürgen Weiß", "Angebote in einem Tag."), ("Zoë Brandt", "Einverstanden.")]
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v Petra Schäfer>Der Export kommt am Freitag.</v>\n"
    assert turns_of("call.vtt", vtt) == [("Petra Schäfer", "Der Export kommt am Freitag.")]


# ------------------------------------------------------------- finding labels are not speakers


@pytest.mark.parametrize(
    "label",
    ["Requirement", "Decision", "Follow-up", "Anforderung", "Herausforderung", "Priorität", "Chance", "Potenzial",
     "Besprochene Lösung", "Entscheidung", "Beschluss", "Nächste Schritte", "Nächster Schritt", "To-do"],
)
def test_a_finding_label_on_its_own_line_is_a_new_statement_of_the_same_speaker(label: str) -> None:
    turns = turns_of("call.txt", f"Dr. Jürgen Weiß: Das klingt sinnvoll.\n{label}: Wir starten im Dezember.")
    assert turns == [("Dr. Jürgen Weiß", "Das klingt sinnvoll."), ("Dr. Jürgen Weiß", f"{label}: Wir starten im Dezember.")]
    assert is_finding_label(label)


def test_a_clock_time_is_not_a_speaker_and_unlabelled_lines_continue_the_turn() -> None:
    assert turns_of("call.txt", "Lena Großmann: Um 14:30 habe ich Zeit.\n10:30 passt mir auch.\nund danach nicht mehr.") == [
        ("Lena Großmann", "Um 14:30 habe ich Zeit. 10:30 passt mir auch. und danach nicht mehr."),
    ]


def test_unlabelled_conversation_stays_one_unknown_turn_with_every_line() -> None:
    text = "Wir haben über Angebote gesprochen.\nDas dauert zu lange.\nWir prüfen einen Piloten."
    assert turns_of("call.txt", text) == [(UNKNOWN_SPEAKER, "Wir haben über Angebote gesprochen. Das dauert zu lange. Wir prüfen einen Piloten.")]


# ------------------------------------------------------------------- real meetings, end to end


def test_german_meeting_keeps_every_speaker_change_statement_and_finding() -> None:
    turns = split_speaker_turns(GERMAN.name, GERMAN.read_bytes())
    assert [turn.turn_index for turn in turns] == list(range(len(turns))), "turn identity: a stable, gapless index"
    assert [turn.speaker for turn in turns[2:]] == [
        "Lena Großmann", "Dr. Jürgen Weiß", "Dr. Jürgen Weiß", "Petra Schäfer", "Müller", "Sarah O'Neill-Hughes",
        "Dr. Jürgen Weiß", "Dr. Jürgen Weiß", "Petra Schäfer", "Müller", "Lena Großmann",
    ]
    # No statement is lost: every spoken line appears, whole, in exactly one turn.
    spoken = [line.split(": ", 1)[1] if line.startswith("[") else line for line in GERMAN.read_text(encoding="utf-8").splitlines()[2:] if line]
    joined = [turn.text for turn in turns]
    for line in spoken:
        assert sum(line in text for text in joined) == 1, line
    assert merged_speaker_changes(sections(turns)) == 0
    assert {category: len(items) for category, items in findings(turns).items()} == {
        "requirements": 1, "challenges": 1, "priorities": 1, "opportunities": 1, "discussed_solutions": 1, "decisions": 1, "follow_ups": 1,
    }
    assert findings(turns)["decisions"] == ["Wir starten im Dezember einen Pilot mit zehn echten Anfragen."]


def test_english_meeting_with_accented_names_keeps_all_findings_apart() -> None:
    turns = split_speaker_turns(ENGLISH.name, ENGLISH.read_bytes())
    assert [turn.speaker for turn in turns][1:] == [
        "Dr. Sarah O'Neill-Hughes", "Prof. Dr. Øyvind Bjørnstad", "Zoë Brandt", "Prof. Dr. Øyvind Bjørnstad",
        "Sarah O'Neill-Hughes", "Zoë Brandt", "José Núñez", "Speaker 2",
    ]
    assert turns[-1].text == "At 10:30 works for me."
    found = findings(turns)
    assert found["decisions"] == ["We will run a pilot with ten real quotes in December."]
    assert found["follow_ups"] == ["Zoë Brandt sends the pricing export by Friday 6 November."]
    assert sum(len(items) for items in found.values()) == 6
    assert merged_speaker_changes(sections(turns)) == 0


def test_the_audit_transcript_no_longer_collapses() -> None:
    audit = (
        "[00:00:05] Dr. Sarah O'Neill-Hughes: Thanks for joining. Our quotes take five days today.\n"
        "[00:00:40] Dr. Jürgen Weiß: Requirement: Quotes must reach the customer within one business day.\n"
        "[00:01:10] Zoë Brandt: Challenge: Pricing data lives in the ERP, three spreadsheets and email threads.\n"
        "[00:01:55] Dr. Jürgen Weiß: Priority: Start with the sales team before customer service.\n"
        "[00:02:30] Sarah O'Neill-Hughes: Discussed solution: An assistant that drafts the quote from the inquiry and past quotes.\n"
        "[00:03:05] Zoë Brandt: Decision: We will run a pilot with ten real quotes in December.\n"
        "[00:03:40] Dr. Jürgen Weiß: Follow-up: Zoë Brandt sends the pricing export by Friday 6 November.\n"
    )
    turns = split_speaker_turns("meeting_umlaut.txt", audit.encode("utf-8"))
    assert len(turns) == 7, "was 2 before the fix"
    assert sum(len(items) for items in findings(turns).values()) == 6, "was 1 before the fix"


# ------------------------------------------------- speaker changes left inside one stored turn


def test_stored_turns_from_the_old_parser_report_their_merged_speaker_changes() -> None:
    # The two turns the audit transcript was stored as before the fix.
    stored = [
        {"speaker_role": "Dr. Sarah O'Neill-Hughes", "content": "Thanks for joining. Our quotes take five days today. [00:00:40] Dr. Jürgen Weiß: Requirement: Quotes must reach the customer within one business day. [00:01:10] Zoë Brandt: Challenge: Pricing data lives in the ERP. [00:01:55] Dr. Jürgen Weiß: Priority: Start with the sales team."},
        {"speaker_role": "Sarah O'Neill-Hughes", "content": "Discussed solution: An assistant. [00:03:05] Zoë Brandt: Decision: We will run a pilot. [00:03:40] Dr. Jürgen Weiß: Follow-up: Zoë Brandt sends the export by Friday."},
    ]
    assert merged_speaker_changes(stored) == 5


def test_merged_speaker_changes_without_time_stamps_are_found_by_name_shape_or_known_speaker() -> None:
    assert merged_speaker_changes([{"speaker_role": UNKNOWN_SPEAKER, "content": "Hallo zusammen. Petra Schäfer: Danke. Dr. Jürgen Weiß: Gern."}]) == 2
    assert merged_speaker_changes([
        {"speaker_role": "Anna", "content": "Erster Punkt."},
        {"speaker_role": "Müller", "content": "Ja. Anna: Wirklich?"},
    ]) == 1


@pytest.mark.parametrize(
    "content",
    [
        "Wir treffen uns um 10:30. Nächste Schritte: Export schicken.",
        "Decision: go. Requirement: one day. Entscheidung: Pilot.",
        "The plan is simple: we start in May.",
        "Summary: all agreed. Next steps: send the export.",
        "Ein Angebot dauert fünf Tage. Das Ziel: ein Tag.",
    ],
)
def test_labels_headings_clock_times_and_ordinary_sentences_are_not_speaker_changes(content: str) -> None:
    assert merged_speaker_changes([{"speaker_role": "Anna", "content": content}]) == 0
