"""BAP-13: the content language of generated customer documents.

One contract for every generated artifact (Discovery 1, Discovery 2, Master Presentation V1 and
V2, follow-up email): English (``en``) or German (``de``). English is the default and keeps its
existing behaviour. The language is chosen when Discovery 1 is generated and is then carried by
every approved version that later artifacts are built from, so a journey stays in one language
and an artifact is never written in a language its sources were not approved in.

Input material (client information, website research, transcripts, notes) may be in either
language; only what is generated follows the selected language. Facts, names and terms that come
from the sources are kept as they are.
"""

from __future__ import annotations

import re
from typing import Any, Literal

ContentLanguage = Literal["en", "de"]
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "de")
DEFAULT_LANGUAGE = "en"
LANGUAGE_NAMES = {"en": "English", "de": "German"}


class UnsupportedLanguage(ValueError):
    """A language that is not ``en`` or ``de``. ``code`` is API-safe."""

    code = "LANGUAGE_UNSUPPORTED"


def normalize_language(value: Any, *, default: str = DEFAULT_LANGUAGE) -> str:
    """``en`` or ``de``; ``default`` when nothing was given. Anything else is refused, not guessed."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    text = str(value).strip().lower()
    if text not in SUPPORTED_LANGUAGES:
        raise UnsupportedLanguage(f"Unsupported language '{value}'. Use 'en' or 'de'.")
    return text


def stored_language(value: Any) -> str:
    """The language of something already stored. Anything saved before BAP-13 is English."""
    text = str(value or "").strip().lower()
    return text if text in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def is_german(language: Any) -> bool:
    return stored_language(language) == "de"


# PR #13 review: statements are used in the words they were confirmed in. When their language is
# not the document's, that is made visible - never translated silently. The check counts common
# function words, which every sentence has and which do not overlap between the two languages.
_ENGLISH_WORDS = frozenset(
    "the a and or of to on at for with from by is are was were be been will would should could must can "
    "one two three four five six seven eight nine ten has have had do does did get gets need needs want wants "
    "this that these those it its we our you your they their not no all any each every more most than then "
    "there which who what when where how if as into out up about after before over under".split()
)
_GERMAN_WORDS = frozenset(
    "der die das den dem des ein eine einen einem einer eines und oder zu im am auf für mit von vom zum "
    "zwei drei vier fünf sechs sieben acht neun zehn hat haben hatte "
    "zur bei ist sind war waren wird werden wurde soll sollen muss müssen kann können nicht kein keine alle "
    "jede jeder jedes mehr als dann wenn wie was wer wo dass sich wir unser unsere sie ihr ihre es aus nach "
    "über unter vor bis noch auch nur schon sehr".split()
)
_WORD = re.compile(r"[a-zäöüß]+", re.IGNORECASE)


def detect_language(text: Any) -> str | None:
    """``en`` or ``de`` when a text clearly reads as one of them; None when it is too short or unclear."""
    words = [word.lower() for word in _WORD.findall(str(text or ""))]
    if len(words) < 3:
        return None
    english = sum(word in _ENGLISH_WORDS for word in words)
    german = sum(word in _GERMAN_WORDS for word in words) + sum(1 for word in words if any(letter in word for letter in "äöüß"))
    # Two markers that outweigh the other language - or, in a sentence of four words or more, one
    # marker and none of the other language at all.
    if (english >= 2 and english > 1.5 * german) or (english == 1 and german == 0 and len(words) >= 4):
        return "en"
    if (german >= 2 and german > 1.5 * english) or (german == 1 and english == 0 and len(words) >= 4):
        return "de"
    return None


def in_other_language(text: Any, language: Any) -> bool:
    """A text that clearly reads as the other supported language than ``language``."""
    detected = detect_language(text)
    return detected is not None and detected != stored_language(language)
