"""Local OCR pipeline for images and scanned PDFs.

Uses **RapidOCR** (ONNX, Apache-2.0) for text recognition — on the GPU via
DirectML when one is available (any DX12 card: AMD/NVIDIA/Intel; measured
5.8x faster than CPU on an RX 5700), controlled by ``Settings.ocr_device`` —
and **pypdfium2** (Apache/BSD) for PDF page-text inspection and rendering.
pypdfium2 is used deliberately instead of PyMuPDF, which is AGPL-licensed and
would be awkward for a distributable app.

RapidOCR v3 downloads its ONNX models (~15 MB) once on first use — the frozen
build bundles them (they must exist in the build venv's site-packages before
PyInstaller runs; ``--selftest``'s image_ocr check proves the bundle worked).

Both dependencies are imported defensively: if either is missing the app still
runs and simply falls back to markitdown for everything.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# RapidOCR pulls in onnxruntime + opencv, which are slow to import. Detect it
# cheaply with find_spec and only import it lazily on first OCR use, so the app
# launches fast when no OCR is needed.
_RAPIDOCR_AVAILABLE = importlib.util.find_spec("rapidocr") is not None

try:
    import numpy as np  # already loaded by markitdown's deps; cheap here
    import pypdfium2 as pdfium  # thin C binding, fast to import

    _PDFIUM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PDFIUM_AVAILABLE = False


# Image formats routed through OCR when it is enabled.
OCR_IMAGE_EXTENSIONS: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
)

# A PDF page needs OCR when it has almost no extractable text AND still renders
# visual content — either a raster image covering much of the page (a true scan)
# or many vector path objects (text drawn as outlined glyphs, common in
# "print-to-PDF"/web exports, which carry no text layer).
_SCANNED_PAGE_TEXT_THRESHOLD = 24
_IMAGE_COVERAGE_THRESHOLD = 0.45
_MIN_VECTOR_PATHS = 12
# Only switch a whole PDF onto the OCR path when a meaningful share of pages look
# scanned — this stops a single full-page figure in a digital book from
# dragging the entire book off the (higher-quality) markitdown path.
SCANNED_DOC_RATIO = 0.15

# pypdfium2 page-object type ids.
_PDF_OBJ_IMAGE = 3
_PDF_OBJ_PATH = 2


def ocr_available() -> bool:
    """True if image OCR can run."""
    return _RAPIDOCR_AVAILABLE


def pdf_ocr_available() -> bool:
    """True if scanned-PDF OCR (render + recognise) can run."""
    return _RAPIDOCR_AVAILABLE and _PDFIUM_AVAILABLE


def pdfium_available() -> bool:
    """True if pypdfium2 alone is importable (page-anchor text extraction
    and figure extraction need no OCR engine)."""
    return _PDFIUM_AVAILABLE


@dataclass
class PdfAnalysis:
    """Per-page triage of a PDF used to decide on the conversion route."""

    total_pages: int
    scanned_pages: list[int]  # 0-based page indices that need OCR

    @property
    def scanned_ratio(self) -> float:
        return len(self.scanned_pages) / self.total_pages if self.total_pages else 0.0

    @property
    def needs_ocr(self) -> bool:
        return self.scanned_ratio >= SCANNED_DOC_RATIO


def _page_has_visual_content(page) -> bool:
    """True if a (near-)textless page still renders meaningful content.

    Catches both true scans (a raster image covering most of the page) and pages
    whose text is drawn as vector outlines (many path objects, no text layer).
    """
    try:
        width, height = page.get_size()
        page_area = width * height
        if page_area <= 0:
            return False
        image_area = 0.0
        path_count = 0
        for obj in page.get_objects():
            kind = getattr(obj, "type", None)
            if kind == _PDF_OBJ_IMAGE:
                try:
                    left, bottom, right, top = obj.get_bounds()
                except Exception:
                    continue  # one bad object must not abort detection
                image_area += abs((right - left) * (top - bottom))
            elif kind == _PDF_OBJ_PATH:
                path_count += 1
        coverage = image_area / page_area
        return coverage >= _IMAGE_COVERAGE_THRESHOLD or path_count >= _MIN_VECTOR_PATHS
    except Exception:
        return False


def analyze_pdf(path: str | Path) -> PdfAnalysis | None:
    """Inspect every page; flag those that look like scanned images.

    Returns ``None`` if PDF tooling is unavailable.
    """
    if not _PDFIUM_AVAILABLE:
        return None
    pdf = pdfium.PdfDocument(str(path))
    try:
        scanned: list[int] = []
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                textpage = page.get_textpage()
                try:
                    # Use *visible* text length (stripped) — count_chars() also
                    # counts whitespace / invisible glyphless-OCR characters and
                    # would wrongly judge such a scanned page as a text page.
                    text_len = len(textpage.get_text_range().strip())
                finally:
                    textpage.close()
                if text_len < _SCANNED_PAGE_TEXT_THRESHOLD and _page_has_visual_content(page):
                    scanned.append(i)
            finally:
                page.close()
        return PdfAnalysis(total_pages=len(pdf), scanned_pages=scanned)
    finally:
        pdf.close()


OCR_DEVICES = ("auto", "cpu", "gpu")


class OcrEngine:
    """Lazy wrapper around RapidOCR plus pypdfium2 page rendering.

    ``device``: "auto" lets RapidOCR pick (DirectML on Windows 10+ with a
    DX12 GPU, CPU otherwise), "gpu" forces DirectML, "cpu" forces CPU.
    onnxruntime falls back to the CPU provider on its own if DirectML can't
    initialise, so "auto"/"gpu" never break OCR on GPU-less machines.
    """

    def __init__(self, render_scale: float = 2.0, device: str = "auto") -> None:
        self._engine = None
        self.render_scale = render_scale
        self.device = device if device in OCR_DEVICES else "auto"

    def _rapidocr(self):
        if self._engine is None:
            from rapidocr import RapidOCR

            device = self.device
            if device == "auto":
                # RapidOCR's own default stays on the CPU even when DirectML
                # is available (measured 3.5s vs 0.7s/page) — resolve "auto"
                # ourselves from the installed onnxruntime build.
                import onnxruntime

                device = (
                    "gpu"
                    if "DmlExecutionProvider" in onnxruntime.get_available_providers()
                    else "cpu"
                )
            self._engine = RapidOCR(
                params={"EngineConfig.onnxruntime.use_dml": device == "gpu"}
            )
        return self._engine

    def _recognise(self, image) -> str:
        """Run OCR on a path or numpy array; return recognised lines joined."""
        result = self._rapidocr()(image)
        if result is None or not result.txts:
            return ""
        # txts follow RapidOCR's reading order.
        return "\n".join(result.txts)

    def image_to_markdown(self, path: str | Path) -> str:
        source = Path(path)
        body = self._recognise(str(source)).strip()
        if not body:
            body = "_(No text detected in image.)_"
        return f"# OCR: {source.name}\n\n{body}\n"

    def pdf_text_to_markdown(
        self,
        path: str | Path,
        on_page: Callable[[int, int], None] | None = None,
    ) -> str:
        """Extract a digital PDF's text layer page by page with page anchors.

        Used when the user wants ``<!-- page N -->`` markers for citation
        grounding: markitdown flattens pages away, so the per-page extraction
        has to happen here. Output quality matches markitdown's PDF path
        (both are plain text layers).
        """
        pdf = pdfium.PdfDocument(str(path))
        parts: list[str] = []
        try:
            total = len(pdf)
            for i in range(total):
                page = pdf[i]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range().strip()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                parts.append(f"<!-- page {i + 1} -->\n\n{text}")
                if on_page is not None:
                    on_page(i + 1, total)
        finally:
            pdf.close()
        return "\n\n".join(parts).strip() + "\n"

    def pdf_to_markdown(
        self,
        path: str | Path,
        analysis: PdfAnalysis,
        on_page: Callable[[int, int], None] | None = None,
    ) -> str:
        """Convert a scanned/mixed PDF: OCR image pages, keep text pages' text."""
        pdf = pdfium.PdfDocument(str(path))
        scanned = set(analysis.scanned_pages)
        total = analysis.total_pages
        parts: list[str] = []
        try:
            for i in range(total):
                page = pdf[i]
                try:
                    if i in scanned:
                        bitmap = page.render(scale=self.render_scale)
                        try:
                            pil = bitmap.to_pil().convert("RGB")
                        finally:
                            bitmap.close()  # free the native render buffer
                        text = self._recognise(np.asarray(pil)).strip()
                        tag = "ocr"
                    else:
                        textpage = page.get_textpage()
                        try:
                            text = textpage.get_text_range().strip()
                        finally:
                            textpage.close()
                        tag = "text"
                finally:
                    page.close()
                parts.append(f"<!-- page {i + 1} ({tag}) -->\n\n{text}")
                if on_page is not None:
                    on_page(i + 1, total)
        finally:
            pdf.close()
        return "\n\n".join(parts).strip() + "\n"
