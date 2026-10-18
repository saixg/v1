"""Task intake: deterministic text extraction from what a developer hands the
Agent Context Engine (build-plan.md Part 4.F.1).

Inputs: the task's own text, plain-text/markdown documents, PDFs, DOCX files,
and images/diagrams. Output: `IntakeDocument`s made of `IntakeChunk`s, each
carrying a stable `chunk_id` and a `source_ref` (document name plus page,
paragraph, table row or line range) so every requirement extracted later can
be traced back to the exact place it came from — and so the extraction
validator can check that place exists and actually contains the quoted text.

Rules, and why:

* **Nothing is executed.** No macros, no embedded scripts, no PDF actions, no
  external entity resolution. pypdf only reads text operators; python-docx
  reads OOXML through an lxml parser with entity resolution off, and the DOCX
  zip is size/ratio/entry-checked *by actually decompressing it under a cap*
  before python-docx is allowed to open it (Part 9's archive expansion
  limits — a DOCX is a zip, and declared sizes in a zip header can lie).
* **Caps fail visibly.** A document over a byte, page, chunk or text cap is
  rejected with an `IntakeError` naming the cap, never silently truncated:
  a requirements document with its last twenty pages cut off would yield a
  requirement set that looks complete and is not.
* **Secrets are redacted here, before anything else sees the text.** Every
  chunk's text is passed through the repository scanner's `redact_secrets`
  (`app.analysis.scan.secrets`) at construction, so no later stage — prompt
  building, logging, persistence — can reach an unredacted span (Part 0.3,
  Part 4.A.5). Images cannot be scanned without OCR, which is out of scope;
  see `intake_image`.
* **Extracted text is untrusted data.** Nothing in this module interprets it,
  and the extraction prompt frames it as data (see `extract.py`).
"""

from __future__ import annotations

import base64
import io
import re
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from app.analysis.scan.secrets import redact_secrets
from app.llm import ImageInput

ANALYZER = "context.intake"
ANALYZER_VERSION = "0.1.0"

# The `source` values `codeaxis.requirements.source_ref` documents. Markdown is
# `text` with `format: markdown` in the ref: the distinction matters for
# display, not for provenance.
IntakeSource = Literal["text", "pdf", "docx", "image"]


class IntakeError(ValueError):
    """The input was rejected. The message names the cap or the defect and is
    safe to show to the user: it never echoes document content."""


@dataclass(frozen=True)
class IntakeLimits:
    """Per-document caps. Defaults sized for a requirements document, not a
    book: the whole intake goes into one `synth` call (Part 3)."""

    max_document_bytes: int = 10 * 1024 * 1024
    max_pdf_pages: int = 200
    # Wall-clock budget for one PDF's text extraction, checked between pages.
    # pypdf is pure Python; a pathological content stream can be slow, and a
    # page-boundary deadline bounds the damage without threads.
    max_pdf_seconds: float = 30.0
    # A DOCX is a zip: entry count, total *actual* decompressed size, and
    # decompressed/compressed ratio, the same three guards as repo ZIP intake.
    max_docx_entries: int = 1000
    max_docx_uncompressed_bytes: int = 50 * 1024 * 1024
    max_docx_expansion_ratio: float = 100.0
    # Extracted text across one document, after redaction.
    max_text_chars: int = 400_000
    max_chunks: int = 4000
    # A chunk is a page, paragraph or table row; anything longer is split
    # into consecutive parts so a quote check stays local to a small span.
    max_chunk_chars: int = 6000
    # Anthropic's per-image ceiling is 5 MB; larger is rejected, not resized.
    max_image_bytes: int = 5 * 1024 * 1024


DEFAULT_LIMITS = IntakeLimits()


@dataclass(frozen=True)
class IntakeChunk:
    """One citable piece of an input document. `text` is already redacted."""

    chunk_id: str
    document: str
    source: IntakeSource
    text: str
    page: int | None = None
    paragraph: int | None = None
    # "table 2, row 3" for DOCX tables; free-form location for non-text refs.
    region: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    # Which slice of an over-long page/paragraph this is, 1-based; None when
    # the location was not split.
    part: int | None = None
    format: str | None = None

    def source_ref(self) -> dict[str, object]:
        """The `requirements.source_ref` payload for a statement from here."""
        ref: dict[str, object] = {
            "source": self.source,
            "ref": self.document,
            "chunk_id": self.chunk_id,
        }
        for key in ("page", "paragraph", "region", "line_start", "line_end", "part", "format"):
            value = getattr(self, key)
            if value is not None:
                ref[key] = value
        return ref

    def location(self) -> str:
        """Human-readable location, used as the chunk's label in prompts."""
        bits = [self.document]
        if self.page is not None:
            bits.append(f"page {self.page}")
        if self.paragraph is not None:
            bits.append(f"paragraph {self.paragraph}")
        if self.region is not None:
            bits.append(self.region)
        if self.line_start is not None:
            bits.append(f"lines {self.line_start}-{self.line_end}")
        if self.part is not None:
            bits.append(f"part {self.part}")
        return ", ".join(bits)


@dataclass(frozen=True)
class IntakeDocument:
    name: str
    source: IntakeSource
    chunks: tuple[IntakeChunk, ...]
    # Count of redacted spans across the document. Reported so the UI can say
    # "2 credentials were removed before analysis"; the values are gone.
    secrets_redacted: int = 0
    # Locations that could not be read (a PDF page whose text extraction
    # raised). An admitted gap rather than a silently shorter document.
    unreadable: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntakeImage:
    """An image/diagram for the `vision` role. Never OCR'd and never
    secret-scanned (no text to scan without OCR), which is why sending one to
    a model is the caller's explicit choice — see `intake_image`."""

    chunk_id: str
    name: str
    media_type: str
    size_bytes: int
    image: ImageInput = field(repr=False)

    def source_ref(self, region: str | None = None) -> dict[str, object]:
        ref: dict[str, object] = {"source": "image", "ref": self.name, "chunk_id": self.chunk_id}
        if region:
            ref["region"] = region
        return ref


# --- shared helpers -----------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def safe_name(name: str | None, fallback: str) -> str:
    """A display name with control characters (newlines included) removed and
    length capped. File names are user-controlled and are echoed into the
    extraction prompt as chunk labels, so a name must not be able to forge a
    delimiter line there."""
    cleaned = _CONTROL.sub(" ", name or "").strip()[:120]
    return cleaned or fallback


def _check_size(data: bytes, limits: IntakeLimits) -> None:
    if len(data) > limits.max_document_bytes:
        raise IntakeError(
            f"document is {len(data)} bytes; the limit is {limits.max_document_bytes}"
        )


def _redact(text: str) -> tuple[str, int]:
    redacted, count = redact_secrets(text.encode("utf-8"))
    return redacted.decode("utf-8"), count


class _Builder:
    """Accumulates chunks for one document, enforcing the text/chunk caps and
    redacting every chunk as it is added."""

    def __init__(self, doc_prefix: str, name: str, source: IntakeSource,
                 limits: IntakeLimits) -> None:
        self.prefix = doc_prefix
        self.name = name
        self.source: IntakeSource = source
        self.limits = limits
        self.chunks: list[IntakeChunk] = []
        self.chars = 0
        self.secrets = 0
        self.unreadable: list[str] = []

    def add(self, text: str, **where: object) -> None:
        text = text.strip()
        if not text:
            return
        parts = _split(text, self.limits.max_chunk_chars)
        for n, piece in enumerate(parts, start=1):
            redacted, count = _redact(piece)
            self.secrets += count
            self.chars += len(redacted)
            if self.chars > self.limits.max_text_chars:
                raise IntakeError(
                    f"{self.name}: extracted text exceeds {self.limits.max_text_chars} characters"
                )
            if len(self.chunks) >= self.limits.max_chunks:
                raise IntakeError(f"{self.name}: more than {self.limits.max_chunks} text chunks")
            self.chunks.append(
                IntakeChunk(
                    chunk_id=f"{self.prefix}.{len(self.chunks) + 1}",
                    document=self.name,
                    source=self.source,
                    text=redacted,
                    part=n if len(parts) > 1 else None,
                    **where,  # pyright: ignore[reportArgumentType]
                )
            )

    def done(self) -> IntakeDocument:
        return IntakeDocument(
            name=self.name,
            source=self.source,
            chunks=tuple(self.chunks),
            secrets_redacted=self.secrets,
            unreadable=tuple(self.unreadable),
        )


def _split(text: str, limit: int) -> list[str]:
    """Split on line boundaries into pieces of at most `limit` characters
    (hard-cutting only a single line longer than the limit)."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            pieces.append(current)
            current = ""
        current += line
    if current.strip():
        pieces.append(current)
    return [p for p in pieces if p.strip()]


# --- plain text / markdown -----------------------------------------------------


def _decode_text(data: bytes, name: str) -> str:
    if b"\x00" in data:
        raise IntakeError(f"{name}: looks binary (NUL byte), not a text document")
    return data.decode("utf-8-sig", errors="replace")


def _paragraphs(text: str) -> Iterator[tuple[int, int, str]]:
    """(line_start, line_end, text) for each blank-line-separated block."""
    start: int | None = None
    buf: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines, start=1):
        if line.strip():
            if start is None:
                start = i
            buf.append(line)
        elif start is not None:
            yield start, i - 1, "\n".join(buf)
            start, buf = None, []
    if start is not None:
        yield start, len(lines), "\n".join(buf)


def intake_text(
    text: str,
    *,
    name: str,
    doc_prefix: str,
    format: str = "text",
    limits: IntakeLimits = DEFAULT_LIMITS,
) -> IntakeDocument:
    """Typed task text or a decoded text/markdown document. One chunk per
    blank-line-separated paragraph, with its line range."""
    if len(text.encode("utf-8")) > limits.max_document_bytes:
        raise IntakeError(f"{name}: text exceeds {limits.max_document_bytes} bytes")
    builder = _Builder(doc_prefix, safe_name(name, "text"), "text", limits)
    for line_start, line_end, para in _paragraphs(text):
        builder.add(para, line_start=line_start, line_end=line_end, format=format)
    return builder.done()


def intake_text_bytes(
    data: bytes, *, name: str, doc_prefix: str, limits: IntakeLimits = DEFAULT_LIMITS
) -> IntakeDocument:
    _check_size(data, limits)
    fmt = "markdown" if name.lower().endswith((".md", ".markdown")) else "text"
    return intake_text(
        _decode_text(data, name), name=name, doc_prefix=doc_prefix, format=fmt, limits=limits
    )


# --- PDF -------------------------------------------------------------------------


def intake_pdf(
    data: bytes, *, name: str, doc_prefix: str, limits: IntakeLimits = DEFAULT_LIMITS
) -> IntakeDocument:
    """One chunk per page (split if over-long), with the 1-based page number.

    Encrypted PDFs are rejected rather than decrypted with an empty password:
    a document someone protected is not one to quietly open. A page whose
    extraction raises is recorded in `unreadable` and skipped. Scanned PDFs
    with no text layer yield no chunks — OCR is out of scope, and the caller
    sees an empty document rather than invented text."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    _check_size(data, limits)
    if not data.lstrip()[:5].startswith(b"%PDF-"):
        raise IntakeError(f"{name}: not a PDF (missing %PDF- header)")
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise IntakeError(f"{name}: encrypted PDFs are not accepted")
        page_count = len(reader.pages)
    except IntakeError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise IntakeError(f"{name}: unreadable PDF ({type(exc).__name__})") from exc
    if page_count > limits.max_pdf_pages:
        raise IntakeError(f"{name}: {page_count} pages; the limit is {limits.max_pdf_pages}")

    builder = _Builder(doc_prefix, safe_name(name, "document.pdf"), "pdf", limits)
    deadline = time.monotonic() + limits.max_pdf_seconds
    for index in range(page_count):
        if time.monotonic() > deadline:
            raise IntakeError(
                f"{name}: text extraction exceeded {limits.max_pdf_seconds:.0f}s "
                f"(stopped at page {index + 1} of {page_count})"
            )
        try:
            text = reader.pages[index].extract_text() or ""
        except IntakeError:
            raise
        except Exception as exc:  # one bad page is a gap, not a crash
            builder.unreadable.append(f"page {index + 1}: {type(exc).__name__}")
            continue
        builder.add(text, page=index + 1)
    return builder.done()


# --- DOCX ------------------------------------------------------------------------

# Parts that are parsed as XML. A DOCTYPE/ENTITY declaration in any of them is
# rejected outright: real Word output never contains one, and it is the
# vector for entity-expansion and external-entity attacks even though the
# parser python-docx uses has entity resolution off.
_XML_SUFFIXES = (".xml", ".rels")
_DTD_MARKERS = (b"<!DOCTYPE", b"<!ENTITY")


def check_docx_archive(data: bytes, limits: IntakeLimits = DEFAULT_LIMITS) -> None:
    """Zip-bomb and structure guard for a DOCX, run before python-docx opens
    it. Decompresses every entry under a running cap: declared sizes in the
    zip directory are attacker-controlled, so they are checked *and* the
    actual inflated byte count is enforced."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise IntakeError("not a valid DOCX (not a zip archive)") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_docx_entries:
            raise IntakeError(
                f"DOCX has {len(infos)} entries; the limit is {limits.max_docx_entries}"
            )
        names = {i.filename for i in infos}
        if "word/document.xml" not in names or "[Content_Types].xml" not in names:
            raise IntakeError("not a valid DOCX (missing word/document.xml)")
        declared = sum(i.file_size for i in infos)
        cap = limits.max_docx_uncompressed_bytes
        ratio_cap = limits.max_docx_expansion_ratio * max(len(data), 1)
        if declared > cap:
            raise IntakeError(f"DOCX expands to {declared} bytes; the limit is {cap}")
        if declared > ratio_cap:
            raise IntakeError("DOCX expansion ratio exceeds the limit (possible zip bomb)")
        total = 0
        for info in infos:
            if info.flag_bits & 0x1:
                raise IntakeError("DOCX contains an encrypted entry")
            is_xml = info.filename.lower().endswith(_XML_SUFFIXES)
            tail = b""
            try:
                with zf.open(info) as fh:
                    while block := fh.read(64 * 1024):
                        total += len(block)
                        if total > cap or total > ratio_cap:
                            raise IntakeError(
                                "DOCX exceeded its decompression limit while reading "
                                "(possible zip bomb)"
                            )
                        if is_xml:
                            window = tail + block
                            if any(m in window for m in _DTD_MARKERS):
                                raise IntakeError("DOCX contains a DTD/entity declaration")
                            tail = window[-16:]
            except (zipfile.BadZipFile, EOFError, OSError, NotImplementedError) as exc:
                raise IntakeError(f"corrupt DOCX entry ({type(exc).__name__})") from exc


def intake_docx(
    data: bytes, *, name: str, doc_prefix: str, limits: IntakeLimits = DEFAULT_LIMITS
) -> IntakeDocument:
    """One chunk per non-empty body paragraph (1-based paragraph index over
    the body, empty ones included so the index matches what Word shows) and
    one per table row, in document order. Headers, footers, comments and
    embedded objects are not read."""
    _check_size(data, limits)
    check_docx_archive(data, limits)

    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # any parse failure is a rejection
        raise IntakeError(f"{name}: unreadable DOCX ({type(exc).__name__})") from exc

    builder = _Builder(doc_prefix, safe_name(name, "document.docx"), "docx", limits)
    body = document.element.body
    paragraph_no = 0
    table_no = 0
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph_no += 1
            builder.add(Paragraph(child, document).text, paragraph=paragraph_no)
        elif tag == "tbl":
            table_no += 1
            table = Table(child, document)
            for row_no, row in enumerate(table.rows, start=1):
                cells = [c.text.strip() for c in row.cells]
                # Merged cells repeat their text per grid column; keep one.
                deduped = [c for i, c in enumerate(cells) if c and (i == 0 or c != cells[i - 1])]
                builder.add(" | ".join(deduped), region=f"table {table_no}, row {row_no}")
    return builder.done()


# --- images ----------------------------------------------------------------------

# Media types the vision route accepts, with the magic bytes each must start
# with. The declared type must match the sniffed one: a mislabeled payload is
# rejected rather than trusted.
_IMAGE_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}


def _sniff_image(data: bytes) -> str | None:
    for media_type, magics in _IMAGE_MAGIC.items():
        if any(data.startswith(m) for m in magics):
            if media_type == "image/webp" and data[8:12] != b"WEBP":
                continue
            return media_type
    return None


def intake_image(
    data: bytes,
    *,
    name: str,
    media_type: str,
    chunk_id: str,
    limits: IntakeLimits = DEFAULT_LIMITS,
) -> IntakeImage:
    """Validate an image for the `vision` role: allowlisted media type,
    magic bytes matching the declared type, size cap.

    STUB(CodeAxis): no OCR, so an image's text cannot be secret-scanned. An
    image that shows a credential would reach the model unredacted. That is
    why images only reach a model when the caller passes them explicitly to
    `extract.extract_requirements`; routes must make that an explicit user
    choice (Part 3: "explicit 'send to external model' setting")."""
    declared = media_type.strip().lower()
    if declared not in _IMAGE_MAGIC:
        raise IntakeError(
            f"{name}: media type {declared!r} is not accepted "
            f"(allowed: {', '.join(sorted(_IMAGE_MAGIC))})"
        )
    if len(data) > limits.max_image_bytes:
        raise IntakeError(f"{name}: image is {len(data)} bytes; the limit is "
                          f"{limits.max_image_bytes}")
    sniffed = _sniff_image(data)
    if sniffed != declared:
        raise IntakeError(f"{name}: content does not match declared type {declared!r}")
    return IntakeImage(
        chunk_id=chunk_id,
        name=safe_name(name, "image"),
        media_type=declared,
        size_bytes=len(data),
        image=ImageInput(media_type=declared, data_base64=base64.b64encode(data).decode("ascii")),
    )


# --- dispatch ---------------------------------------------------------------------


def intake_document(
    data: bytes,
    *,
    name: str,
    doc_prefix: str,
    content_type: str | None = None,
    limits: IntakeLimits = DEFAULT_LIMITS,
) -> IntakeDocument:
    """Route an uploaded document by its *content* (magic bytes) first and
    its extension second; a mismatch between the two is rejected. Unknown
    types are rejected — there is no "try everything" fallback."""
    lower = name.lower()
    ctype = (content_type or "").lower()
    is_pdf_bytes = data.lstrip()[:5].startswith(b"%PDF-")
    is_zip_bytes = data.startswith(b"PK\x03\x04")
    if lower.endswith(".pdf") or ctype == "application/pdf":
        if not is_pdf_bytes:
            raise IntakeError(f"{name}: named as PDF but content is not a PDF")
        return intake_pdf(data, name=name, doc_prefix=doc_prefix, limits=limits)
    if lower.endswith(".docx") or "wordprocessingml" in ctype:
        if not is_zip_bytes:
            raise IntakeError(f"{name}: named as DOCX but content is not a DOCX")
        return intake_docx(data, name=name, doc_prefix=doc_prefix, limits=limits)
    if lower.endswith((".txt", ".md", ".markdown", ".text")) or ctype.startswith("text/"):
        if is_pdf_bytes or is_zip_bytes:
            raise IntakeError(f"{name}: named as text but content is binary")
        return intake_text_bytes(data, name=name, doc_prefix=doc_prefix, limits=limits)
    raise IntakeError(f"{name}: unsupported document type (accepted: .txt, .md, .pdf, .docx)")


# --- one task's whole intake --------------------------------------------------------


@dataclass(frozen=True)
class Upload:
    """One uploaded file as the route received it. `data` is untrusted."""

    name: str
    data: bytes
    content_type: str | None = None


@dataclass(frozen=True)
class Intake:
    """Everything one extraction call may see: the task text (prefix `T`),
    each document (`D1`, `D2`, ...) and each image (`I1`, ...). Chunk ids are
    unique across the whole intake, which is what lets the validator resolve
    a model's citation without knowing which document it meant."""

    documents: tuple[IntakeDocument, ...]
    images: tuple[IntakeImage, ...] = ()

    def chunks(self) -> dict[str, IntakeChunk]:
        return {c.chunk_id: c for d in self.documents for c in d.chunks}

    def image_map(self) -> dict[str, IntakeImage]:
        return {i.chunk_id: i for i in self.images}

    @property
    def secrets_redacted(self) -> int:
        return sum(d.secrets_redacted for d in self.documents)

    @property
    def unreadable(self) -> list[str]:
        return [f"{d.name}: {u}" for d in self.documents for u in d.unreadable]


def _is_image(upload: Upload) -> bool:
    ctype = (upload.content_type or "").lower()
    return ctype.startswith("image/") or upload.name.lower().endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp")
    )


_EXT_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp"}


def prepare_intake(
    task_text: str,
    uploads: tuple[Upload, ...] | list[Upload] = (),
    *,
    limits: IntakeLimits = DEFAULT_LIMITS,
) -> Intake:
    """Build the intake for one task: its text plus every upload, each
    extracted, chunked and secret-redacted. Raises `IntakeError` on the first
    rejected input — a task is extracted from all of its inputs or none, so a
    silently skipped attachment cannot make the requirement set look whole."""
    documents = [intake_text(task_text, name="task", doc_prefix="T", limits=limits)]
    images: list[IntakeImage] = []
    for upload in uploads:
        if _is_image(upload):
            ext = "." + upload.name.lower().rsplit(".", 1)[-1] if "." in upload.name else ""
            media_type = (upload.content_type or _EXT_MEDIA.get(ext, "")).lower()
            images.append(
                intake_image(upload.data, name=upload.name, media_type=media_type,
                             chunk_id=f"I{len(images) + 1}", limits=limits)
            )
        else:
            documents.append(
                intake_document(upload.data, name=upload.name, doc_prefix=f"D{len(documents)}",
                                content_type=upload.content_type, limits=limits)
            )
    return Intake(documents=tuple(documents), images=tuple(images))
