"""Render a Master Presentation into the artifact bundle the pipeline already consumes.

Same bundle as every other deck (``deck.pptx``, ``deck.pdf``, ``manifest.json``, one PNG per
slide), so publication, filing, previews and downloads are shared.

* PPTX      the verified canonical deck with the appendix slides, the closing slide last (``assembly``)
* PDF       that PPTX converted by LibreOffice headless (``office``)
* previews  the pages of that PDF, one PNG per slide

The assembled PPTX is the single rendering source: the PDF and the previews show exactly the
file that is downloaded. Nothing is reported as rendered unless the canonical slides are proven
unchanged and the PDF has one page per slide in the deck's own font; there is no fallback.
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from services.presentation.master_deck import office
from services.presentation.master_deck.assembly import AssemblyError, assemble, slide_order
from services.presentation.master_deck.layouts import LayoutError
from services.presentation.master_deck.plan import CANONICAL_LAYOUT
from services.presentation.master_deck.registry import MasterDeckError, get_master, verify_master


class MasterRenderError(RuntimeError):
    code = "MASTER_PRESENTATION_RENDER_FAILED"
    retryable = False


def split_slide_specs(slide_specs: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], bool]:
    """Master id, appendix contents and slide order of a stored Master Presentation.

    The slides are in deck order: the canonical slides, the appendix, the canonical closing slide
    last (``closing_last`` True). A plan stored before BAP-4 - every canonical slide first, then
    the appendix - is accepted as it was planned (``closing_last`` False). Anything else is rejected.
    """
    canonical = [spec for spec in slide_specs if spec.get("layoutId") == CANONICAL_LAYOUT]
    if not canonical:
        raise MasterRenderError("The deck has no canonical master slides")
    master_ids = {str(spec["content"].get("master_id")) for spec in canonical}
    if len(master_ids) != 1:
        raise MasterRenderError("The deck mixes slides of different master decks")
    master = get_master(master_ids.pop())
    appendix = [dict(spec["content"]) for spec in slide_specs if spec.get("layoutId") != CANONICAL_LAYOUT]
    layout = [
        ("canonical", spec["content"].get("master_slide")) if spec.get("layoutId") == CANONICAL_LAYOUT else ("appendix", None)
        for spec in slide_specs
    ]
    for closing_last in (True, False):
        expected = [(kind, ref if kind == "canonical" else None) for kind, ref in slide_order(master, len(appendix), closing_last=closing_last)]
        if layout == expected:
            return master.master_id, appendix, closing_last
    raise MasterRenderError(
        f"A Master Presentation has the {master.slide_count} canonical slides in their original order, "
        "the appendix after them and the closing slide last"
    )


def render_master_bundle(slide_specs: list[dict[str, Any]]) -> bytes:
    try:
        master_id, appendix, closing_last = split_slide_specs(slide_specs)
        master = get_master(master_id)
        verify_master(master)
        total = master.slide_count + len(appendix)
        with tempfile.TemporaryDirectory(prefix="master-presentation-") as tmp:
            work = Path(tmp)
            pptx_path = work / "deck.pptx"
            assemble(master, appendix, pptx_path, closing_last=closing_last)
            pdf_path, previews = office.render_pptx(pptx_path, work, expected_slides=total)
            preview_names = [f"preview-{index:03d}.png" for index in range(1, total + 1)]
            bundle_manifest = {
                "validation": {"status": "VALID", "warnings": []},
                "engine": "borek_deck",
                "deckKind": "master_v1",
                "pdfEngine": "libreoffice",
                "previewEngine": "libreoffice+pdftoppm",
                "renderSource": "deck.pptx",
                "slideCount": total,
                "previews": preview_names,
                "master": {**master.identity(), "slideCount": master.slide_count, "canonicalSlidesVerified": True},
                "appendixSlideCount": len(appendix),
            }
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(pptx_path, "deck.pptx")
                archive.write(pdf_path, "deck.pdf")
                archive.writestr("manifest.json", json.dumps(bundle_manifest, ensure_ascii=False))
                for path, name in zip(previews, preview_names, strict=True):
                    archive.write(path, name)
            return buffer.getvalue()
    except office.OfficeRenderError as exc:
        error = MasterRenderError(str(exc))
        error.code = exc.code
        raise error from exc
    except (MasterDeckError, AssemblyError, LayoutError) as exc:
        raise MasterRenderError(f"{type(exc).__name__}: {exc}") from exc
