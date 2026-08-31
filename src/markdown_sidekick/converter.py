"""Conversion engine wrapping Microsoft's markitdown library.

Keeps all markitdown-specific logic in one place so the UI never has to know
how conversion actually happens.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

try:
    from markitdown import MarkItDown
except ImportError as exc:  # pragma: no cover - surfaced to the user in the UI
    raise ImportError(
        "The 'markitdown' package is not installed. Activate the project "
        "virtual environment or run: pip install \"markitdown[all]\""
    ) from exc

from . import audio, mineru, ocr


# File extensions markitdown can meaningfully handle. Used to build the file
# picker filter and to give friendly warnings; markitdown still sniffs content
# at conversion time, so this list is a guide, not a hard gate.
SUPPORTED_EXTENSIONS: tuple[str, ...] = (
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".xls",
    ".html",
    ".htm",
    ".csv",
    ".json",
    ".xml",
    ".txt",
    ".md",
    ".epub",
    ".zip",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
    ".webp",
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".ogg",
    ".mp4",
    ".m4v",
    ".mkv",
    ".mov",
    ".webm",
    ".avi",
)


@dataclass
class ConversionResult:
    """Outcome of converting a single source file."""

    source: Path
    markdown: str = ""
    error: str | None = None
    output_path: Path | None = None
    engine: str = "markitdown"  # which pipeline produced the markdown

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def title(self) -> str:
        return self.source.name


@dataclass
class ConversionEngine:
    """Reusable wrapper around markitdown with an optional local OCR fallback.

    Routing per file:
      * image file + OCR enabled  -> RapidOCR
      * scanned/mixed PDF + OCR   -> render scanned pages + OCR, keep text pages
      * everything else           -> markitdown (best for clean digital docs)
    """

    enable_plugins: bool = False
    enable_ocr: bool = True
    enable_audio: bool = True
    whisper_model: str = "base"
    mineru_endpoint: str = ""  # blank = disabled
    page_anchors: bool = False  # emit <!-- page N --> markers for PDFs
    _md: MarkItDown = field(init=False, repr=False)
    _ocr: "ocr.OcrEngine | None" = field(init=False, default=None, repr=False)
    _audio: "audio.AudioTranscriber | None" = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._md = MarkItDown(enable_plugins=self.enable_plugins)

    def _ocr_engine(self) -> "ocr.OcrEngine":
        if self._ocr is None:
            self._ocr = ocr.OcrEngine()
        return self._ocr

    def _audio_engine(self) -> "audio.AudioTranscriber":
        # Rebuild if the model size changed so the right model is loaded.
        if self._audio is None or self._audio.model_size != self.whisper_model:
            self._audio = audio.AudioTranscriber(model_size=self.whisper_model)
        return self._audio

    def convert_file(
        self,
        path: str | os.PathLike[str],
        on_subprogress: Callable[[Path, float, float, str], None] | None = None,
    ) -> ConversionResult:
        """Convert a single file, never raising; errors are captured on the result.

        ``on_subprogress(source, current, total, unit)`` is invoked during slow
        within-file work (OCR pages, audio seconds) so the UI can show progress.
        ``unit`` is "page" or "sec". It must be thread-safe.
        """
        source = Path(path)
        if not source.exists():
            return ConversionResult(source=source, error="File does not exist.")
        if source.is_dir():
            return ConversionResult(source=source, error="Path is a directory, not a file.")
        ext = source.suffix.lower()
        try:
            # Specialised routes are attempted defensively: on any failure
            # (corrupt/encrypted file, render error, missing/undownloadable model)
            # fall through to markitdown rather than failing the file outright.
            if self.enable_ocr and ocr.ocr_available() and ext in ocr.OCR_IMAGE_EXTENSIONS:
                try:
                    md = self._ocr_engine().image_to_markdown(source)
                    return ConversionResult(source=source, markdown=md, engine="ocr")
                except Exception:
                    pass  # fall back to markitdown

            # High-fidelity MinerU (opt-in via a configured endpoint) gets first
            # crack at PDFs; on any failure we fall through to local OCR/markitdown.
            if ext == ".pdf" and mineru.mineru_configured(self.mineru_endpoint):
                try:
                    md = mineru.convert_via_mineru(source, self.mineru_endpoint)
                    if md and md.strip():
                        return ConversionResult(source=source, markdown=md, engine="mineru")
                except Exception:
                    pass  # fall back to local OCR / markitdown

            if self.enable_ocr and ocr.pdf_ocr_available() and ext == ".pdf":
                try:
                    analysis = ocr.analyze_pdf(source)
                    if analysis is not None and analysis.needs_ocr:
                        inner = (
                            (lambda p, t: on_subprogress(source, p, t, "page"))
                            if on_subprogress is not None
                            else None
                        )
                        md = self._ocr_engine().pdf_to_markdown(
                            source, analysis, on_page=inner
                        )
                        return ConversionResult(
                            source=source, markdown=md, engine="ocr+text"
                        )
                except Exception:
                    pass  # fall back to markitdown

            # Digital PDFs normally go to markitdown, but page anchors need
            # per-page extraction — markitdown flattens page boundaries away.
            if self.page_anchors and ext == ".pdf" and ocr.pdfium_available():
                try:
                    inner = (
                        (lambda p, t: on_subprogress(source, p, t, "page"))
                        if on_subprogress is not None
                        else None
                    )
                    md = self._ocr_engine().pdf_text_to_markdown(source, on_page=inner)
                    # A scanned PDF with OCR off yields only empty anchors —
                    # let markitdown have a go instead of returning husks.
                    body = re.sub(r"<!-- page \d+ -->", "", md)
                    if len(body.strip()) >= 50:
                        return ConversionResult(
                            source=source, markdown=md, engine="pdftext"
                        )
                except Exception:
                    pass  # fall back to markitdown

            if self.enable_audio and audio.audio_available() and ext in audio.MEDIA_EXTENSIONS:
                try:
                    inner = (
                        (lambda c, t: on_subprogress(source, c, t, "sec"))
                        if on_subprogress is not None
                        else None
                    )
                    md = self._audio_engine().transcribe_to_markdown(
                        source, on_progress=inner
                    )
                    return ConversionResult(source=source, markdown=md, engine="whisper")
                except Exception:
                    pass  # fall back to markitdown

            result = self._md.convert(str(source))
            return ConversionResult(
                source=source, markdown=result.text_content or "", engine="markitdown"
            )
        except Exception as exc:  # markitdown raises a variety of types
            return ConversionResult(source=source, error=f"{type(exc).__name__}: {exc}")

    def convert_many(
        self,
        paths: Iterable[str | os.PathLike[str]],
        on_progress: Callable[[int, int, ConversionResult], None] | None = None,
        on_subprogress: Callable[[Path, float, float, str], None] | None = None,
    ) -> list[ConversionResult]:
        """Convert several files, reporting progress after each one.

        ``on_progress`` receives ``(index, total, result)`` with ``index`` being
        1-based. ``on_subprogress`` is forwarded to per-file OCR/audio progress.
        Both must be safe to call from a worker thread.
        """
        items = [Path(p) for p in paths]
        total = len(items)
        results: list[ConversionResult] = []
        for index, item in enumerate(items, start=1):
            result = self.convert_file(item, on_subprogress=on_subprogress)
            results.append(result)
            if on_progress is not None:
                on_progress(index, total, result)
        return results


def default_output_path(source: Path, out_dir: Path | None = None) -> Path:
    """Suggested ``.md`` destination next to the source (or in ``out_dir``)."""
    target_dir = out_dir if out_dir is not None else source.parent
    return target_dir / f"{source.stem}.md"


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Plain-language explanations for conversion failures
# ---------------------------------------------------------------------------
# Matched against the raw error string (which includes the exception type
# name, e.g. "PDFPasswordIncorrect: ..."); first hit wins, so more specific
# patterns come first. Every entry is (pattern, what happened, what to try).
# Patterns are compiled at import time so a malformed addition fails loudly
# in tests/selftest instead of inside the GUI's error-display path.
_ERROR_HINTS: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), what, fix)
    for pattern, what, fix in (
    (
        r"password|encrypt|decrypt",
        "This document is password-protected or encrypted.",
        "Remove the protection (open it and print/export to a new file), then retry.",
    ),
    (
        r"WinError 32|being used by another process|Errno 13|Permission denied|PermissionError",
        "The file is locked or in use by another program.",
        "Close the program that has it open (often a PDF reader or Office), then retry.",
    ),
    (
        r"File does not exist|FileNotFoundError|No such file|cannot find the (?:file|path)",
        "The file can't be found — it may have been moved, renamed, or deleted.",
        "Re-add the file from its current location.",
    ),
    (
        r"Path is a directory",
        "That's a folder, not a file.",
        "Add the files inside it instead.",
    ),
    (
        r"UnsupportedFormatException|[Uu]nsupported format|no converter",
        "No conversion engine understands this file type.",
        "Export the content as PDF, DOCX, HTML, or another supported format, then retry.",
    ),
    (
        # Includes the shapes pdfminer raises through markitdown's wrapper
        # ("PdfConverter threw PSEOF/PDFSyntaxError with message ...").
        r"BadZipFile|not a zip file|corrupt|damaged|truncated|EOFError|"
        r"Unexpected EOF|PSEOF|PSSyntaxError|PDFSyntaxError|No /Root object|"
        r"invalid.{0,20}header",
        "The file appears to be corrupt or incomplete.",
        "Re-download or re-export the file, then retry.",
    ),
    (
        r"MemoryError",
        "The file is too large to convert with the memory available.",
        "Close other programs, or split the document into parts, then retry.",
    ),
    (
        r"codec|moov atom|InvalidDataError|DecoderNotFound",
        "The audio/video stream couldn't be decoded.",
        "Convert it to a common format (MP3, WAV, or MP4), then retry.",
    ),
    )
)

# Exception messages embed the file path in quotes; its words must not steer
# the diagnosis (a locked "passwords-export.xlsx" is not an encryption error).
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def explain_error(error: str) -> tuple[str, str]:
    """Plain-language (what happened, what to try) for a raw conversion error.

    Falls back to a generic explanation — the raw error is still shown to the
    user as technical details, so an unmatched class loses nothing.
    """
    scrubbed = _QUOTED_RE.sub("", error)
    for pattern, what, fix in _ERROR_HINTS:
        if pattern.search(scrubbed):
            return what, fix
    return (
        "The conversion engine reported an unexpected error.",
        "Retry the file; if it keeps failing, the technical details say why.",
    )
