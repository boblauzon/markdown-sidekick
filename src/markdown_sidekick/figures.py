"""Extract embedded PDF figures to an assets folder and link them.

markitdown (and any text extraction) drops a PDF's images entirely — for
technical documents the diagrams are often the point. This module pulls the
embedded raster images out with pypdfium2, filters decorative noise (icons,
bullets, repeated logos), writes PNGs to an ``assets/`` folder, and splices
``![Figure]`` links into the Markdown — next to the right page when
``<!-- page N -->`` anchors are present, or as a trailing section otherwise.

Everything is defensive: a PDF that can't be walked yields an empty list, and
a single unreadable image never aborts the extraction.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import pypdfium2 as pdfium

    _PDFIUM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PDFIUM_AVAILABLE = False

# pypdfium2 page-object type id for images (see ocr.py).
_PDF_OBJ_IMAGE = 3
# Skip images smaller than this many pixels — icons, bullets, rules.
_MIN_PIXELS = 10_000
# Safety valve for pathological documents.
_MAX_FIGURES = 500

_ANCHOR_RE = re.compile(r"^<!-- page (\d+)(?: \([^)]*\))? -->\s*$")


@dataclass
class FigureRef:
    page: int  # 1-based
    path: Path
    width: int
    height: int
    caption: str = ""  # optional vision-model alt text


def figures_available() -> bool:
    return _PDFIUM_AVAILABLE


def extract_pdf_figures(
    pdf_path: str | Path,
    assets_dir: Path,
    *,
    min_pixels: int = _MIN_PIXELS,
) -> list[FigureRef]:
    """Write each meaningful embedded image to ``assets_dir`` as PNG.

    Images are de-duplicated by content hash, so a logo repeated on every
    page is written once (and linked once, at its first occurrence).
    """
    if not _PDFIUM_AVAILABLE:
        return []
    figures: list[FigureRef] = []
    seen: set[str] = set()
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        for i in range(len(pdf)):
            if len(figures) >= _MAX_FIGURES:
                break
            page = pdf[i]
            try:
                try:
                    objects = list(page.get_objects(max_depth=2))
                except Exception:
                    continue
                for obj in objects:
                    if getattr(obj, "type", None) != _PDF_OBJ_IMAGE:
                        continue
                    try:
                        bitmap = obj.get_bitmap(render=False)
                        try:
                            pil = bitmap.to_pil().convert("RGB")
                        finally:
                            bitmap.close()
                    except Exception:
                        continue  # one bad image must not kill the run
                    width, height = pil.size
                    if width * height < min_pixels:
                        continue
                    digest = hashlib.sha1(pil.tobytes()).hexdigest()[:12]
                    if digest in seen:
                        continue
                    seen.add(digest)
                    assets_dir.mkdir(parents=True, exist_ok=True)
                    path = assets_dir / f"fig-p{i + 1:04d}-{digest}.png"
                    try:
                        pil.save(path)
                    except Exception:
                        continue
                    figures.append(FigureRef(i + 1, path, width, height))
            finally:
                page.close()
    finally:
        pdf.close()
    return figures


def insert_figure_links(markdown: str, figures: list[FigureRef], rel_dir: str = "assets") -> str:
    """Splice ``![Figure]`` links into the document.

    With ``<!-- page N -->`` anchors present, each figure lands directly after
    its page's anchor. Without anchors there is no reliable position, so the
    figures are appended as an "Extracted figures" section instead.
    """
    if not figures:
        return markdown

    def link(fig: FigureRef) -> str:
        alt = fig.caption or f"Figure (page {fig.page}, {fig.width}×{fig.height})"
        return f"![{alt}]({rel_dir}/{fig.path.name})"

    by_page: dict[int, list[FigureRef]] = {}
    for fig in figures:
        by_page.setdefault(fig.page, []).append(fig)

    lines = markdown.split("\n")
    anchored_pages = {
        int(m.group(1)) for ln in lines if (m := _ANCHOR_RE.match(ln.strip()))
    }
    out: list[str] = []
    for line in lines:
        out.append(line)
        m = _ANCHOR_RE.match(line.strip())
        if m:
            for fig in by_page.get(int(m.group(1)), []):
                out.append("")
                out.append(link(fig))

    # Figures on pages without an anchor have no reliable inline position.
    leftovers = [f for f in figures if f.page not in anchored_pages]
    if leftovers:
        out.append("")
        out.append("## Extracted figures")
        out.append("")
        for fig in leftovers:
            out.append(f"- {link(fig)}")
    return "\n".join(out)
