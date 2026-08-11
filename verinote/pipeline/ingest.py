# SPDX-License-Identifier: MPL-2.0
"""Source ingestion: register original sources and extraction text artifacts.

Extraction reads text, but facts cite original files. Text uploads are stored
under `sources/` and reused as their own extraction artifact. Binary uploads are
stored under `sources/` with their original filename, converted through a
per-extension converter, and the converted text is stored separately under
`artifacts/sources/<source_id>/`.

The converter table is open: `register_converter('.ext', fn)` adds a format.
The built-in docx/pdf converters import their (optional) library lazily and
raise a helpful `IngestError` when it is missing — install with
`pip install verinote[ingest]`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable

from verinote.store import Store
from verinote.text import nfc

# Stored as-is, no conversion.
TEXT_SUFFIXES = {".txt", ".md"}

# What an extraction says where it could not say a character. Extractors emit
# NUL for a glyph they cannot map (pypdf does this for fonts without a
# ToUnicode map), and a NUL is not "slightly dirty text": it is a value that
# breaks differently at each downstream boundary -- exec arguments refuse it,
# SQLite stores it, and the display layer shows nothing at all. U+FFFD is the
# one character whose job is to say "something was here and could not be read".
UNREADABLE_CHAR = "�"


class IngestError(RuntimeError):
    """Unsupported source type, a missing converter dependency, or a parse failure."""


Converter = Callable[[bytes], str]
_CONVERTERS: dict[str, Converter] = {}


def register_converter(suffix: str, fn: Converter) -> None:
    """Register a binary→text converter for a file extension (e.g. ``.rtf``)."""
    _CONVERTERS[suffix.lower()] = fn


def supported_suffixes() -> set[str]:
    """Every suffix the uploader/ingester accepts (text + registered binaries)."""
    return TEXT_SUFFIXES | set(_CONVERTERS)


def _convert_docx(raw: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as e:
        raise IngestError("docx ingestion needs `pip install verinote[ingest]`") from e
    try:
        document = docx.Document(BytesIO(raw))
    except Exception as e:  # python-docx raises various errors on bad input
        raise IngestError(f"could not read docx: {e}") from e
    return "\n".join(p.text for p in document.paragraphs)


def _convert_pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise IngestError("pdf ingestion needs `pip install verinote[ingest]`") from e
    try:
        reader = PdfReader(BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        raise IngestError(f"could not read pdf: {e}") from e


register_converter(".docx", _convert_docx)
register_converter(".pdf", _convert_pdf)


@dataclass(frozen=True)
class SanitizedText:
    """Extraction text with the NULs replaced, and how many there were."""

    text: str
    unreadable_chars: int


def sanitize_extracted_text(text: str) -> SanitizedText:
    """Replace NULs the extractor could not map with U+FFFD, and count them.

    The count is of NULs *in the input*, not of U+FFFD in the output: text can
    already contain a legitimate replacement character, and only the NULs are
    this extraction's loss.

    Only NUL. Not the other control characters -- `\\x0c` in particular is
    pdftotext's ordinary page separator, and widening this would delete
    structure nobody reported as broken.
    """
    return SanitizedText(text.replace("\x00", UNREADABLE_CHAR), text.count("\x00"))


def ingest_bytes(raw: bytes, filename: str) -> tuple[str, str]:
    """Resolve raw upload bytes to ``(text, kind)``; kind is ``text``/``binary``.

    Returns the converter's text unchanged: sanitizing happens in `store_source`
    and nowhere else, so every sink sees one value. Raises `IngestError` for
    unsupported suffixes or conversion failures.
    """
    suffix = Path(filename).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        try:
            return raw.decode("utf-8"), "text"
        except UnicodeDecodeError as e:
            raise IngestError("file is not valid UTF-8 text") from e
    if suffix in _CONVERTERS:
        return _CONVERTERS[suffix](raw), "binary"
    raise IngestError(f"unsupported source type: {suffix or filename!r}")


def store_source(
    store: Store, root: Path, filename: str, raw: bytes, text: str, kind: str
) -> dict:
    """Persist an original source and its extraction text artifact.

    Sanitizing happens here and nowhere else. The single reassignment below is
    what makes the digest, the artifact file, and the returned text one value:
    a second decision point elsewhere would let them drift apart.
    """
    sources_dir = root / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    name = nfc(Path(filename).name)
    extracted = sanitize_extracted_text(nfc(text))
    text = extracted.text
    source_path = sources_dir / name
    source_path.write_bytes(raw)
    citation = f"sources/{name}"
    source_id = store.add_source(citation, kind=kind)

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    artifact_dir = root / "artifacts" / "sources" / str(source_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{digest}.txt"
    if not artifact_path.exists():
        artifact_path.write_text(text, encoding="utf-8")
    artifact_relpath = f"artifacts/sources/{source_id}/{digest}.txt"

    artifact_id = store.add_source_artifact(
        source_id=source_id,
        kind="extracted_text",
        path=artifact_relpath,
        checksum=digest,
    )
    return {
        "citation": citation,
        "text": text,
        "kind": kind,
        "source_id": source_id,
        "artifact_id": artifact_id,
        "artifact_path": artifact_relpath,
        # Always an int, 0 when nothing was lost. NULL means "never measured"
        # and that is a property of a stored row, not of an ingest that ran.
        "unreadable_chars": extracted.unreadable_chars,
    }


def ingest_file(store: Store, src: Path, *, root: Path) -> dict:
    """Convert/register a file on disk. Returns {citation, text, kind}."""
    src = Path(src)
    if not src.is_file():
        raise IngestError(f"no such file: {src}")
    raw = src.read_bytes()
    text, kind = ingest_bytes(raw, src.name)
    return store_source(store, root, src.name, raw, text, kind)
