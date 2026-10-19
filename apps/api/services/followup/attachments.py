"""BAP-3: validation of files the owner uploads as additional follow-up email attachments.

An uploaded file is an attachment the owner chose. It is never read for its content beyond the
checks below, and nothing in it is evidence for a statement in the email.

What is checked, on the bytes and not on the name or the browser's MIME type alone:

    name        reduced to a safe display name: no folders, no control characters, no characters
                that mail programs or file systems treat specially, at most 120 characters
    type        one of the supported extensions, and content that is that kind of file:
                a PDF header, an Office Open XML package with its main part, a PNG or JPEG
                signature, or decodable text without control characters
    size        not empty, at most MAX_EXTERNAL_ATTACHMENT_BYTES; a ZIP-based Office file may not
                unpack to more than MAX_UNPACKED_BYTES
    MIME type   a type the browser sends for this kind of file, or a generic one

Pure functions: they read their arguments and return plain data.
"""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

# Limits. A follow-up email is sent by the owner's own mail program, and providers limit the size
# of the MESSAGE (commonly 25 MB), not of the files: in a message every attachment is Base64
# encoded, about 4/3 of its size plus line breaks. The 25 MB policy is therefore applied to the
# exported email: selections are checked with an estimate of the encoded size (encoded_size), and
# the exported .eml itself must not exceed MAX_EMAIL_BYTES.
MAX_EXTERNAL_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_EXTERNAL_ATTACHMENTS = 10
MAX_EMAIL_BYTES = 25 * 1024 * 1024
# Headers, the text in its three encodings and the MIME structure stay well below this.
MESSAGE_ALLOWANCE_BYTES = 128 * 1024
PART_OVERHEAD_BYTES = 1024
# Kept for callers: the room the attachments have once encoded.
MAX_TOTAL_ATTACHMENT_BYTES = MAX_EMAIL_BYTES - MESSAGE_ALLOWANCE_BYTES
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_NAME_CHARACTERS = 120

# extension -> (canonical MIME type, MIME types browsers send for it, content kind)
SUPPORTED: dict[str, tuple[str, frozenset[str], str]] = {
    ".pdf": ("application/pdf", frozenset({"application/pdf", "application/x-pdf"}), "pdf"),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword", "application/zip", "application/x-zip-compressed"}),
        "word",
    ),
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel", "application/zip", "application/x-zip-compressed"}),
        "excel",
    ),
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        frozenset({"application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/vnd.ms-powerpoint", "application/zip", "application/x-zip-compressed"}),
        "powerpoint",
    ),
    ".png": ("image/png", frozenset({"image/png"}), "png"),
    ".jpg": ("image/jpeg", frozenset({"image/jpeg", "image/pjpeg"}), "jpeg"),
    ".jpeg": ("image/jpeg", frozenset({"image/jpeg", "image/pjpeg"}), "jpeg"),
    ".txt": ("text/plain", frozenset({"text/plain"}), "text"),
    ".csv": ("text/csv", frozenset({"text/csv", "text/plain", "application/csv", "application/vnd.ms-excel", "text/x-csv"}), "text"),
}
SUPPORTED_EXTENSIONS = tuple(SUPPORTED)
_GENERIC_MIME = frozenset({"", "application/octet-stream", "binary/octet-stream"})
_OFFICE_MAIN_PART = {"word": "word/document.xml", "excel": "xl/workbook.xml", "powerpoint": "ppt/presentation.xml"}
# Characters with a special meaning in file systems, shells or mail headers.
_UNSAFE_CHARACTERS = re.compile(r'[<>:"/\\|?*;\[\]{}$`\x00-\x1f\x7f]')
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ExternalAttachmentError(ValueError):
    """Rejected upload. ``code`` and ``user_message`` are safe to return to the client."""

    def __init__(self, user_message: str, code: str = "EMAIL_ATTACHMENT_INVALID") -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message


@dataclass(frozen=True, slots=True)
class ValidatedAttachment:
    file_name: str
    extension: str
    mime_type: str
    size_bytes: int
    sha256: str


def supported_list() -> str:
    return ", ".join(extension.removeprefix(".").upper() for extension in SUPPORTED_EXTENSIONS if extension != ".jpeg")


def safe_file_name(raw: str | None) -> str:
    """A display name that is safe in a mail header and on any file system. Never used as a path."""
    name = str(raw or "")
    # The last component under either separator: "..\\..\\x.pdf" and "/etc/x.pdf" become "x.pdf".
    name = PureWindowsPath(PurePosixPath(name.replace("\\", "/")).name).name
    name = unicodedata.normalize("NFC", name)
    name = _UNSAFE_CHARACTERS.sub("_", name)
    name = " ".join(name.split()).strip(" .")
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    stem = stem.strip(" ._") or "attachment"
    extension = extension.lower()
    budget = MAX_NAME_CHARACTERS - (len(extension) + 1 if extension else 0)
    stem = stem[:budget].rstrip(" .")
    return f"{stem}.{extension}" if extension else stem


def validate_external_attachment(raw_name: str | None, mime_type: str | None, content: bytes) -> ValidatedAttachment:
    name = safe_file_name(raw_name)
    extension = "." + name.rpartition(".")[2] if "." in name else ""
    if extension not in SUPPORTED:
        shown = extension or "no extension"
        raise ExternalAttachmentError(
            f"{name}: this file type ({shown}) cannot be attached. Supported: {supported_list()}.", "EMAIL_ATTACHMENT_UNSUPPORTED_TYPE"
        )
    canonical, accepted, kind = SUPPORTED[extension]
    base_mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if base_mime not in _GENERIC_MIME and base_mime not in accepted:
        raise ExternalAttachmentError(f"{name}: the file type {base_mime} does not match a {extension} file.", "EMAIL_ATTACHMENT_UNSUPPORTED_TYPE")
    if not content:
        raise ExternalAttachmentError(f"{name} is empty.", "EMAIL_ATTACHMENT_EMPTY")
    if len(content) > MAX_EXTERNAL_ATTACHMENT_BYTES:
        raise ExternalAttachmentError(
            f"{name} is larger than {MAX_EXTERNAL_ATTACHMENT_BYTES // (1024 * 1024)} MB.", "EMAIL_ATTACHMENT_TOO_LARGE"
        )
    _check_content(name, kind, content)
    return ValidatedAttachment(
        file_name=name, extension=extension, mime_type=canonical, size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest()
    )


def _check_content(name: str, kind: str, content: bytes) -> None:
    invalid = ExternalAttachmentError(f"{name} is damaged or is not the kind of file its name says.", "EMAIL_ATTACHMENT_CORRUPT")
    if kind == "pdf":
        if not content.lstrip(b"\x00\t\r\n ")[:5] == b"%PDF-" or b"%%EOF" not in content[-2048:]:
            raise invalid
    elif kind in _OFFICE_MAIN_PART:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                if sum(entry.file_size for entry in entries) > MAX_UNPACKED_BYTES:
                    raise ExternalAttachmentError(f"{name} unpacks to more than {MAX_UNPACKED_BYTES // (1024 * 1024)} MB.", "EMAIL_ATTACHMENT_TOO_LARGE")
                names = {entry.filename for entry in entries}
                if "[Content_Types].xml" not in names or _OFFICE_MAIN_PART[kind] not in names:
                    raise invalid
        except ExternalAttachmentError:
            raise
        except (zipfile.BadZipFile, ValueError, OSError):
            raise invalid from None
    elif kind == "png":
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise invalid
    elif kind == "jpeg":
        if not content.startswith(b"\xff\xd8\xff"):
            raise invalid
    elif kind == "text":
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                text = content.decode(encoding)
            except UnicodeDecodeError:
                continue
            if _CONTROL_CHARACTERS.search(text):
                raise invalid
            return
        raise invalid


def encoded_size(raw_bytes: int) -> int:
    """Bytes one attachment of ``raw_bytes`` takes in the exported email (Base64, 76-character lines)."""
    base64 = 4 * ((raw_bytes + 2) // 3)
    lines = (base64 + 75) // 76
    return base64 + 2 * lines + PART_OVERHEAD_BYTES


def attachments_fit(raw_sizes: list[int]) -> bool:
    """Whether attachments of these sizes, encoded, leave the email within MAX_EMAIL_BYTES."""
    return sum(encoded_size(size) for size in raw_sizes) <= MAX_TOTAL_ATTACHMENT_BYTES


def unique_names(names: list[str]) -> list[str]:
    """The same names, made unique for one email: a repeated "a.pdf" becomes "a (2).pdf"."""
    seen: set[str] = set()
    result = []
    for name in names:
        stem, dot, extension = name.rpartition(".")
        if not dot:
            stem, extension = name, ""
        candidate, counter = name, 1
        while candidate.casefold() in seen:
            counter += 1
            candidate = f"{stem} ({counter}).{extension}" if extension else f"{stem} ({counter})"
        seen.add(candidate.casefold())
        result.append(candidate)
    return result
