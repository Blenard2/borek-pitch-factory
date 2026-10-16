"""ES-1 — validate and normalize transcript uploads.

Accepts .txt / .vtt / .srt / .docx and rejects every other format with a
user-facing error. Speaker-turn splitting is ES-2.

Content is never required to follow a structure: time stamps, speaker names and finding labels
are all optional. What is checked are technical limits (size, length), the format (the file must
really be text, captions or a Word document) and that some text remains after normalization.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.table import Table

ALLOWED_TRANSCRIPT_EXTENSIONS = frozenset({".txt", ".vtt", ".srt", ".docx"})

# Technical limits. A one-hour meeting is roughly 50-100 KB of text; these leave room for long
# meetings while keeping the upload bounded in memory and the analysis within the model's context.
MAX_TRANSCRIPT_BYTES = 10 * 1024 * 1024
MAX_TRANSCRIPT_CHARACTERS = 500_000
# A .docx is a zip archive; its unpacked size is bounded too, so a small archive cannot expand without limit.
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024

# Browsers report the same file under different MIME types, depending on the system and the browser.
# The type only has to be plausible for the extension; the content is validated separately.
_TEXT_MIME_TYPES = frozenset(
    {
        "text/plain", "text/vtt", "text/x-vtt", "text/srt", "text/x-srt",
        "application/x-subrip", "application/x-srt", "application/srt",
    }
)
_DOCX_MIME_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword", "application/zip", "application/x-zip-compressed",
    }
)
_GENERIC_MIME_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

_SRT_INDEX_RE = re.compile(r"^\d+$")
# "00:00:01.000 --> 00:00:04.000" (VTT), "00:00:01,000 --> ..." (SRT), and the short forms
# "00:01.000" (WebVTT without hours) and "0:00:01,000" (one-digit hour).
_CUE_TIME = r"(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}"
_TIMESTAMP_RE = re.compile(rf"^{_CUE_TIME}\s*-->\s*{_CUE_TIME}")
_HTML_TAG_RE = re.compile(r"</?[^>]+>")
# Control characters other than tab, line feed and carriage return do not occur in transcript text.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class TranscriptIngestionError(ValueError):
    """Rejected upload. ``user_message`` is safe to return to the client."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@dataclass(frozen=True, slots=True)
class TranscriptIngestionResult:
    filename: str
    extension: str
    normalized_text: str


def transcript_mime_allowed(extension: str, mime_type: str | None) -> bool:
    """Whether the MIME type a browser sent is plausible for this transcript extension.

    Parameters such as ``; charset=utf-8`` and letter case are ignored. A type that belongs to
    another kind of file (an image, a PDF, a Word file under .txt) is not accepted.
    """
    base = (mime_type or "").split(";", 1)[0].strip().lower()
    if base in _GENERIC_MIME_TYPES:
        return True
    extension = extension.lower()
    if extension == ".docx":
        return base in _DOCX_MIME_TYPES
    if extension in {".txt", ".vtt", ".srt"}:
        return base in _TEXT_MIME_TYPES
    return False


def ingest_transcript(filename: str, content: bytes) -> TranscriptIngestionResult:
    if not filename or not filename.strip():
        raise TranscriptIngestionError(
            "Please upload a file named with one of these extensions: .txt, .vtt, .srt, or .docx."
        )

    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_TRANSCRIPT_EXTENSIONS:
        displayed = extension if extension else "no extension"
        raise TranscriptIngestionError(
            f"Unsupported transcript format ({displayed}). "
            "Upload a .txt, .vtt, .srt, or .docx file."
        )

    if not content:
        raise TranscriptIngestionError(
            "The uploaded file is empty. Export the transcript again and retry."
        )
    if len(content) > MAX_TRANSCRIPT_BYTES:
        raise TranscriptIngestionError(transcript_too_large_message())

    if extension == ".txt":
        text = _decode_text_bytes(content)
    elif extension == ".vtt":
        text = _normalize_vtt(_decode_text_bytes(content))
    elif extension == ".srt":
        text = _normalize_srt(_decode_text_bytes(content))
    else:
        text = _normalize_docx(content)

    normalized = _collapse_blank_lines(text).strip()
    if not normalized:
        raise TranscriptIngestionError(
            "The file was read but contained no transcript text. "
            "Check the export and upload a .txt, .vtt, .srt, or .docx file."
        )
    if len(normalized) > MAX_TRANSCRIPT_CHARACTERS:
        raise TranscriptIngestionError(
            f"The transcript is too long to analyse ({len(normalized):,} characters; the limit is "
            f"{MAX_TRANSCRIPT_CHARACTERS:,}). Split the meeting into parts and upload each part."
        )

    return TranscriptIngestionResult(
        filename=Path(filename).name,
        extension=extension,
        normalized_text=normalized,
    )


def transcript_too_large_message() -> str:
    return (
        f"The transcript file is larger than {MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MB. "
        "Upload the transcript text only, or split a very long meeting into parts."
    )


def _decode_text_bytes(content: bytes) -> str:
    # Notepad's "Unicode" export is UTF-16 with a byte order mark.
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return _checked_text(content.decode("utf-16"))
        except UnicodeDecodeError:
            pass
    else:
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                return _checked_text(content.decode(encoding))
            except UnicodeDecodeError:
                continue
    raise TranscriptIngestionError(
        "The file is not valid text. Save it as UTF-8 .txt, .vtt, or .srt, or upload a .docx file."
    )


def _checked_text(text: str) -> str:
    """Binary files (an image or archive renamed to .txt) decode as cp1252 too; their control characters give them away."""
    if _CONTROL_CHAR_RE.search(text):
        raise TranscriptIngestionError(
            "The file does not contain plain text. It may be a different file type renamed to .txt, .vtt, or .srt. "
            "Export the transcript as text, or upload a .docx file."
        )
    return text


def _normalize_vtt(raw: str) -> str:
    lines: list[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("WEBVTT"):
            continue
        if stripped.upper().startswith("KIND:") or stripped.upper().startswith("LANGUAGE:"):
            continue
        if stripped.startswith("NOTE"):
            continue
        if _TIMESTAMP_RE.match(stripped):
            continue
        if _SRT_INDEX_RE.match(stripped):
            continue
        lines.append(_HTML_TAG_RE.sub("", stripped).strip())
    return "\n".join(line for line in lines if line)


def _normalize_srt(raw: str) -> str:
    lines: list[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if _SRT_INDEX_RE.match(stripped):
            continue
        if _TIMESTAMP_RE.match(stripped):
            continue
        lines.append(_HTML_TAG_RE.sub("", stripped).strip())
    return "\n".join(line for line in lines if line)


def _normalize_docx(content: bytes) -> str:
    unreadable = TranscriptIngestionError(
        "The .docx file could not be read. Export it again from Word and retry."
    )
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise TranscriptIngestionError(transcript_too_large_message())
        document = Document(io.BytesIO(content))
    except TranscriptIngestionError:
        raise
    except Exception:
        raise unreadable from None

    # Paragraphs and tables in document order: some meeting tools export the conversation as a table.
    lines: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                cells: list[str] = []
                for cell in row.cells:
                    text = " ".join(cell.text.split())
                    if text and text not in cells:  # merged cells repeat their text
                        cells.append(text)
                if cells:
                    lines.append(" ".join(cells))
        elif block.text and block.text.strip():
            lines.append(block.text.strip())
    return "\n".join(lines)


def _collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)
