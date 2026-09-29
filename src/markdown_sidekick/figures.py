"""Extract embedded PDF figures to an images folder and link them in place.

markitdown (and any text extraction) drops a PDF's images entirely — for
design and technical books the figures are often the point. This module pulls
the embedded raster images out with pypdfium2, filters decorative noise
(spacers, icons, rules and bullets under 120 px on a side, images outside the
page's TrimBox, repeated logos), writes them to an ``images/`` folder and
splices ``![Figure N.K](images/…)`` links into the Markdown:

- at the exact reading-order position when the layout engine left a
  ``<!-- figure N.K -->`` marker there (see :mod:`pdflayout`),
- else directly after the page's ``<!-- page N -->`` anchor,
- else in a trailing "Extracted figures" section.

JPEGs in RGB/greyscale are copied byte-for-byte (no re-encode, no quality
loss); anything else is decoded and saved as PNG (line art) or JPEG (photos).

Everything is defensive: a PDF that can't be walked yields an empty list, and
a single unreadable image never aborts the extraction.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

try:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    _PDFIUM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PDFIUM_AVAILABLE = False

# Both pixel dimensions must reach this — drops spacers, icons, bullets and
# thin rules while keeping every real figure.
MIN_SIDE_PX = 120
# And the image must be drawn at least this big on the page (points).
_MIN_SIDE_PT = 18
# Safety valve for pathological documents.
_MAX_FIGURES = 2000
# A decoded image with at most this many colours is line art -> PNG.
_PALETTE_COLOURS = 256
# A document is a scan when at least this share of its pages are covered
# (>= _SCAN_COVERAGE of the trimmed page) by a single image.
_SCAN_COVERAGE = 0.85
_SCAN_PAGE_SHARE = 0.5

_ANCHOR_RE = re.compile(r"^<!-- page (\d+)(?: \([^)]*\))? -->\s*$")
_MARKER_RE = re.compile(r"^<!-- figure (\d+)\.(\d+) -->\s*$")
# A marker line plus the blank line after it, so removal leaves one gap.
_MARKER_LINE_RE = re.compile(r"^<!-- figure \d+\.\d+ -->[ \t]*(?:\n(?:[ \t]*\n)?|$)", re.M)


@dataclass
class FigureRef:
    page: int  # 1-based
    path: Path
    width: int
    height: int
    caption: str = ""  # optional vision-model alt text
    index: int = 0  # 1-based position among the page's qualifying images


def figures_available() -> bool:
    return _PDFIUM_AVAILABLE


def _page_bbox(obj) -> tuple[float, float, float, float]:
    """An object's bounds in PAGE space: objects inside Form XObjects report
    bounds in the form's space, so apply each enclosing form's matrix."""
    left, bottom, right, top = obj.get_bounds()
    container = obj.container
    while container is not None:
        left, bottom, right, top = container.get_matrix().on_rect(left, bottom, right, top)
        container = container.container
    return min(left, right), min(bottom, top), max(left, right), max(bottom, top)


def page_images(page, clip=None):
    """Yield ``(k, image_obj, bbox)`` for the page's qualifying images, in
    content-stream order; ``k`` is 1-based among qualifying images only.

    The layout engine's figure markers and :func:`extract_pdf_figures` both
    enumerate through here, so marker ``N.K`` always names the same image.
    """
    k = 0
    try:
        objects = page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=4)
        for obj in objects:
            try:
                w, h = obj.get_px_size()
                if min(w, h) < MIN_SIDE_PX:
                    continue
                x0, y0, x1, y1 = _page_bbox(obj)
            except Exception:
                continue
            if min(x1 - x0, y1 - y0) < _MIN_SIDE_PT:
                continue
            if clip is not None:
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                if not (clip[0] - 1 <= cx <= clip[2] + 1 and clip[1] - 1 <= cy <= clip[3] + 1):
                    continue  # off the trimmed page (a spread's facing page)
            k += 1
            yield k, obj, (x0, y0, x1, y1)
    except Exception:
        return


def page_image_boxes(page, clip=None) -> list[tuple[int, tuple[float, float, float, float]]]:
    return [(k, bbox) for k, _obj, bbox in page_images(page, clip)]


def _passthrough_jpeg(obj) -> bool:
    """True when the stored JPEG can be written out verbatim: CMYK JPEGs
    (common in print PDFs) render wrongly in most viewers, so they are
    decoded instead."""
    try:
        if list(obj.get_filters()) != ["DCTDecode"]:
            return False
        meta = obj.get_metadata()
    except Exception:
        return False
    cs = meta.colorspace
    if cs in (
        pdfium_c.FPDF_COLORSPACE_DEVICEGRAY,
        pdfium_c.FPDF_COLORSPACE_DEVICERGB,
        pdfium_c.FPDF_COLORSPACE_CALGRAY,
        pdfium_c.FPDF_COLORSPACE_CALRGB,
    ):
        return True
    return cs == pdfium_c.FPDF_COLORSPACE_ICCBASED and meta.bits_per_pixel in (8, 24)


def _save_image(obj, stem: Path, raw: bytes) -> tuple[Path, int, int] | None:
    """``raw`` is the image's undecoded stream (already read for hashing)."""
    width, height = obj.get_px_size()
    if raw and _passthrough_jpeg(obj):
        path = stem.with_suffix(".jpg")
        path.write_bytes(raw)
        return path, width, height
    bitmap = obj.get_bitmap(render=False)
    try:
        # to_pil() SHARES the native buffer: convert (which copies) before
        # close(), or PIL reads freed memory — an access violation.
        pil = bitmap.to_pil()
        pil = pil.convert("L" if pil.mode in ("L", "1", "LA") else "RGB")
    finally:
        bitmap.close()
    if pil.getcolors(_PALETTE_COLOURS) is not None:
        path = stem.with_suffix(".png")
        pil.save(path, optimize=True)
    else:
        path = stem.with_suffix(".jpg")
        pil.save(path, quality=88)
    return path, pil.size[0], pil.size[1]


def _covers_page(bbox, clip) -> bool:
    x0, y0, x1, y1 = bbox
    cx0, cy0, cx1, cy1 = clip
    inter = max(0.0, min(x1, cx1) - max(x0, cx0)) * max(0.0, min(y1, cy1) - max(y0, cy0))
    return inter >= _SCAN_COVERAGE * max(1.0, (cx1 - cx0) * (cy1 - cy0))


def _is_scan(pdf) -> bool:
    """Is this a scanned document — most pages ARE one page-sized image?

    Its page images are the pages, not figures; a design book's occasional
    full-bleed photo is a figure and is kept."""
    from .pdflayout import clip_box

    total = len(pdf)
    if not total:
        return False
    need = _SCAN_PAGE_SHARE * total
    covered = 0
    for i in range(total):
        # Stop as soon as the verdict is settled — a digital book is known
        # not to be a scan once too few pages remain to reach the share.
        if covered >= need:
            return True
        if covered + (total - i) < need:
            return False
        page = pdf[i]
        try:
            clip = clip_box(page)
            if any(_covers_page(bbox, clip) for _k, _obj, bbox in page_images(page, clip)):
                covered += 1
        finally:
            page.close()
    return covered >= need


def extract_pdf_figures(pdf_path: str | Path, images_dir: Path) -> list[FigureRef]:
    """Write each meaningful embedded image to ``images_dir``.

    Files are named ``fig_p{page}_{k}.{jpg|png}``. Images are de-duplicated
    by stream content, so a logo repeated on every page is written once (and
    linked once, at its first occurrence).
    """
    if not _PDFIUM_AVAILABLE:
        return []
    from .pdflayout import clip_box

    figures: list[FigureRef] = []
    seen: set[str] = set()
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        scanned = _is_scan(pdf)
        for i in range(len(pdf)):
            if len(figures) >= _MAX_FIGURES:
                break
            page = pdf[i]
            try:
                clip = clip_box(page)
                for k, obj, bbox in page_images(page, clip):
                    if scanned and _covers_page(bbox, clip):
                        continue  # the page scan itself, not a figure on it
                    try:
                        raw = bytes(obj.get_data(decode_simple=False))
                        digest = hashlib.sha1(raw).hexdigest() if raw else f"{i}-{k}"
                        if digest in seen:
                            continue
                        seen.add(digest)
                        images_dir.mkdir(parents=True, exist_ok=True)
                        saved = _save_image(obj, images_dir / f"fig_p{i + 1}_{k}", raw)
                    except Exception:
                        continue  # one bad image must not kill the run
                    if saved is not None:
                        path, width, height = saved
                        figures.append(FigureRef(i + 1, path, width, height, index=k))
            finally:
                page.close()
    finally:
        pdf.close()
    return figures


def strip_figure_markers(markdown: str) -> str:
    """Remove unconsumed ``<!-- figure N.K -->`` markers (copy/preview/export
    without figure extraction)."""
    if "<!-- figure " not in markdown:
        return markdown
    return _MARKER_LINE_RE.sub("", markdown)


def insert_figure_links(markdown: str, figures: list[FigureRef], rel_dir: str = "images") -> str:
    """Splice ``![Figure N.K]`` links into the document.

    Figure markers are replaced in place (a marker whose image was filtered or
    de-duplicated is simply removed). Figures without a marker land after
    their page's ``<!-- page N -->`` anchor, or — with no reliable position —
    in a trailing "Extracted figures" section.
    """
    if not figures:
        return strip_figure_markers(markdown)

    def link(fig: FigureRef) -> str:
        label = f"Figure {fig.page}.{fig.index}" if fig.index else f"Figure (page {fig.page})"
        alt = (fig.caption or label).replace("[", "(").replace("]", ")")
        # Quoted: a folder named after "My Book.md" must not break the link.
        return f"![{alt}]({quote(f'{rel_dir}/{fig.path.name}')})"

    by_key = {(f.page, f.index): f for f in figures if f.index}
    placed: set[int] = set()
    by_page: dict[int, list[FigureRef]] = {}
    lines = markdown.split("\n")
    out: list[str] = []
    skip_blank = False
    for line in lines:
        if skip_blank and not line.strip():
            skip_blank = False
            continue  # a removed marker's trailing gap
        skip_blank = False
        m = _MARKER_RE.match(line.strip())
        if m:
            fig = by_key.get((int(m.group(1)), int(m.group(2))))
            if fig is not None and id(fig) not in placed:
                placed.add(id(fig))
                out.append(link(fig))
            else:
                skip_blank = True
            continue
        out.append(line)
    rest = [f for f in figures if id(f) not in placed]
    for fig in rest:
        by_page.setdefault(fig.page, []).append(fig)

    anchored: set[int] = set()
    final: list[str] = []
    for line in out:
        final.append(line)
        m = _ANCHOR_RE.match(line.strip())
        if m:
            page = int(m.group(1))
            anchored.add(page)
            for fig in by_page.get(page, []):
                final.append("")
                final.append(link(fig))

    # Figures on pages without an anchor have no reliable inline position.
    leftovers = [f for f in rest if f.page not in anchored]
    if leftovers:
        final.append("")
        final.append("## Extracted figures")
        final.append("")
        for fig in leftovers:
            final.append(f"- {link(fig)}")
    return "\n".join(final)
