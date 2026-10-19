"""Registry of canonical master decks: trusted, versioned repository assets.

A master deck is the immutable base of every Master Presentation. It is addressed by id only -
never by a path a caller supplies - and it is verified (checksum, slide count) before
every use. A missing, replaced or damaged master is an explicit error; nothing is substituted.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"
DEFAULT_MASTER_ID = "borek_ai_tech_en_v1"
_SLIDE_PART = re.compile(r"ppt/slides/slide\d+\.xml")


class MasterDeckError(RuntimeError):
    """The canonical master deck is unknown, missing or not the registered file."""

    code = "MASTER_DECK_INVALID"
    retryable = False


@dataclass(frozen=True)
class MasterDeck:
    master_id: str
    master_version: str
    sha256: str
    slide_count: int
    language: str

    @property
    def directory(self) -> Path:
        return ASSETS / self.master_id

    @property
    def pptx_path(self) -> Path:
        return self.directory / "deck.pptx"

    def identity(self) -> dict[str, str]:
        return {"master_id": self.master_id, "master_version": self.master_version, "master_sha256": self.sha256}


# The checksum is the trust anchor: it is the SHA-256 of the supplied "Ai Tech Borek
# Presentation EN.pptx" and lives in code, so replacing the file (or its manifest) is detected.
_REGISTRY: dict[str, MasterDeck] = {
    DEFAULT_MASTER_ID: MasterDeck(
        master_id=DEFAULT_MASTER_ID,
        master_version="1.0",
        sha256="2c23670e46dc2ce71d3527b0acc846cad54572d9817977ec26c3efe17f51dd5a",
        slide_count=26,
        language="en",
    ),
}


def get_master(master_id: str = DEFAULT_MASTER_ID) -> MasterDeck:
    master = _REGISTRY.get(str(master_id))
    if master is None:
        raise MasterDeckError(f"Unknown master deck '{master_id}'")
    return master


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pptx_slide_count(path: Path) -> int:
    try:
        with zipfile.ZipFile(path) as package:
            return sum(1 for name in package.namelist() if _SLIDE_PART.fullmatch(name))
    except (OSError, zipfile.BadZipFile) as exc:
        raise MasterDeckError(f"Master deck is not a readable PowerPoint package: {exc}") from exc


def load_manifest(master: MasterDeck) -> dict:
    """Slide titles of the deck, written by ``scripts/build_master_deck_manifest.py``."""
    path = master.directory / "manifest.json"
    if not path.is_file():
        raise MasterDeckError(f"Master deck {master.master_id} has no manifest")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (manifest.get("master_id"), manifest.get("sha256")) != (master.master_id, master.sha256):
        raise MasterDeckError(f"Master deck {master.master_id}: manifest does not belong to the registered file")
    if len(manifest.get("slides") or []) != master.slide_count:
        raise MasterDeckError(f"Master deck {master.master_id}: manifest does not list {master.slide_count} slides")
    return manifest


def verify_master(master: MasterDeck) -> dict:
    """Check the stored deck against the registry. Returns the manifest; raises on any mismatch."""
    path = master.pptx_path
    if not path.is_file():
        raise MasterDeckError(f"Master deck {master.master_id} is missing")
    actual = file_sha256(path)
    if actual != master.sha256:
        raise MasterDeckError(
            f"Master deck {master.master_id} does not match its registered checksum (found {actual[:12]}…)"
        )
    count = pptx_slide_count(path)
    if count != master.slide_count:
        raise MasterDeckError(f"Master deck {master.master_id} has {count} slides, expected {master.slide_count}")
    return load_manifest(master)


@lru_cache(maxsize=4)
def canonical_slide_titles(master_id: str = DEFAULT_MASTER_ID) -> tuple[str, ...]:
    return tuple(str(slide["title"]) for slide in load_manifest(get_master(master_id))["slides"])


# ---------------------------------------------------------------------------------- slide order (BAP-4)

CLOSING_ROLE = "closing"
# Identity of the slide order below; recorded with every generation so that a deck built in the
# earlier order (closing slide before the appendix) is never reused as the current one.
DECK_LAYOUT = "closing-last:v1"


def closing_slide(master: MasterDeck) -> int | None:
    """Number of the canonical slide that closes every Master Presentation, as the manifest declares it."""
    numbers = [int(slide["number"]) for slide in load_manifest(master)["slides"] if slide.get("role") == CLOSING_ROLE]
    if len(numbers) > 1:
        raise MasterDeckError(f"Master deck {master.master_id} declares more than one closing slide")
    return numbers[0] if numbers else None


def deck_order(master: MasterDeck, appendix_count: int) -> list[tuple[str, int]]:
    """The slides of a Master Presentation in order: ("canonical", master slide number) or ("appendix", index).

    Every canonical slide in its original order, the appendix after them, and the closing slide
    last - whatever the number of appendix slides. Each slide appears exactly once.
    """
    closing = closing_slide(master)
    order = [("canonical", number) for number in range(1, master.slide_count + 1) if number != closing]
    order.extend(("appendix", index) for index in range(appendix_count))
    if closing is not None:
        order.append(("canonical", closing))
    return order


def first_appendix_position(master: MasterDeck) -> int:
    """1-based position of the first appendix slide in the deck."""
    return next(position for position, (kind, _ref) in enumerate(deck_order(master, 1), start=1) if kind == "appendix")
