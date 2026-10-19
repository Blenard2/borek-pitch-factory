"""Write ``manifest.json`` for a registered canonical master deck.

The manifest lists the slide titles, read from the deck itself. Previews and the PDF are not
stored: they are rendered from the assembled presentation on every generation.

    python scripts/build_master_deck_manifest.py [master_id]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "apps" / "api")]

from pptx import Presentation  # noqa: E402

from services.presentation.master_deck.registry import (  # noqa: E402
    DEFAULT_MASTER_ID,
    MasterDeckError,
    file_sha256,
    get_master,
)

# Title text box per slide: the first text after the kicker; cover, dividers and closing differ.
_TITLE_OVERRIDES = {1: "Your AI department. Delivered, not built.", 26: "Let’s talk"}
# BAP-4: the slide that closes every Master Presentation, after any appendix. Declared here for
# this master, so the generation never finds it by position.
_ROLES = {26: "closing"}


def slide_title(slide, number: int) -> str:
    if number in _TITLE_OVERRIDES:
        return _TITLE_OVERRIDES[number]
    texts = [shape.text_frame.text.strip() for shape in slide.shapes if shape.has_text_frame and shape.text_frame.text.strip()]
    return " ".join(texts[1].split()) if len(texts) > 1 else f"Slide {number}"


def main() -> None:
    master = get_master(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MASTER_ID)
    if file_sha256(master.pptx_path) != master.sha256:
        raise MasterDeckError("The stored deck is not the registered file; refusing to write a manifest for it")
    deck = Presentation(str(master.pptx_path))
    slides = []
    for number, slide in enumerate(deck.slides, start=1):
        entry = {"number": number, "title": slide_title(slide, number)}
        if number in _ROLES:
            entry["role"] = _ROLES[number]
        slides.append(entry)
    manifest = {
        "master_id": master.master_id,
        "master_version": master.master_version,
        "file": "deck.pptx",
        "sha256": master.sha256,
        "slide_count": len(slides),
        "language": master.language,
        "slides": slides,
    }
    target = master.directory / "manifest.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {target.relative_to(ROOT)} ({len(slides)} slides)")


if __name__ == "__main__":
    main()
