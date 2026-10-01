"""Conversion engine wrapping Microsoft's markitdown library.

Keeps all markitdown-specific logic in one place so the UI never has to know
how conversion actually happens.
"""

from __future__ import annotations

import os
import re
import time
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

from . import audio, debuglog, errors, mineru, ocr, pdflayout


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

# A source at least this big that "converts" to no real text is reported as an
# error rather than a silent empty success. Set high enough that small-but-real
# empty documents (an untouched .docx is ~10 KB of zip scaffolding) pass through
# as empty output rather than being declared failures.
_EMPTY_OUTPUT_MIN_BYTES = 16_384


@dataclass
class ConversionResult:
    """Outcome of converting a single source file."""

    source: Path
    markdown: str = ""
    error: str | None = None
    output_path: Path | None = None
    engine: str = "markitdown"  # which pipeline produced the markdown
    # Document metadata the PDF layout engine could vouch for ("" = unknown);
    # export prefers these over guessing from the text.
    doc_title: str = ""
    doc_author: str = ""
    # Failure diagnosis (errors.py): the code shown to the user and the
    # reference of its error-log entry. Blank while ok.
    error_code: str = ""
    error_ref: str = ""
    # errors.Incident for each route that failed before another produced
    # the document — usable output, but not the best route's.
    warnings: list = field(default_factory=list)

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
      * digital PDF               -> column-aware layout engine (pdflayout)
      * everything else           -> markitdown (best for clean digital docs)
    """

    enable_plugins: bool = False
    enable_ocr: bool = True
    ocr_device: str = "auto"  # auto | cpu | gpu (DirectML) — see ocr.OCR_DEVICES
    enable_audio: bool = True
    whisper_model: str = "base"
    mineru_endpoint: str = ""  # blank = disabled
    page_anchors: bool = False  # emit <!-- page N --> markers for PDFs
    # Column-aware PDF reading (pdflayout). Off = the legacy markitdown path,
    # kept as an escape hatch for PDFs the geometric reader mishandles.
    pdf_layout: bool = True
    # Emit <!-- figure N.K --> markers where images sit, so figure extraction
    # at export can link each image in place (set from Settings.extract_images).
    figure_markers: bool = False
    _md: MarkItDown = field(init=False, repr=False)
    _ocr: "ocr.OcrEngine | None" = field(init=False, default=None, repr=False)
    _audio: "audio.AudioTranscriber | None" = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._md = MarkItDown(enable_plugins=self.enable_plugins)

    def _ocr_engine(self) -> "ocr.OcrEngine":
        # Rebuilt when the device setting changes — the ONNX sessions inside
        # are bound to a provider at construction time.
        if self._ocr is None or self._ocr.device != self.ocr_device:
            self._ocr = ocr.OcrEngine(device=self.ocr_device)
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

        A failure gets an error code (``result.error_code``) and an entry in
        the error log (``result.error_ref``); a specialised route that failed
        before another succeeded is kept as a warning (``result.warnings``) —
        the output is usable but not what that route would have produced.
        """
        source = Path(path)
        conv = debuglog.new_conversion(source)
        with debuglog.context(file=source.name, conv=conv):
            started = time.perf_counter()
            if debuglog.enabled():
                debuglog.event("convert.start", **self._describe(source))
            failures: list[tuple[str, BaseException]] = []
            result = self._convert(source, on_subprogress, failures)
            self._diagnose(result, failures)
            if debuglog.enabled():
                debuglog.event(
                    "convert.end",
                    level="info" if result.ok else "error",
                    engine=result.engine,
                    ok=result.ok,
                    error=result.error,
                    code=result.error_code,
                    warnings=[w.code for w in result.warnings],
                    chars=len(result.markdown),
                    lines=result.markdown.count("\n"),
                    title=result.doc_title,
                    author=result.doc_author,
                    ms=round((time.perf_counter() - started) * 1000),
                    **(debuglog.memory() or {}),
                )
                if result.ok:
                    debuglog.snapshot("raw", result.markdown)
            return result

    def _describe(self, source: Path) -> dict:
        """The file and the routing configuration, for the debug trace."""
        info: dict = {"path": str(source), "ext": source.suffix.lower()}
        try:
            st = source.stat()
            info["bytes"] = st.st_size
            info["modified"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
        except OSError as exc:
            info["stat_error"] = str(exc)
        info["config"] = {
            "ocr": self.enable_ocr,
            "ocr_available": ocr.ocr_available(),
            "ocr_device": self.ocr_device,
            "pdf_layout": self.pdf_layout,
            "audio": self.enable_audio,
            "whisper_model": self.whisper_model,
            "mineru": bool(self.mineru_endpoint),
            "page_anchors": self.page_anchors,
            "figure_markers": self.figure_markers,
        }
        return info

    def _convert(
        self,
        source: Path,
        on_subprogress: Callable[[Path, float, float, str], None] | None,
        failures: list[tuple[str, BaseException]],
    ) -> ConversionResult:
        if not source.exists():
            return ConversionResult(source=source, error="File does not exist.")
        if source.is_dir():
            return ConversionResult(source=source, error="Path is a directory, not a file.")
        ext = source.suffix.lower()
        try:
            # Specialised routes are attempted defensively: on any failure
            # (corrupt/encrypted file, render error, missing/undownloadable model)
            # fall through to markitdown rather than failing the file outright —
            # but every failure is kept (``failures``) for the diagnosis.
            if self.enable_ocr and ocr.ocr_available() and ext in ocr.OCR_IMAGE_EXTENSIONS:
                try:
                    with debuglog.span("route", route="ocr_image"):
                        md = self._ocr_engine().image_to_markdown(source)
                    return ConversionResult(source=source, markdown=md, engine="ocr")
                except Exception as exc:
                    failures.append(("ocr_image", exc))

            # High-fidelity MinerU (opt-in via a configured endpoint) gets first
            # crack at PDFs; on any failure we fall through to local OCR/markitdown.
            if ext == ".pdf" and mineru.mineru_configured(self.mineru_endpoint):
                try:
                    with debuglog.span("route", route="mineru") as sp:
                        md = mineru.convert_via_mineru(source, self.mineru_endpoint)
                        sp["chars"] = len(md or "")
                    if md and md.strip():
                        return ConversionResult(source=source, markdown=md, engine="mineru")
                    raise RuntimeError(f"MinerU at {self.mineru_endpoint} returned no Markdown")
                except Exception as exc:
                    failures.append(("mineru", exc))

            inner_page = (
                (lambda p, t: on_subprogress(source, p, t, "page"))
                if on_subprogress is not None
                else None
            )
            use_layout = self.pdf_layout and pdflayout.layout_available()
            if self.enable_ocr and ocr.pdf_ocr_available() and ext == ".pdf":
                analysis = None
                try:
                    with debuglog.span("pdf.analysis") as sp:
                        analysis = ocr.analyze_pdf(source)
                        if analysis is not None:
                            sp["pages"] = analysis.total_pages
                            sp["scanned"] = len(analysis.scanned_pages)
                            sp["scanned_ratio"] = round(analysis.scanned_ratio, 3)
                            sp["needs_ocr"] = analysis.needs_ocr
                            sp["scanned_first"] = analysis.scanned_pages[:40]
                except Exception as exc:
                    failures.append(("pdf_analysis", exc))
                try:
                    if analysis is not None and analysis.needs_ocr:
                        engine = self._ocr_engine()
                        timer = debuglog.PageTimer("ocr+text")
                        with debuglog.span("route", route="ocr_pdf", layout=use_layout):
                            if use_layout:
                                # One pipeline for mixed PDFs: OCR supplies the
                                # scanned pages' text, the layout reader the rest
                                # (plus outline headings and figure markers).
                                res = pdflayout.extract_markdown(
                                    source,
                                    anchors=True,
                                    figure_markers=self.figure_markers,
                                    ocr_pages=frozenset(analysis.scanned_pages),
                                    ocr_page=engine.ocr_page,
                                    on_page=timer.wrap(inner_page),
                                )
                                result = ConversionResult(
                                    source=source,
                                    markdown=res.markdown,
                                    engine="ocr+text",
                                    doc_title=res.title,
                                    doc_author=res.author,
                                )
                            else:
                                md = engine.pdf_to_markdown(
                                    source, analysis, on_page=timer.wrap(inner_page)
                                )
                                result = ConversionResult(
                                    source=source, markdown=md, engine="ocr+text"
                                )
                        timer.emit()
                        return result
                except Exception as exc:
                    failures.append(("ocr_pdf", exc))

            # Digital PDFs: read the page geometry (columns, TrimBox, shadow
            # layers, bookmarks) rather than markitdown's row clustering,
            # which slices multi-column prose into tables.
            if use_layout and ext == ".pdf":
                try:
                    timer = debuglog.PageTimer("pdflayout")
                    with debuglog.span("route", route="pdflayout") as sp:
                        res = pdflayout.extract_markdown(
                            source,
                            anchors=self.page_anchors,
                            figure_markers=self.figure_markers,
                            on_page=timer.wrap(inner_page),
                        )
                        # A scanned PDF with OCR off has no text layer — let
                        # markitdown have a go instead of returning husks.
                        body = re.sub(r"<!-- (?:page|figure) [^>]*-->", "", res.markdown)
                        enough = len(body.strip()) >= 50
                        sp["body_chars"] = len(body.strip())
                        sp["outcome"] = "used" if enough else "too_little_text"
                    timer.emit()
                    if enough:
                        return ConversionResult(
                            source=source,
                            markdown=res.markdown,
                            engine="pdflayout",
                            doc_title=res.title,
                            doc_author=res.author,
                        )
                except Exception as exc:
                    failures.append(("pdflayout", exc))

            # Layout off (or failed): page anchors still need per-page
            # extraction, which markitdown flattens away.
            if self.page_anchors and ext == ".pdf" and ocr.pdfium_available():
                try:
                    inner = (
                        (lambda p, t: on_subprogress(source, p, t, "page"))
                        if on_subprogress is not None
                        else None
                    )
                    with debuglog.span("route", route="pdftext") as sp:
                        md = self._ocr_engine().pdf_text_to_markdown(source, on_page=inner)
                        # A scanned PDF with OCR off yields only empty anchors —
                        # let markitdown have a go instead of returning husks.
                        body = re.sub(r"<!-- page \d+ -->", "", md)
                        sp["body_chars"] = len(body.strip())
                    if len(body.strip()) >= 50:
                        return ConversionResult(
                            source=source, markdown=md, engine="pdftext"
                        )
                except Exception as exc:
                    failures.append(("pdftext", exc))

            if self.enable_audio and audio.audio_available() and ext in audio.MEDIA_EXTENSIONS:
                try:
                    inner = (
                        (lambda c, t: on_subprogress(source, c, t, "sec"))
                        if on_subprogress is not None
                        else None
                    )
                    with debuglog.span("route", route="whisper"):
                        md = self._audio_engine().transcribe_to_markdown(
                            source, on_progress=inner
                        )
                    return ConversionResult(source=source, markdown=md, engine="whisper")
                except Exception as exc:
                    failures.append(("whisper", exc))

            with debuglog.span("route", route="markitdown") as sp:
                result = self._md.convert(str(source))
                text = result.text_content or ""
                sp["chars"] = len(text)
            # markitdown quirk: unrecognized binary content (a corrupt docx,
            # an mp3 with broken magic bytes) can "convert" without error to
            # the literal string "None" (str(None) leaking from a converter),
            # a "| None |" table, or nothing at all. A non-trivial source
            # yielding no real text is a failure, not a success.
            has_text = any(
                m.group(0) != "None" for m in re.finditer(r"\w+", text)
            )
            if not has_text:
                try:
                    src_size = source.stat().st_size
                except OSError:
                    src_size = 0
                if src_size >= _EMPTY_OUTPUT_MIN_BYTES:
                    return ConversionResult(
                        source=source,
                        error=(
                            "EmptyOutputError: the converter produced no text "
                            f"from this {src_size:,}-byte file"
                        ),
                    )
                # A scan or photo with OCR off "converts" to nothing at any
                # size — and the fix is one click away, so say so.
                if not self.enable_ocr and (ext == ".pdf" or ext in ocr.OCR_IMAGE_EXTENSIONS):
                    return ConversionResult(
                        source=source,
                        error="EmptyOutputError: no text layer, and OCR is turned off",
                    )
            return ConversionResult(source=source, markdown=text, engine="markitdown")
        except Exception as exc:  # markitdown raises a variety of types
            failures.append(("markitdown", exc))
            return ConversionResult(source=source, error=f"{type(exc).__name__}: {exc}")

    # The warning a failed route leaves when a later route still produced
    # the document.
    _FALLBACK_CODES = {
        "mineru": "MS-114",
        "pdflayout": "MS-120",
        "pdf_analysis": "MS-121",  # untriaged: scanned pages went un-OCR'd
        "ocr_pdf": "MS-121",
        "ocr_image": "MS-122",
        "whisper": "MS-123",
        "pdftext": "MS-124",
    }

    @staticmethod
    def _route_failure_code(route: str, exc: BaseException) -> str:
        """Why the transcription / image-OCR route failed: the file itself
        (unreadable, undecodable, too big) or the engine."""
        text = f"{type(exc).__name__}: {exc}"
        own = errors.classify_conversion(text)
        if own in ("MS-102", "MS-107", "MS-108", "MS-109"):
            return own
        if route == "whisper":
            return "MS-111" if errors.is_network_error(text) else "MS-112"
        if "Image" in type(exc).__name__ or "cannot identify image" in text:
            return "MS-107"  # a broken picture, not a broken OCR engine
        return "MS-113"

    def _diagnose(
        self, result: ConversionResult, failures: list[tuple[str, BaseException]]
    ) -> None:
        """Give a failure its error code, and log every failed route.

        A specialised route's failure is usually the real cause of a later
        generic one — an mp3 whose speech model couldn't download reaches
        markitdown and "produces no text", but the useful message is about
        the download — so it takes precedence over the generic diagnosis.
        """
        source = result.source
        ext = source.suffix.lower()
        routes = dict(failures)
        where = f"convert {source.name}"
        if not result.ok:
            code = errors.classify_conversion(result.error or "")
            cause = routes.get("markitdown")
            specialised = next((r for r in ("whisper", "ocr_image") if r in routes), None)
            if specialised and code not in ("MS-101", "MS-102"):
                cause = routes[specialised]
                code = self._route_failure_code(specialised, cause)
            elif code in ("MS-105", "MS-106", "MS-199"):
                if ext in audio.MEDIA_EXTENSIONS and not self.enable_audio:
                    code = "MS-115"
                elif code == "MS-106" and not self.enable_ocr and (
                    ext == ".pdf" or ext in ocr.OCR_IMAGE_EXTENSIONS
                ):
                    code = "MS-110"
            incident = errors.report(
                code,
                exc=cause,
                detail=result.error or "",
                where=where,
                file=str(source),
                failed_routes=[f"{r}: {type(e).__name__}: {e}" for r, e in failures],
            )
            result.error_code, result.error_ref = incident.code, incident.ref
            return
        reported: set[str] = set()
        for route, exc in failures:
            # A file pdfium can't even open fails every PDF route the same
            # way: one accurate warning, not one per route.
            unreadable = type(exc).__name__ == "PdfiumError" and "load document" in str(exc)
            code = "MS-125" if unreadable else self._FALLBACK_CODES.get(route)
            if code is None or code in reported:
                continue
            reported.add(code)
            result.warnings.append(
                errors.report(
                    code, exc=exc, where=where, file=str(source), route=route, engine=result.engine
                )
            )

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


def explain_error(error: str) -> tuple[str, str]:
    """Plain-language (what happened, what to try) for a raw conversion error.

    The wording lives in the error-code catalog (errors.CATALOG); an
    unmatched error gets the generic MS-199 explanation — the raw error is
    still shown to the user as technical details, so nothing is lost.
    """
    spec = errors.CATALOG[errors.classify_conversion(error)]
    return spec.title, spec.next_step
