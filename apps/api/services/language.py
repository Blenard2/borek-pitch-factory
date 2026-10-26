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
