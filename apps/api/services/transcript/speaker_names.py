"""Speaker names and finding labels, shared by the turn splitter and the fixture extractor.

Both read the same ``Name: text`` lines, so they must agree on what a speaker name is and on
which words are finding labels ("Requirement:", "Entscheidung:") rather than names.

A speaker name starts with a letter of any script ("Jürgen", "Øyvind", "Zoë") and continues with
letters, digits, combining accents, spaces and the punctuation names and titles use:
"Dr. Jürgen Weiß", "Petra Schäfer", "Sarah O'Neill-Hughes", "Dipl.-Ing. Kühn", "Speaker 2 (Sales)".
"""

from __future__ import annotations

import re
import unicodedata

# Combining accents: a decomposed "u" + U+0308 is the same name as "ü".
_COMBINING = "̀-ͯ᪰-᫿᷀-᷿⃐-⃿︠-︯"
# Apostrophes ' ’ ʼ, hyphens - ‐, the no-break space, dots, underscores and brackets.
_NAME_PUNCTUATION = "  .'’ʼ\\-‐_()"
# ``[^\W\d_]`` is a letter of any script; ``[^\W_]`` a letter or digit.
SPEAKER_NAME = rf"[^\W\d_](?:[^\W_]|[{_COMBINING}]|[{_NAME_PUNCTUATION}]){{0,80}}"

# Finding labels, English and German (BAP-13). The fixture extractor reads the statement after
# them; the turn splitter must never mistake one for a speaker.
FINDING_LABELS: tuple[tuple[str, str], ...] = (
    ("requirements", r"requirements?|anforderung(?:en)?"),
    ("challenges", r"challenges?|herausforderung(?:en)?"),
    ("priorities", r"priorit(?:y|ies)|priorität(?:en)?"),
    ("opportunities", r"opportunit(?:y|ies)|chancen?|potenziale?"),
    ("discussed_solutions", r"discussed solutions?|besprochene lösung(?:en)?|lösungsansatz|lösungsansätze"),
    ("decisions", r"decisions?|entscheidung(?:en)?|beschlüsse|beschluss"),
    ("follow_ups", r"follow[- ]?ups?|nächster? schritte?|nächste schritte|to-?dos?"),
)
_FINDING_LABEL_RE = re.compile(r"(?i)(?:" + "|".join(label for _, label in FINDING_LABELS) + r")")


def normalize_text(text: str) -> str:
    """Composed accents (NFC), so the same name always reads the same."""
    return unicodedata.normalize("NFC", text)


_NAME_EQUIVALENTS = str.maketrans({"_": " ", " ": " ", "’": "'", "ʼ": "'", "‐": "-"})


def clean_speaker(label: str) -> str:
    """The speaker name as shown and stored: composed accents, single spaces, plain apostrophes
    and hyphens, so "O’Neill" and "O'Neill" are the same speaker."""
    return " ".join(normalize_text(label).translate(_NAME_EQUIVALENTS).split())


def is_finding_label(text: str) -> bool:
    """True for "Requirement", "Entscheidung", "Nächste Schritte" and the other finding labels."""
    return _FINDING_LABEL_RE.fullmatch(" ".join(normalize_text(text).split())) is not None
