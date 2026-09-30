"""Column-aware text extraction for digital PDFs (pypdfium2).

markitdown's PDF path clusters words into rows across the WHOLE page, so
multi-column prose comes out as Markdown tables read left-to-right across the
columns, and margin notes are spliced into the middle of body sentences. Print
PDFs add their own contamination: InDesign slug lines, job tickets and the
facing page of a spread all sit outside the TrimBox, and drop-shadow or
fake-bold type is drawn twice, which text extractors emit as doubled
characters ("DDrraawwiinngg").

This module reads the page geometrically instead:

1. **Clip** every character to the TrimBox (falling back to CropBox), which
   removes prepress slugs, crop-mark labels and spread overflow at the source.
2. **Dedupe** characters drawn on top of an identical character (shadow and
   fake-bold layers).
3. Build **line segments** in content-stream order, splitting at line breaks
   and at gaps wider than a word space.
4. Order segments with a recursive **XY-cut**: columns (vertical whitespace
   channels spanning the whole region) before rows, so each column is read
   top to bottom. Regions whose columns form a numeric grid become Markdown
   tables; TOC-like grids and key/value layouts are read row by row instead.
5. **Emit** paragraphs reflowed with a "would the next word have fit?" test,
   fixed-pitch runs as fenced code with their indentation rebuilt from the
   geometry, bookmark titles as headings, and optional page anchors / figure
   markers.

Document-level passes then repair ligatures the font failed to map (control
characters standing in for "ff"/"fi"…) and resolve line-end hyphens against
the document's own vocabulary ("devi-ation" joins, "aesthetic-usability"
keeps its hyphen).

Everything is heuristic but geometric, and each heuristic errs towards the
reading order the PDF itself stores — a region with no clean cut keeps its
content-stream order.
"""

from __future__ import annotations

import ctypes
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Callable

try:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    _PDFIUM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PDFIUM_AVAILABLE = False


def layout_available() -> bool:
    return _PDFIUM_AVAILABLE


# ---------------------------------------------------------------------------
# Tunables (all relative to the local text height unless noted)
# ---------------------------------------------------------------------------
# A gap wider than this (x line height) ends a segment: wider than any word
# space, narrower than a column gutter or a table's cell spacing.
_SEGMENT_GAP = 0.8
# A column cut needs a vertical whitespace channel at least this wide.
_COLUMN_GAP = 0.5
# Row cuts: a gap must exceed the region's typical line gap by this much.
_ROW_GAP_EXTRA = 0.3
_ROW_GAP_MIN = 0.4
_ROW_GAP_ALWAYS = 1.2
# Characters count as the same glyph drawn twice when their boxes coincide
# within these fractions of the glyph size (shadow / fake-bold layers are
# offset by far less than one advance width).
_DUP_DX = 0.3
_DUP_DY = 0.3
# A document whose text is mostly fixed-pitch uses it as the body font —
# fencing it would turn the whole book into one code block.
_MONO_BODY_SHARE = 0.35
_MAX_HEADING_LEVEL = 4

_MONO_FONT_RE = re.compile(
    r"mono|courier|consol|menlo|monaco|inconsolata|sourcecode|source ?code|firacode"
    r"|fira ?code|lucida ?console|lucida ?sans ?typewriter|andale|jetbrains|sfmono"
    r"|cascadia|letter ?gothic|prestige|cousine|anonymous|ocr-?[ab]\b|typewriter"
    r"|\bhack\b|ptmono|dejavusansmono",
    re.IGNORECASE,
)
_BULLET_START_RE = re.compile(r"^(?:[•▪■●○◦‣►▶✓✔□–—*]|\d{1,3}[.)]|[a-z][.)])\s")
_LIST_MARKER_RE = re.compile(r"^\s*(?:[•▪■●○◦‣►▶✓✔□–—*\-]|\d{1,3}[.)]|[a-z][.)])\s*$")
_PAGE_REF_RE = re.compile(r"^(?:\d{1,4}|[ivxlcdm]{1,7})$", re.IGNORECASE)
_SENTENCE_END = (".", "!", "?", ":", '"', "”", "’", "…")

# Outline entries that are front matter, not document structure: they are
# not turned into headings, so their pages fall into the lead "front matter"
# section of a chapter export.
_FRONT_MATTER_TITLES = frozenset(
    {
        "cover", "frontcover", "backcover", "title", "titlepage", "halftitle",
        "halftitlepage", "copyright", "copyrightpage", "copyrights",
        "copyrightandcredits", "dedication", "dedications", "contents",
        "tableofcontents", "toc", "epigraph", "frontispiece", "packtpage",
        "blankpage", "blank", "seriespage", "endpapers", "imprint",
    }
)


@dataclass
class OutlineEntry:
    level: int  # 0 = top level
    title: str
    page: int  # 0-based page index


@dataclass
class LayoutStats:
    pages: int = 0
    chars: int = 0
    clipped_chars: int = 0  # outside the TrimBox (slugs, spread overflow)
    duplicate_chars: int = 0  # shadow / fake-bold layers
    rotated_chars: int = 0  # off the page's dominant orientation
    tables: int = 0
    code_blocks: int = 0
    headings: int = 0
    ligatures_repaired: int = 0


@dataclass
class LayoutResult:
    markdown: str
    title: str = ""
    author: str = ""
    stats: LayoutStats = field(default_factory=LayoutStats)


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------
@dataclass
class _Seg:
    """A run of text on one line (or a figure placeholder)."""

    text: str
    x0: float
    y0: float  # bottom (PDF space: y grows upward)
    x1: float
    y1: float  # top
    h: float  # text height (loose glyph box)
    cw: float  # average advance per character
    mono: bool
    idx: int  # content-stream order
    kind: str = "text"  # "text" | "fig"
    key: str = ""  # figure marker key for kind == "fig"
    # (word, x0, x1) — table columns and hanging indents need word geometry
    words: list = field(default_factory=list)


@dataclass
class _Line:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    h: float
    cw: float
    mono: bool
    text_x0: float = 0.0  # where the text starts after a leading bullet


def _median_h(items: list[_Seg]) -> float:
    hs = [s.h for s in items if s.kind == "text"]
    return median(hs) if hs else 10.0


def _rotate_box(x0, y0, x1, y1, quad):
    """Rotate a box by -quad*90 degrees so text of that orientation reads
    left-to-right with y up."""
    if quad == 0:
        return x0, y0, x1, y1
    pts = ((x0, y0), (x1, y1))
    if quad == 1:
        pts = [(y, -x) for x, y in pts]
    elif quad == 2:
        pts = [(-x, -y) for x, y in pts]
    else:
        pts = [(-y, x) for x, y in pts]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _is_garbage_char(ch: str) -> bool:
    return unicodedata.category(ch) in ("Cc", "Co") and ch not in "\n\t\x02"


# ---------------------------------------------------------------------------
# Page -> segments
# ---------------------------------------------------------------------------
def clip_box(page) -> tuple[float, float, float, float]:
    """TrimBox clipped to the MediaBox; CropBox/MediaBox when it is absent or
    degenerate (pypdfium2 already falls back to the CropBox)."""
    try:
        mx0, my0, mx1, my1 = page.get_mediabox()
        tx0, ty0, tx1, ty1 = page.get_trimbox()
    except Exception:
        w, h = page.get_size()
        return 0.0, 0.0, w, h
    x0, y0 = max(mx0, min(tx0, tx1)), max(my0, min(ty0, ty1))
    x1, y1 = min(mx1, max(tx0, tx1)), min(my1, max(ty0, ty1))
    media_area = max(1.0, (mx1 - mx0) * (my1 - my0))
    if x1 - x0 <= 0 or y1 - y0 <= 0 or (x1 - x0) * (y1 - y0) < 0.2 * media_area:
        return mx0, my0, mx1, my1
    return x0, y0, x1, y1


def _read_chars(textpage):
    """Pass 1 (ctypes): every visible character with its loose box.

    Returns (chars, quad_counts) where chars are tuples
    ``(ch, x0, y0, x1, y1, quad, mono, space_before, break_before, index, obj)``.
    Generated characters (pdfium's inferred spaces / line breaks) become the
    space_before / break_before hints of the next real character.
    """
    n = pdfium_c.FPDFText_CountChars(textpage)
    rect = pdfium_c.FS_RECTF()
    buf = ctypes.create_string_buffer(128)
    flags = ctypes.c_int()
    obj_info: dict[int, tuple[int | None, bool]] = {}
    chars = []
    quads: Counter[int] = Counter()
    space = brk = False
    last_addr = None
    info: tuple[int | None, bool] = (0, False)
    for i in range(n):
        u = pdfium_c.FPDFText_GetUnicode(textpage, i)
        obj = pdfium_c.FPDFText_GetTextObject(textpage, i)
        addr = ctypes.cast(obj, ctypes.c_void_p).value
        if not addr:  # generated by pdfium
            if u in (10, 13):
                brk = True
            else:
                space = True
            continue
        if u in (0xA0, 9, 13) and chars and chars[-1][10] == addr and chars[-1][0].isalpha() and not space:
            # Mid-word in one text object: some font subsets encode the
            # "ff" ligature as NBSP, TAB or CR ("di\xa0erent"). Kept (as
            # NBSP) for the ligature repair, which turns it back into a
            # space if no word results; a real line break still splits the
            # segment geometrically.
            u = 0xA0
        elif u in (32, 9, 0xA0, 0x3000):
            space = True
            continue
        if u in (10, 13) or u >= 0x110000:
            brk = brk or u in (10, 13)
            continue
        if u == 0:
            u = 0x1A  # unmapped glyph (usually a ligature): keep a placeholder
        if 0xDC00 <= u <= 0xDFFF:
            # pdfium reports characters beyond the BMP as UTF-16 surrogate
            # halves: fold the low half into the preceding high half.
            if chars and 0xD800 <= ord(chars[-1][0]) <= 0xDBFF:
                hi = ord(chars[-1][0])
                pair = chr(0x10000 + ((hi - 0xD800) << 10) + (u - 0xDC00))
                chars[-1] = (pair,) + chars[-1][1:]
            continue
        if addr != last_addr:
            last_addr = addr
            cached = obj_info.get(addr)
            if cached is None:
                angle = pdfium_c.FPDFText_GetCharAngle(textpage, i)
                quad: int | None = 0
                if angle >= 0:
                    q = round(angle / (math.pi / 2))
                    quad = q % 4 if abs(angle - q * math.pi / 2) < 0.1 else None
                need = pdfium_c.FPDFText_GetFontInfo(textpage, i, buf, len(buf), ctypes.byref(flags))
                if need > len(buf):  # pdfium writes nothing when it doesn't fit
                    buf = ctypes.create_string_buffer(need)
                    pdfium_c.FPDFText_GetFontInfo(textpage, i, buf, need, ctypes.byref(flags))
                name = buf.value.decode("latin-1", "replace") if need else ""
                mono = bool(flags.value & 1) or bool(_MONO_FONT_RE.search(name))
                cached = (quad, mono)
                obj_info[addr] = cached
            info = cached
        pdfium_c.FPDFText_GetLooseCharBox(textpage, i, ctypes.byref(rect))
        x0, x1 = sorted((rect.left, rect.right))
        y0, y1 = sorted((rect.bottom, rect.top))
        chars.append((chr(u), x0, y0, x1, y1, info[0], info[1], space, brk, i, addr))
        if info[0] is not None:
            quads[info[0]] += 1
        space = brk = False
    return chars, quads


def _is_duplicate(grid, ch, mx, my, w, h, obj, idx) -> bool:
    """Is this glyph a second copy of one already kept (shadow / fake-bold)?

    The components of a ligature ("fi" -> 'f','i') share ONE box, so glyphs
    adjacent in the same text object are never duplicates of each other.
    """
    gx0, gy0 = int(mx // 4), int(my // 4)
    for gx in (gx0 - 1, gx0, gx0 + 1):
        for gy in (gy0 - 1, gy0, gy0 + 1):
            for och, ox, oy, ow, oh, oobj, oidx in grid.get((gx, gy), ()):
                if (
                    abs(ox - mx) <= _DUP_DX * max(w, ow, 0.5)
                    and abs(oy - my) <= _DUP_DY * max(h, oh)
                    and abs(ow - w) <= 0.35 * max(w, ow, 0.5)
                    and abs(oh - h) <= 0.25 * max(h, oh)
                    and (och == ch or (_is_garbage_char(och) and _is_garbage_char(ch)))
                    and not (oobj == obj and abs(oidx - idx) <= 3)
                ):
                    return True
    return False


def _segments(chars, quad, clip, stats: LayoutStats) -> list[_Seg]:
    """Pass 2: clip, dedupe and group characters into line segments."""
    cx0, cy0, cx1, cy1 = _rotate_box(*clip, quad)
    grid: dict[tuple[int, int], list] = {}
    segs: list[_Seg] = []
    # current segment state
    parts: list[str] = []
    words: list[tuple[str, float, float]] = []
    wchars: list[str] = []
    wx0 = wx1 = 0.0
    s_x0 = s_y0 = s_x1 = s_y1 = 0.0
    s_hs: list[float] = []
    s_w = 0.0
    s_n = s_mono = s_idx = 0
    last_x0 = last_w = 0.0
    last_obj = last_idx = -1

    def flush() -> None:
        if wchars:
            words.append(("".join(wchars), wx0, wx1))
            wchars.clear()
        if parts:
            text = "".join(parts).strip()
            if text:
                segs.append(
                    _Seg(
                        text, s_x0, s_y0, s_x1, s_y1, median(s_hs), s_w / max(1, s_n),
                        s_mono >= 0.6 * s_n, s_idx, words=list(words),
                    )
                )
        parts.clear()
        words.clear()

    for ch, x0, y0, x1, y1, q, mono, space, brk, idx, obj in chars:
        if q != quad:
            stats.rotated_chars += 1
            continue
        x0, y0, x1, y1 = _rotate_box(x0, y0, x1, y1, quad)
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        if not (cx0 - 1 <= mx <= cx1 + 1 and cy0 - 1 <= my <= cy1 + 1):
            stats.clipped_chars += 1
            continue
        w, h = x1 - x0, y1 - y0
        if h <= 0:
            continue
        if _is_duplicate(grid, ch, mx, my, w, h, obj, idx):
            stats.duplicate_chars += 1
            continue
        grid.setdefault((int(mx // 4), int(my // 4)), []).append((ch, mx, my, w, h, obj, idx))
        stats.chars += 1

        glue = False  # attach with no space (ligature component / superscript)
        if parts:
            h_ref = median(s_hs)
            cw = s_w / max(1, s_n)
            gap = x0 - s_x1
            overlap = min(y1, s_y1) - max(y0, s_y0)
            ligature = obj == last_obj and idx <= last_idx + 3 and abs(x0 - last_x0) < 0.25 * max(w, 0.5)
            same_line = (
                not brk
                and overlap >= 0.5 * min(h, h_ref)
                and 0.55 <= h / h_ref <= 1.8
                and (ligature or gap >= -max(1.05 * last_w, 0.6 * cw))
            )
            seg_mono = s_mono >= 0.6 * s_n
            if (
                same_line
                and not ligature
                and not (seg_mono and mono)
                and gap > _SEGMENT_GAP * h_ref
                # A list marker set apart from its item ("•    item") stays
                # attached, or the XY-cut reads a column of bare bullets.
                and not (gap <= 4 * h_ref and _LIST_MARKER_RE.match("".join(parts)))
            ):
                same_line = False
            if not same_line:
                flush()
            elif ligature or gap < 0:
                glue = True
            elif h < 0.8 * h_ref and y0 > s_y0 + 0.25 * h_ref and gap < 0.6 * cw:
                glue = True  # superscript note marker: "designs.1"
            elif seg_mono and mono and gap > 0.5 * cw:
                parts.append(" " * max(1, round(gap / max(cw, 0.1))))
            elif space or gap > 0.45 * cw:
                if parts[-1] != " ":
                    parts.append(" ")
            if parts and parts[-1].startswith(" ") and wchars and not glue:
                words.append(("".join(wchars), wx0, wx1))
                wchars.clear()
        if not parts:
            s_x0, s_y0, s_x1, s_y1 = x0, y0, x1, y1
            s_hs = [h]
            s_w, s_n, s_mono, s_idx = w, 1, int(mono), idx
        else:
            s_x0, s_y0 = min(s_x0, x0), min(s_y0, y0)
            s_x1, s_y1 = max(s_x1, x1), max(s_y1, y1)
            if len(s_hs) < 64:
                s_hs.append(h)
            if not glue or not (obj == last_obj and abs(x0 - last_x0) < 0.25 * max(w, 0.5)):
                s_w += w  # a ligature's second component adds no width
            s_n += 1
            s_mono += int(mono)
        parts.append(ch)
        if not wchars:
            wx0, wx1 = x0, x1
        wchars.append(ch)
        wx1 = max(wx1, x1)
        last_x0, last_w, last_obj, last_idx = x0, w, obj, idx
    flush()
    return segs


# ---------------------------------------------------------------------------
# Reading order: recursive XY-cut
# ---------------------------------------------------------------------------
def _split_columns(items: list[_Seg], min_gap: float) -> list[list[_Seg]]:
    ordered = sorted(items, key=lambda s: s.x0)
    groups: list[list[_Seg]] = [[ordered[0]]]
    end = ordered[0].x1
    for s in ordered[1:]:
        if s.x0 - end >= min_gap:
            groups.append([s])
        else:
            groups[-1].append(s)
        end = max(end, s.x1)
    return groups


def _row_intervals(items: list[_Seg], min_gap: float = 0.0):
    """Merged vertical extents, top-down: [(top, bottom, members)]."""
    ordered = sorted(items, key=lambda s: -s.y1)
    rows: list[list] = []
    for s in ordered:
        if rows and s.y1 > rows[-1][1] - min_gap:
            rows[-1][1] = min(rows[-1][1], s.y0)
            rows[-1][2].append(s)
        else:
            rows.append([s.y1, s.y0, [s]])
    return rows


def _split_rows(items: list[_Seg], h: float):
    rows = _row_intervals(items)
    if len(rows) < 2:
        return None
    gaps = [rows[k][1] - rows[k + 1][0] for k in range(len(rows) - 1)]
    positive = [g for g in gaps if g > 0]
    if not positive:
        return None
    if len(gaps) >= 3:
        thr = max(_ROW_GAP_MIN * h * 0.6, median(gaps) + _ROW_GAP_EXTRA * h)
    else:
        thr = _ROW_GAP_MIN * h
    # A gap taller than a line is never inside a paragraph — cut it even when
    # the region is mostly such gaps (heading / text / figure / text), where
    # the median rule alone would see nothing unusual.
    thr = min(thr, _ROW_GAP_ALWAYS * h)
    best = max(range(len(gaps)), key=lambda k: gaps[k])
    if gaps[best] < thr:
        return None
    upper = [s for r in rows[: best + 1] for s in r[2]]
    lower = [s for r in rows[best + 1 :] for s in r[2]]
    return upper, lower


def _key_value(cols: list[list[_Seg]], h: float) -> bool:
    """A narrow left column of labels whose rows line up with the right
    column's rows ("Term  definition…") reads row by row, not column by
    column."""
    if len(cols) != 2:
        return False
    left, right = cols
    lx1 = max(s.x1 for s in left)
    lx0 = min(s.x0 for s in left)
    rx1 = max(s.x1 for s in right)
    if lx1 - lx0 > 0.5 * (rx1 - lx0):
        return False
    lrows = _row_intervals(left, 0.3 * h)
    rrows = _row_intervals(right, 0.3 * h)
    if len(lrows) < 2 or len(rrows) < 2:
        return False
    if any(len(r[2]) > 3 for r in lrows):
        return False  # a label is a line or three, not a paragraph

    def aligned(a, b) -> float:
        tops = [r[0] for r in b]
        return sum(1 for r in a if any(abs(r[0] - t) <= 0.6 * h for t in tops)) / len(a)

    return aligned(lrows, rrows) >= 0.7 and aligned(rrows, lrows) >= 0.6


def _bands(items: list[_Seg]) -> list[list[_Seg]]:
    """Group items into baseline bands, top-down."""
    ordered = sorted(items, key=lambda s: -(s.y0 + s.y1) / 2)
    bands: list[list] = []  # [y0, y1, members]
    for s in ordered:
        placed = False
        for band in bands[-3:]:
            ov = min(s.y1, band[1]) - max(s.y0, band[0])
            if ov >= 0.5 * min(s.h, band[1] - band[0]):
                band[0], band[1] = min(band[0], s.y0), max(band[1], s.y1)
                band[2].append(s)
                placed = True
                break
        if not placed:
            bands.append([s.y0, s.y1, [s]])
    bands.sort(key=lambda b: -b[1])
    return [b[2] for b in bands]


def _word_columns(bands: list[list[_Seg]], min_gap: float) -> list[tuple[float, float]]:
    """Column intervals from the x-projection of every word in the region: a
    column boundary is a vertical channel no word in ANY row crosses."""
    spans = sorted(
        (x0, x1) for band in bands for s in band for _w, x0, x1 in (s.words or [(s.text, s.x0, s.x1)])
    )
    if not spans:
        return []
    cols = [list(spans[0])]
    for x0, x1 in spans[1:]:
        if x0 - cols[-1][1] >= min_gap:
            cols.append([x0, x1])
        else:
            cols[-1][1] = max(cols[-1][1], x1)
    return [(a, b) for a, b in cols]


def _grid_rows(items: list[_Seg], min_gap: float) -> tuple[list[list[str]], str]:
    """(rows of cells, caption). A first row that is one text run spanning
    several columns is the grid's title, not a row — splitting it into cells
    would also pad the table with phantom columns."""
    bands = _bands(items)
    caption = ""
    if len(bands) >= 4 and len(bands[0]) == 1:
        head = bands[0][0]
        rest = _word_columns(bands[1:], min_gap)
        if len(rest) >= 2 and sum(1 for a, b in rest if head.x0 <= b and head.x1 >= a) >= 2:
            caption, bands = head.text, bands[1:]
    columns = _word_columns(bands, min_gap)
    rows: list[list[str]] = []
    for band in bands:
        cells = [""] * len(columns)
        for s in sorted(band, key=lambda s: s.x0):
            for word, x0, x1 in s.words or [(s.text, s.x0, s.x1)]:
                mid = (x0 + x1) / 2
                k = next((j for j, (a, b) in enumerate(columns) if a - 1 <= mid <= b + 1), 0)
                cells[k] = (cells[k] + " " + word).strip()
        rows.append(cells)
    return rows, caption


def _digit_share(cell: str) -> float:
    chars = [c for c in cell if not c.isspace()]
    return sum(c.isdigit() for c in chars) / len(chars) if chars else 0.0


def _grid(items: list[_Seg], h: float, channel: bool) -> tuple[str, tuple] | None:
    """Classify a region as a grid: ("table", (rows, caption)) for a numeric
    grid, ("rows", (rows, caption)) for TOC-like title/page-number lines,
    None for prose.

    ``channel`` says the XY-cut already found whitespace columns here; without
    one (a leaf that no cut could split) the evidence bar is higher.
    """
    if any(s.kind != "text" for s in items) or len(items) < 3:
        return None
    rows, caption = _grid_rows(items, max(1.5, 0.35 * h))
    if not rows or len(rows[0]) < 2:
        return None
    ncols = len(rows[0])
    multi = [r for r in rows if sum(1 for c in r if c) >= 2]
    if len(multi) < 3 or len(multi) < (0.6 if channel else 0.75) * len(rows):
        return None
    flat = [c for r in rows for c in r if c]
    # TOC / index: an outer column of bare page refs beside text.
    for k in (0, ncols - 1):
        col = [r[k] for r in rows if r[k]]
        others = [c for r in rows for j, c in enumerate(r) if j != k and c]
        if (
            len(col) >= 3
            and len(col) >= 0.6 * len(rows)
            and sum(1 for c in col if _PAGE_REF_RE.match(c)) >= 0.7 * len(col)
            and others
            and sum(1 for c in others if re.search(r"[A-Za-z]{2}", c)) >= 0.7 * len(others)
        ):
            return "rows", (rows, caption)
    words = [len(c.split()) for c in flat]
    prose = sum(
        1 for c, w in zip(flat, words) if w >= 5 or c.endswith(("-", "\x02", "\xad"))
    )
    # A numeric cell is MOSTLY digits ("1.586", "R 119 G 132") — merely
    # containing one ("02. Allegory", "Acme Forklift 1300A") is a list or a
    # label, and those read better as columns.
    numeric = sum(1 for c, w in zip(flat, words) if w <= 3 and _digit_share(c) >= 0.4)
    if channel:
        ok = prose <= 0.15 * len(flat) and numeric >= 0.35 * len(flat)
    else:
        ok = ncols >= 3 and prose <= 0.1 * len(flat) and numeric >= 0.5 * len(flat)
    if ok and median(len(c) for c in flat) <= 24:
        return "table", (rows, caption)
    return None


def _xy_cut(items: list[_Seg], out: list, depth: int = 0) -> None:
    """Append ordered leaves to ``out``: ("text", items) | ("table"|"rows", rows)."""
    if len(items) == 1 or depth > 120:
        out.append(("text", items))
        return
    h = _median_h(items)
    cols = _split_columns(items, max(2.0, _COLUMN_GAP * h))
    if len(cols) > 1:
        grid = _grid(items, h, channel=True)
        if grid is not None:
            out.append(grid)
            return
        if not _key_value(cols, h):
            for col in cols:
                _xy_cut(col, out, depth + 1)
            return
    split = _split_rows(items, h)
    if split is not None:
        _xy_cut(split[0], out, depth + 1)
        _xy_cut(split[1], out, depth + 1)
        return
    grid = _grid(items, h, channel=False)
    out.append(grid if grid is not None else ("text", items))


# ---------------------------------------------------------------------------
# Leaves -> Markdown blocks
# ---------------------------------------------------------------------------
def _lines(items: list[_Seg]) -> list[_Line]:
    out: list[_Line] = []
    for band in _bands([s for s in items if s.kind == "text"]):
        band.sort(key=lambda s: s.x0)
        text = band[0].text
        for prev, s in zip(band, band[1:]):
            if prev.mono and s.mono:
                cw = max(0.1, (prev.cw + s.cw) / 2)
                text += " " * max(1, round((s.x0 - prev.x1) / cw)) + s.text
            else:
                text += " " + s.text
        words = [w for s in band for w in (s.words or [(s.text, s.x0, s.x1)])]
        x0 = min(s.x0 for s in band)
        text_x0 = x0
        if len(words) >= 2 and _BULLET_START_RE.match(words[0][0] + " "):
            text_x0 = words[1][1]
        n = sum(len(s.text) for s in band)
        mono = sum(len(s.text) for s in band if s.mono) >= 0.6 * n
        out.append(
            _Line(
                text,
                x0,
                min(s.y0 for s in band),
                max(s.x1 for s in band),
                max(s.y1 for s in band),
                median(s.h for s in band),
                sum(s.cw * len(s.text) for s in band) / max(1, n),
                mono,
                text_x0,
            )
        )
    return out


def _continues(prev: _Line, nxt: _Line, left: float, right: float) -> bool:
    """Is ``nxt`` the wrapped continuation of ``prev`` (same paragraph)?"""
    ptext, ntext = prev.text.rstrip(), nxt.text.lstrip()
    if not ptext or not ntext:
        return False
    if not 0.75 <= nxt.h / prev.h <= 1.33:
        return False
    if prev.y0 - nxt.y1 > 0.9 * prev.h:
        return False  # not the next line down: something sits between them
    if _BULLET_START_RE.match(ntext):
        return False
    if ptext.endswith(("\x02", "\xad")):
        return True
    if ptext.endswith("-") and ntext[:1].islower():
        return True
    # TOC-style entries end in a page number and the next starts afresh.
    if ptext[-1].isdigit() and (ntext[:1].isupper() or ntext[:1].isdigit()):
        return False
    em = 0.8 * prev.h
    if nxt.x0 > left + 0.6 * em and nxt.x0 > prev.text_x0 + 0.6 * em:
        return False  # indented: a new paragraph (or a nested item)
    first = ntext.split()[0]
    need = (len(first) + 1) * prev.cw
    forced = need > (right - prev.x1) - prev.cw
    if ptext.endswith(_SENTENCE_END):
        return forced
    # Mid-sentence line end: a wrap unless the line is conspicuously short
    # (a label, a list entry, a title set on its own line).
    return forced or (prev.x1 - left) >= 0.6 * (right - left)


def _join_para(lines: list[_Line], left: float, right: float) -> list[tuple[str, _Line]]:
    """Reflowed paragraphs, each with its own first line (for geometry: a
    title and the subtitle under it share a run, not a type size)."""
    paras: list[tuple[str, _Line]] = []
    cur = lines[0].text.strip()
    first = lines[0]
    for prev, nxt in zip(lines, lines[1:]):
        ntext = nxt.text.strip()
        if _continues(prev, nxt, left, right):
            tail = cur[:-1] + "-" if cur.endswith(("\x02", "\xad")) else cur
            if _URL_TAIL_RE.search(tail) and not ntext[:1].isupper():
                cur = tail + ntext  # a URL wrapped after "/", "." or "-"
            elif cur.endswith(("\x02", "\xad")):
                cur = cur[:-1] + "\x02" + ntext
            elif cur.endswith("-") and ntext[:1].islower():
                cur = cur[:-1] + "\x02" + ntext
            else:
                cur += " " + ntext
        else:
            paras.append((cur, first))
            cur, first = ntext, nxt
    paras.append((cur, first))
    return paras


_URL_TAIL_RE = re.compile(r"(?:https?://|www\.)\S*[/._\-#?=&]$")


def _code_block(lines: list[_Line]) -> str:
    left = min(l.x0 for l in lines)
    cw = median(l.cw for l in lines) or 1.0
    body = []
    for l in lines:
        indent = max(0, round((l.x0 - left) / cw))
        body.append(" " * indent + l.text.rstrip())
    return "```\n" + "\n".join(body) + "\n```"


def _table_block(rows: list[list[str]]) -> str:
    rows = [[c.replace("|", "\\|") for c in r] for r in rows]
    ncols = len(rows[0])
    widths = [max(3, max(len(r[k]) for r in rows)) for k in range(ncols)]

    def fmt(r: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(r, widths)) + " |"

    out = [fmt(rows[0]), "| " + " | ".join("-" * w for w in widths) + " |"]
    out.extend(fmt(r) for r in rows[1:])
    return "\n".join(out)


def _line_runs(lines: list[_Line], fence_mono: bool) -> list[tuple[bool, list[_Line]]]:
    """Split a leaf's lines into (is_code, lines) runs.

    A lone fixed-pitch line that continues a prose sentence is inline code or
    a URL that wrapped onto its own line ("…Active Support Notifi-" /
    "cations (https://…) – the"), not a listing — fencing it would cut the
    sentence in two. A lone line introduced by a colon stays code ("Run:" /
    "$ pip install …"). A run whose every line is a bulleted item is a list
    of code names, not a listing.
    """
    runs: list[list] = []
    for line in lines:
        is_mono = line.mono and fence_mono
        if runs and runs[-1][0] == is_mono:
            runs[-1][1].append(line)
        else:
            runs.append([is_mono, [line]])
    for k, run in enumerate(runs):
        if not run[0]:
            continue
        # A bulleted list of fixed-pitch terms ("• metadata/cmd") is a list
        # of code names, not a listing: it stays a Markdown list.
        if all(_BULLET_START_RE.match(l.text.lstrip()) for l in run[1]):
            run[0] = False
        elif len(run[1]) == 1 and k > 0 and not runs[k - 1][0]:
            before = runs[k - 1][1][-1].text.rstrip()
            if not before.endswith((":", ".", "!", "?")):
                run[0] = False
    merged: list[tuple[bool, list[_Line]]] = []
    for is_mono, run_lines in runs:
        if merged and merged[-1][0] == is_mono:
            merged[-1][1].extend(run_lines)
        else:
            merged.append((is_mono, list(run_lines)))
    return merged


@dataclass
class _Block:
    kind: str  # "para" | "code" | "table" | "fig" | "heading"
    text: str
    top: float = 0.0
    x0: float = 0.0
    h: float = 0.0


def _leaf_blocks(kind: str, payload, fence_mono: bool, stats: LayoutStats) -> list[_Block]:
    if kind in ("table", "rows"):
        rows, caption = payload
        head = [_Block("para", caption)] if caption else []
        if kind == "table":
            stats.tables += 1
            return head + [_Block("table", _table_block(rows))]
        lines = [" ".join(c for c in r if c) for r in rows]
        return head + [_Block("para", line) for line in lines if line]
    items: list[_Seg] = payload
    blocks: list[_Block] = []
    figs = [s for s in items if s.kind == "fig"]
    lines = _lines(items)
    top = max((l.y1 for l in lines), default=0.0)
    for f in figs:
        if f.y1 >= top:
            blocks.append(_Block("fig", f.key, f.y1))
    if lines:
        left = min(l.x0 for l in lines)
        right = max(l.x1 for l in lines)
        for is_code, run in _line_runs(lines, fence_mono):
            if is_code:
                stats.code_blocks += 1
                blocks.append(_Block("code", _code_block(run), run[0].y1, left, run[0].h))
            else:
                for para, first in _join_para(run, left, right):
                    blocks.append(_Block("para", para, first.y1, first.x0, first.h))
    for f in figs:
        if f.y1 < top:
            blocks.append(_Block("fig", f.key, f.y1))
    return blocks


# ---------------------------------------------------------------------------
# Page furniture: running headers, footers and folios
# ---------------------------------------------------------------------------
# Only text in the outermost two baselines at the top or bottom of the page,
# inside this share of the page height, is ever considered.
_MARGIN_ZONE = 0.12
_FURNITURE_MIN_PAGES = 3
_FOLIO_RE = re.compile(
    r"^[\[(\-–—|\s]*(?:page\s+)?(\d{1,4}|[ivxlcdm]{1,7})[\])\-–—|\s]*$", re.IGNORECASE
)
_EDGE_NUMBER_RE = re.compile(r"^(\d{1,4})\b|\b(\d{1,4})$")


def _furniture_key(text: str) -> str:
    return " ".join(re.sub(r"\d+", "#", text.lower()).split())


def _edge_candidates(segs: list[_Seg], clip, quad) -> list[tuple[_Seg, bool]]:
    """Short segments in the outermost two baselines at the top and bottom of
    the page, within the margin zone, as ``(segment, is_outermost_line)``."""
    texts = [s for s in segs if s.kind == "text"]
    if not texts:
        return []
    _x0, cy0, _x1, cy1 = _rotate_box(*clip, quad)
    zone = _MARGIN_ZONE * (cy1 - cy0)
    # Running heads are set small; a chapter opener's title printed in the
    # same words is not furniture.
    max_h = 1.3 * median(s.h for s in texts)
    bands = _bands(texts)
    last = len(bands) - 1
    # Top and bottom are chosen independently, each by its own zone — a page
    # with only a body line and a folio has just two bands.
    top = [k for k in (0, 1) if k <= last and min(s.y0 for s in bands[k]) >= cy1 - zone]
    bottom = [
        k for k in (last - 1, last)
        if k >= 0 and k not in top and max(s.y1 for s in bands[k]) <= cy0 + zone
    ]
    picked: list[tuple[_Seg, bool]] = []
    for k in top + bottom:
        outer = k == 0 or k == last
        picked.extend(
            (s, outer) for s in bands[k] if len(s.text.split()) <= 12 and s.h <= max_h
        )
    return picked


def _roman_value(text: str) -> int | None:
    """Value of a strictly-formed roman numeral, else None."""
    t = text.lower()
    if not t or not re.fullmatch(r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})", t):
        return None
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = 0
    for a, b in zip(t, t[1:] + " "):
        v = vals[a]
        total += -v if b != " " and vals[b] > v else v
    return total or None


def _learn_offset(pairs: list[int], pages: int) -> int | None:
    """The folio offset (physical page minus printed number) most pages agree on."""
    if not pairs:
        return None
    off, n = Counter(pairs).most_common(1)[0]
    return off if n >= max(2, 0.2 * pages) else None


def _strip_furniture(pages: list[tuple[int, list[_Seg], tuple, int]]) -> int:
    """Drop running headers/footers and page numbers from every page.

    ``pages`` are ``(physical_page_index, segments, clip, quad)``. A
    margin-zone line is furniture when:

    - its text (digits masked) recurs on at least three pages and has real
      letters ("MAKING AND BREAKING THE GRID", "# Universal Principles…");
    - it is a bare page number AND matches the folio offset learned from all
      bare numbers in the outermost lines (arabic and roman separately), so a
      list number "2." or a stray table value is never mistaken for one;
    - it is the outermost line, a few words long, and carries the page's own
      folio number ("Alignment 25").

    Returns the number of segments removed.
    """
    cands = [(pno, _edge_candidates(segs, clip, quad)) for pno, segs, clip, quad in pages]
    counts: Counter[str] = Counter()
    for _pno, page_cands in cands:
        for key in {_furniture_key(s.text) for s, _outer in page_cands}:
            counts[key] += 1
    arabic: list[int] = []
    roman: list[int] = []
    for pno, page_cands in cands:
        for s, outer in page_cands:
            m = _FOLIO_RE.match(s.text)
            if not (m and outer):
                continue
            if m.group(1).isdigit():
                arabic.append(pno + 1 - int(m.group(1)))
            elif (v := _roman_value(m.group(1))) is not None:
                roman.append(pno + 1 - v)
    total = max((pno for pno, *_rest in pages), default=0) + 1
    arabic_off = _learn_offset(arabic, total)
    roman_off = _learn_offset(roman, 1)
    removed = 0
    for (pno, segs, _clip, _quad), (_p, page_cands) in zip(pages, cands):
        drop: set[int] = set()
        for s, outer in page_cands:
            key = _furniture_key(s.text)
            m = _FOLIO_RE.match(s.text)
            if counts[key] >= _FURNITURE_MIN_PAGES and len(re.findall(r"[A-Za-z]", key)) >= 2:
                drop.add(id(s))
            elif m:
                token = m.group(1)
                if token.isdigit():
                    if arabic_off is not None and int(token) == pno + 1 - arabic_off:
                        drop.add(id(s))
                elif roman_off is not None and _roman_value(token) == pno + 1 - roman_off:
                    drop.add(id(s))
            elif outer and arabic_off is not None and len(s.text.split()) <= 5:
                n = _EDGE_NUMBER_RE.search(s.text.strip())
                if n and int(n.group(1) or n.group(2)) == pno + 1 - arabic_off:
                    drop.add(id(s))
        if drop:
            removed += len(drop)
            segs[:] = [s for s in segs if id(s) not in drop]
    return removed


# ---------------------------------------------------------------------------
# Outline (bookmarks)
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", unicodedata.normalize("NFKD", text).lower())


def _repair_title(title: str) -> str:
    """Tidy a bookmark title; U+FFFD marks a character the PDF's title string
    lost — almost always a curly apostrophe or an em dash."""
    title = " ".join(title.split())
    title = re.sub(r"(?<=[A-Za-z])�(?=(?:s|t|d|m|ll|re|ve)\b)", "’", title, flags=re.I)
    title = re.sub(r"(?<=\w)�(?=\w)", "—", title)
    return title.replace("�", "").strip()


def _is_front_matter(title: str) -> bool:
    n = _norm(title)
    return n in _FRONT_MATTER_TITLES or n.startswith("contents") or n.startswith("tableofcontents")


def read_outline(pdf) -> list[OutlineEntry]:
    """The document's bookmarks, minus duplicate "by category" subtrees."""
    raw: list[OutlineEntry] = []
    try:
        for bm in pdf.get_toc():
            dest = bm.get_dest()
            page = dest.get_index() if dest is not None else None
            title = _repair_title(bm.get_title() or "")
            if page is None or not title:
                continue
            raw.append(OutlineEntry(bm.level, title, page))
    except Exception:
        return []
    # An entry repeating an earlier (title, page) is a second index into the
    # same content (e.g. "Contents by Category"); a container whose entries
    # are ALL such repeats is dropped with them.
    seen: set[tuple[str, int]] = set()
    dup = [False] * len(raw)
    for i, e in enumerate(raw):
        key = (_norm(e.title), e.page)
        dup[i] = key in seen
        seen.add(key)
    keep: list[OutlineEntry] = []
    for i, e in enumerate(raw):
        if dup[i]:
            continue
        j = i + 1
        children = []
        while j < len(raw) and raw[j].level > e.level:
            children.append(j)
            j += 1
        if children and all(dup[c] for c in children):
            continue
        keep.append(e)
    return keep


def _chapter_level(outline: list[OutlineEntry]) -> int:
    """The shallowest level with at least two real (non-front-matter) entries
    — that level becomes ``#``."""
    levels = sorted({e.level for e in outline})
    for lv in levels:
        real = [e for e in outline if e.level == lv and not _is_front_matter(e.title)]
        if len(real) >= 2:
            return lv
    return levels[0] if levels else 0


def _block_matches(block_text: str, title_norm: str) -> bool:
    b = _norm(block_text)
    if len(b) < 2 or not title_norm:
        return False
    if b == title_norm:
        return True
    if b in title_norm and (len(b) >= 0.25 * len(title_norm) or len(b) >= 12):
        return True
    return title_norm in b and len(b) <= 1.3 * len(title_norm) + 6


def _place_headings(blocks: list[_Block], entries: list[tuple[int, str]], stats: LayoutStats) -> list[_Block]:
    """Insert ``(depth, title)`` headings into a page's ordered blocks,
    replacing the block(s) that print the title when they can be found."""
    out = list(blocks)
    cursor = 0
    for depth, title in entries:
        tn = _norm(title)
        heading = _Block("heading", "#" * depth + " " + title)
        hit = None
        for j in range(cursor, len(out)):
            if out[j].kind in ("para",) and _block_matches(out[j].text, tn):
                hit = j
                break
        stats.headings += 1
        if hit is None:
            out.insert(cursor, heading)
            cursor += 1
            continue
        out[hit] = heading
        # Swallow the rest of a title set in several pieces ("Chapter 1" /
        # "What Makes a Logo Last?").
        k = hit + 1
        while k < len(out) and k <= hit + 3 and out[k].kind == "para":
            b = _norm(out[k].text)
            if len(b) >= 2 and b in tn:
                del out[k]
            else:
                break
        cursor = hit + 1
    return out


# ---------------------------------------------------------------------------
# Figures (positions only — the files are written at export time)
# ---------------------------------------------------------------------------
def _figure_segs(page, clip, quad, page_no: int) -> list[_Seg]:
    from . import figures

    segs = []
    for k, bbox in figures.page_image_boxes(page, clip):
        x0, y0, x1, y1 = _rotate_box(*bbox, quad)
        segs.append(
            _Seg("", x0, y0, x1, y1, 0.0, 0.0, False, -1, kind="fig", key=f"{page_no}.{k}")
        )
    return segs


def _overlaps_text(fig: _Seg, texts: list[_Seg]) -> bool:
    area = max(1.0, (fig.x1 - fig.x0) * (fig.y1 - fig.y0))
    covered = 0.0
    for s in texts:
        w = min(fig.x1, s.x1) - max(fig.x0, s.x0)
        h = min(fig.y1, s.y1) - max(fig.y0, s.y0)
        if w > 0 and h > 0:
            covered += w * h
            if covered > 0.02 * area or w * h > 0.5 * (s.x1 - s.x0) * (s.y1 - s.y0):
                return True
    return False


# ---------------------------------------------------------------------------
# Document-level text repair
# ---------------------------------------------------------------------------
# Tried in order; "th" last — display faces set a "Th" ligature ("\x1featre").
_LIGATURES = ("ff", "fi", "fl", "ffi", "ffl", "ft", "st", "ct", "tt", "th")
# Common ligature-bearing words: a book whose font breaks EVERY "ff" never
# spells "different" correctly anywhere, so its own vocabulary can't vote.
_SEED_WORDS = frozenset(
    """different difference differences differently effect effects effective
    effectively efficient efficiently efficiency effort efforts offer offers
    offered office offices official officially afford affect affected affects
    staff stuff traffic suffer suffix offset offline off difficult difficulty
    sufficient sufficiently coffee buffer buffers diffuse jeff cliff chef
    first find finds finding findings field fields file files final finally
    finish finished fine figure figures fit fits fifth fifty fiction fixed fix
    define defined definition definitions specific specifically significant
    significance profile profiles benefit benefits confident configure
    configured configuration identify identified classification notification
    certificate artificial scientific unified magnifying fiber filter filters
    flow flows flower flowers floor fly flat flag flexible flexibility reflect
    reflects reflection influence influenced conflict conflicts flight float
    fluid fluent flaw flawed flavor flourish flush inflate workflow workflows
    after often left soft craft draft shift gift swift lift fifteen""".split()
)
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
_CTRL_CLASS = "\x00\x01\x03-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
_CTRL_WORD_RE = re.compile(rf"[^\W\d_]*[{_CTRL_CLASS}][^\W\d_{_CTRL_CLASS}]*(?:[{_CTRL_CLASS}][^\W\d_]*)*")
_CTRL_RE = re.compile(rf"[{_CTRL_CLASS}]")
# Printable stand-ins some font subsets use for a ligature (see
# _repair_ligatures): NBSP, soft hyphen, pdfium's line-end hyphen marker.
_AMBIG_WORD_RE = re.compile(r"(?<![^\W\d_])([^\W\d_]+)([\xa0\xad\x02])([^\W\d_]+)")
# The \x02 marker is always glued to its continuation; no whitespace may be
# matched after it, or a dangling break would swallow a paragraph gap.
_HYPHEN_BREAK_RE = re.compile(r"([^\W\d_]*)\x02([^\W\d_]*)")
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


_LIGATURE_STEM_MIN = 6  # shortest known word that may vouch as a stem


def _apply_ligature(word: str, code: str, lig: str) -> str:
    # The "Th" ligature is a capital's: at a word start it reads "Th".
    if lig == "th" and word.startswith(code):
        word = "Th" + word[len(code):]
    return word.replace(code, lig)


def _vocab_ligatures(word: str, code: str, vocab: set[str]) -> tuple[list[str], str | None]:
    """(exact, stem) for ``word`` with ``code`` standing for a ligature:
    ``exact`` lists the ligatures that make it a known word; failing any,
    ``stem`` is the one that makes it contain the longest known word
    spanning the ligature ("undi\\x96erentiated" holds "different")."""
    exact = [lig for lig in _LIGATURES if word.replace(code, lig).lower() in vocab]
    if exact:
        return exact, None
    p = word.index(code)
    best, best_len = None, 0
    for lig in _LIGATURES:
        cand = word.replace(code, lig).lower()
        end = p + len(lig)
        # Substrings holding the whole ligature and a letter either side.
        for i in range(p):
            for j in range(len(cand), end, -1):
                n = j - i
                if n <= best_len or n < _LIGATURE_STEM_MIN:
                    break
                if cand[i:j] in vocab:
                    best, best_len = lig, n
                    break
    return [], best


def _repair_ligatures(text: str, stats: LayoutStats) -> str:
    """Restore ligatures the font failed to map to Unicode.

    Such glyphs surface as control characters (or pdfium's "no mapping",
    kept as a placeholder). A word resolves to the ligature that turns it
    into a word the document spells correctly elsewhere (or a common
    ligature word), or that makes it contain one ("undi\\x96erentiated"
    holds "different"); failing that, the ligature that the same control
    code stands for in other words — a code is consistent within one font
    ("di\\x81erent" -> "different"), though every font subset of a book may
    pick its own. Unresolvable codes are dropped.

    Some subsets even reuse a printable character (NBSP, the soft hyphen,
    pdfium's line-end hyphen marker): between two letters those are only
    candidates, repaired when a ligature makes a known word and otherwise
    left in their usual role.
    """
    has_ctrl = _CTRL_RE.search(text) is not None
    if not has_ctrl and not _AMBIG_WORD_RE.search(text):
        return text.replace("\xa0", " ")
    words = set(_CTRL_WORD_RE.findall(text))
    # Words carrying a code are left out: their fragments ("di", "erent")
    # are not words and must not vouch for anything.
    vocab_text = _AMBIG_WORD_RE.sub(" ", _CTRL_WORD_RE.sub(" ", text))
    vocab = {w.lower() for w in _WORD_RE.findall(vocab_text)}
    vocab |= _SEED_WORDS
    resolved: dict[str, str] = {}
    votes: dict[str, Counter[str]] = {}  # from unambiguous whole words
    stem_votes: dict[str, Counter[str]] = {}  # weaker: from stems
    mapping: dict[str, str] = {}
    pending = {w for w in words if len(set(_CTRL_RE.findall(w))) == 1}
    # Two rounds: a word repaired in the first (a surname, via its font's
    # well-attested code) joins the vocabulary, so the same word under
    # another font's code resolves in the second.
    for _round in range(2):
        ambiguous: dict[str, list[str]] = {}
        stems: dict[str, str] = {}
        for word in pending:
            code = _CTRL_RE.search(word).group(0)  # type: ignore[union-attr]
            exact, stem = _vocab_ligatures(word, code, vocab)
            if len(exact) == 1:
                resolved[word] = _apply_ligature(word, code, exact[0])
                votes.setdefault(code, Counter())[exact[0]] += 1
            elif exact:
                ambiguous[word] = exact  # "o?er": offer or other
            elif stem is not None:
                stems[word] = stem
                stem_votes.setdefault(code, Counter())[stem] += 1
        mapping = {code: c.most_common(1)[0][0] for code, c in stem_votes.items()}
        mapping.update({code: c.most_common(1)[0][0] for code, c in votes.items()})
        pending -= resolved.keys()
        for word in pending:
            code = _CTRL_RE.search(word).group(0)  # type: ignore[union-attr]
            lig = mapping.get(code)
            if word in ambiguous:
                # The code's other words decide between real words.
                lig = lig if lig in ambiguous[word] else ambiguous[word][0]
            elif word in stems and code not in votes:
                lig = stems[word]  # a whole-word vote outranks a stem guess
            if lig is not None:
                resolved[word] = _apply_ligature(word, code, lig)
        pending -= resolved.keys()
        if not pending:
            break
        vocab |= {w.lower() for w in resolved.values()}

    def fix_word(m: re.Match[str]) -> str:
        word = m.group(0)
        fixed = resolved.get(word)
        if fixed is None:
            fixed = _CTRL_RE.sub(lambda c: mapping.get(c.group(0), ""), word)
        if fixed != _CTRL_RE.sub("", word):
            stats.ligatures_repaired += 1
        return fixed

    if has_ctrl:
        text = _CTRL_RE.sub("", _CTRL_WORD_RE.sub(fix_word, text))
        vocab |= {w.lower() for w in resolved.values()}

    def fix_candidate(m: re.Match[str]) -> str:
        a, code, b = m.groups()
        if code != "\xa0" and (a + b).lower() in vocab:
            return m.group(0)  # a real hyphen break: "devi\x02ation"
        # Whole words only: these characters have a real role, and a stem
        # can span a genuine break ("Extr[afford]inary" from "Extra-ordinary").
        exact, _stem = _vocab_ligatures(m.group(0), code, vocab)
        if not exact:
            return m.group(0)
        stats.ligatures_repaired += 1
        return a + exact[0] + b

    text = _AMBIG_WORD_RE.sub(fix_candidate, text)
    return text.replace("\xa0", " ")


def _resolve_hyphens(text: str) -> str:
    """Resolve line-end hyphen breaks (marked \\x02): join the word unless the
    document itself spells it hyphenated elsewhere."""
    if "\x02" not in text:
        return text
    body = _HYPHEN_BREAK_RE.sub(" ", text)
    vocab = {w.lower() for w in _WORD_RE.findall(body)}
    compounds = {
        w.lower() for w in re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)+", body)
    }

    def sub(m: re.Match[str]) -> str:
        a, b = m.group(1), m.group(2)
        if not b:
            return a + "-"
        if not a:
            # "24-" / "hour": a number before the break is a compound, not a
            # split word ("24-hour", "3-D") — keep its hyphen.
            prev = m.string[m.start() - 1 : m.start()]
            return f"-{b}" if prev.isdigit() else b
        if b[0].isupper():
            return f"{a}-{b}"
        joined = (a + b).lower()
        if f"{a}-{b}".lower() in compounds and joined not in vocab:
            return f"{a}-{b}"
        return a + b

    return _HYPHEN_BREAK_RE.sub(sub, text)


# ---------------------------------------------------------------------------
# Document metadata
# ---------------------------------------------------------------------------
_FILENAME_TITLE_RE = re.compile(r"\.(?:pdf|indd|docx?|qxd|qxp|ai|psd|txt)\s*$|\d{7,}", re.I)


def _clean_inline(text: str) -> str:
    """A one-line string (title, metadata) with pdfium's markers repaired and
    anything unencodable or YAML-hostile (control chars, lone surrogates)
    removed."""
    text = _LONE_SURROGATE_RE.sub("", text)
    text = _resolve_hyphens(_repair_ligatures(text, LayoutStats()))
    return " ".join(_CTRL_RE.sub("", text).replace("\x02", "-").split())


def _sane_meta(value: str | None) -> str:
    value = _clean_inline(value or "")
    if (
        len(value) < 3
        or len(value) > 200
        or _FILENAME_TITLE_RE.search(value)
        or not re.search(r"[A-Za-z]{3}", value)
        or value.lower().startswith(("untitled", "microsoft word", "document"))
    ):
        return ""
    return value


def _title_from_page(blocks: list[_Block]) -> str:
    paras = [b for b in blocks if b.kind == "para" and 3 <= len(b.text) <= 120]
    if not paras:
        return ""
    tallest = max(b.h for b in paras)
    picked = [k for k, b in enumerate(paras) if b.h >= 0.8 * tallest][:3]
    parts = [paras[k].text for k in picked]
    # "Design Elements:" set large over a smaller "Color Fundamentals" —
    # a title ending in a colon continues in the next block.
    if parts[-1].rstrip().endswith(":") and picked[-1] + 1 < len(paras):
        parts.append(paras[picked[-1] + 1].text)
    title = " ".join(" ".join(parts).split())
    # Small-caps fonts extract as "DesiGn"; fall back to title case then.
    if re.search(r"[a-z][A-Z]", title):
        title = title.title()
    return title if 3 <= len(title) <= 150 else ""


# Library of Congress CIP data on a copyright page: "Title: Made by James :
# the honest guide to creativity and logo design / James Martin.
# Description: …". The next field name ends the author statement, since
# names carry their own periods ("Lyle H. Sandler").
_CIP_CONTEXT_RE = re.compile(r"Library of Congress|\bNames:|\bDescription:|\bIdentifiers:")
_CIP_RE = re.compile(
    r"\bTitle:\s*(?P<title>[^/]{3,250}?)\s*/\s*(?P<author>[^/]{3,150}?)\."
    r"(?=\s+(?:Description|Identifiers|Subjects|Series|Other titles|Includes|Summary|Edition)\b|\s*$)"
)
# "Copyright © 2020 Packt Publishing" — publishers set the title (and an
# edition line) directly above the notice.
_COPYRIGHT_RE = re.compile(r"^Copyright\s*(?:©|\(c\))", re.IGNORECASE)
_EDITION_RE = re.compile(
    r"^(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|\d{1,2}(?:st|nd|rd|th))\s+Edition$",
    re.IGNORECASE,
)
_FRONT_MATTER_PAGES = 12  # the copyright page is searched for in these
_SMALL_WORDS = frozenset(
    "a an and as at but by for from in into nor of on or the to vs via with".split()
)


def _title_case(title: str) -> str:
    """CIP titles are in sentence case ("Universal principles of branding");
    capitalise them as a title, keeping small words and existing capitals."""
    words = title.split()
    return " ".join(
        w if (k and w.lower() in _SMALL_WORDS) or not w[:1].islower() else w[:1].upper() + w[1:]
        for k, w in enumerate(words)
    )


_TITLE_PAGE_SCAN = 6  # an unbookmarked title page sits among these (after cover art)
# ("Foreword by …" is a title-page credit; a real foreword is too long.)
_NOT_TITLE_PAGE_RE = re.compile(r"\b(?:contents|copyright|dedicat\w*|isbn)\b|©", re.I)


def _unmarked_title_page(pages: list[list[_Block]]) -> list[_Block] | None:
    """The first page with any text, if it reads as a title page: a few
    short blocks (title, subtitle, author), nothing else. Only that page is
    considered — a later sparse page is as likely an epigraph."""
    for blocks in pages[:_TITLE_PAGE_SCAN]:
        paras = [b for b in blocks if b.kind == "para" and b.text.strip()]
        if not paras:
            continue  # the cover: an image
        text = " ".join(b.text for b in paras)
        if len(paras) <= 6 and len(text) <= 300 and not _NOT_TITLE_PAGE_RE.search(text):
            return blocks
        return None
    return None


def _front_matter_meta(pages: list[list[_Block]]) -> tuple[str, str]:
    """(title, author) from a copyright page, "" when absent: its CIP data,
    else the title lines set above a "Copyright ©" notice."""
    for blocks in pages:
        paras = [b.text for b in blocks if b.kind == "para"]
        page_text = " ".join(paras)
        if _CIP_CONTEXT_RE.search(page_text):
            m = _CIP_RE.search(page_text)
            if m:
                title = _title_case(m.group("title").split(" : ")[0])
                return title, m.group("author").split(";")[0]
        for k, text in enumerate(paras[:3]):
            if not (k and _COPYRIGHT_RE.match(text)):
                continue
            head = [p.strip() for p in paras[:k]]
            if len(head) >= 2 and _EDITION_RE.match(head[-1]):
                return f"{head[-2]}, {head[-1]}", ""
            if len(head) == 1 and len(head[0]) <= 120 and not head[0].endswith("."):
                return head[0], ""
    return "", ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def extract_markdown(
    path: str | Path,
    *,
    anchors: bool = False,
    figure_markers: bool = False,
    ocr_pages: set[int] | frozenset[int] = frozenset(),
    ocr_page: Callable[[object], str] | None = None,
    on_page: Callable[[int, int], None] | None = None,
) -> LayoutResult:
    """Convert a PDF to Markdown with column-aware reading order.

    ``ocr_pages`` (0-based) are rendered through ``ocr_page(page) -> text``
    instead of reading their text layer, so mixed scanned/digital PDFs keep
    one pipeline (outline headings, anchors, figure markers). ``anchors``
    emits ``<!-- page N -->`` comments; ``figure_markers`` emits
    ``<!-- figure N.K -->`` where each qualifying image sits in the reading
    order, for :func:`figures.insert_figure_links` to replace at export.
    """
    stats = LayoutStats()
    pdf = pdfium.PdfDocument(str(path))
    try:
        total = len(pdf)
        stats.pages = total
        outline = read_outline(pdf)
        chapter_level = _chapter_level(outline)
        by_page: dict[int, list[tuple[int, str]]] = {}
        title_pages: set[int] = set()
        for e in outline:
            if _is_front_matter(e.title):
                if _norm(e.title) in ("title", "titlepage", "halftitle"):
                    title_pages.add(e.page)
                continue
            depth = min(_MAX_HEADING_LEVEL, 1 + max(0, e.level - chapter_level))
            by_page.setdefault(e.page, []).append((depth, e.title))

        # Phase 1: every page's segments (or OCR text).
        raw_pages: list[tuple[list[_Seg], tuple, int] | str] = []
        for i in range(total):
            page = pdf[i]
            try:
                if i in ocr_pages and ocr_page is not None:
                    raw_pages.append(ocr_page(page).strip())
                else:
                    textpage = page.get_textpage()
                    try:
                        chars, quads = _read_chars(textpage)
                    finally:
                        textpage.close()
                    quad = quads.most_common(1)[0][0] if quads else 0
                    clip = clip_box(page)
                    segs = _segments(chars, quad, clip, stats)
                    if figure_markers:
                        segs.extend(_figure_segs(page, clip, quad, i + 1))
                    raw_pages.append((segs, clip, quad))
            finally:
                page.close()
            if on_page is not None:
                on_page(i + 1, total)

        # Phase 2: running headers/footers are judged across the document.
        text_pages = [p for p in raw_pages if not isinstance(p, str)]
        # Physical page numbers: OCR'd pages are absent here but still count.
        _strip_furniture(
            [(i, *p) for i, p in enumerate(raw_pages) if not isinstance(p, str)]
        )
        mono_chars = all_chars = 0
        for segs, _clip, _quad in text_pages:
            for s in segs:
                if s.kind == "text":
                    all_chars += len(s.text)
                    if s.mono:
                        mono_chars += len(s.text)
        fence_mono = all_chars == 0 or mono_chars < _MONO_BODY_SHARE * all_chars

        # Phase 3: reading order per page.
        page_leaves: list[list] = []
        for raw_page in raw_pages:
            if isinstance(raw_page, str):
                page_leaves.append([("ocr", raw_page)])
                continue
            segs = raw_page[0]
            texts = [s for s in segs if s.kind == "text"]
            leaves: list = []
            fg: list[_Seg] = []
            for s in segs:
                if s.kind == "fig" and _overlaps_text(s, texts):
                    leaves.append(("fig", s.key))  # background / wrapped: page start
                else:
                    fg.append(s)
            if fg:
                _xy_cut(fg, leaves)
            page_leaves.append(leaves)

        pages: list[list[_Block]] = []
        for i, leaves in enumerate(page_leaves):
            blocks: list[_Block] = []
            for kind, payload in leaves:
                if kind == "ocr":
                    if payload:
                        blocks.append(_Block("para", payload))
                elif kind == "fig":
                    blocks.append(_Block("fig", payload))
                else:
                    blocks.extend(_leaf_blocks(kind, payload, fence_mono, stats))
            if i in by_page:
                blocks = _place_headings(blocks, by_page[i], stats)
            pages.append(blocks)

        meta = {}
        try:
            meta = pdf.get_metadata_dict()
        except Exception:
            pass
        title = _sane_meta(meta.get("Title"))
        if not title:
            for p in sorted(title_pages):
                if p < len(pages):
                    # Page text is still raw here (hyphen / ligature markers):
                    # give the title the same repairs the body gets below.
                    title = _clean_inline(_title_from_page(pages[p]))
                    if title:
                        break
        author = _sane_meta(meta.get("Author"))
        if not (title and author):
            # Many print PDFs carry only an ISBN filename as metadata; the
            # copyright page still names the book (and often its author).
            fm_title, fm_author = _front_matter_meta(pages[:_FRONT_MATTER_PAGES])
            title = title or _sane_meta(fm_title)
            author = author or _sane_meta(fm_author)
        if not title and (page := _unmarked_title_page(pages)) is not None:
            title = _sane_meta(_title_from_page(page))
    finally:
        pdf.close()

    pages = [_merge_continuations(blocks) for blocks in pages]
    if not anchors:
        _merge_across_pages(pages)
    parts: list[str] = []
    for i, blocks in enumerate(pages):
        if anchors:
            tag = " (ocr)" if i in ocr_pages and ocr_page is not None else ""
            if ocr_pages and ocr_page is not None and not tag:
                tag = " (text)"
            parts.append(f"<!-- page {i + 1}{tag} -->")
        for b in blocks:
            if b.kind == "fig":
                parts.append(f"<!-- figure {b.text} -->")
            elif b.text.strip():
                parts.append(b.text)
    text = "\n\n".join(parts)
    text = _LONE_SURROGATE_RE.sub("", text)  # an unpaired half can't be encoded
    text = _repair_ligatures(text, stats)
    text = _resolve_hyphens(text)
    return LayoutResult(text.strip() + "\n", title=title, author=author, stats=stats)


# A paragraph cut off by a column or page break: the first block ends
# mid-sentence (or mid-word) and the next begins lowercase — sentences don't.
_CONTINUED_MIN_CHARS = 30
_OPEN_END = (".", "!", "?", ":", ";", '"', "”", "’", ")", "…", "]")


# A paragraph ending on one of these words stops mid-sentence, so a
# capitalised block that FINISHES the sentence continues it ("…currently at
# the" / "University of Vermont."). Particles that can end a phrase ("log
# in", "stand by") are deliberately absent. Figure labels and captions often
# sit between the halves instead ("…taper to an" / "Negative Feedback
# Loop"), hence the sentence-end and caption tests on the next block.
_DANGLING_WORD_RE = re.compile(
    r"(?:^|\s)(?:the|a|an|of|and|or|nor|to|for|with|from|than|that|its|their|his|her|our|your)$"
)
_CAPTION_LEAD_RE = re.compile(
    r"^(?:Above|Below|Opposite|Left|Right|Top|Bottom|Facing|Figure|Fig\.|Table|Plate|Source)\b"
)
_DANGLING_MIN_CHARS = 50  # shorter is a heading or a label ("The simplest use of yield from")


def _joined(prev: str, nxt: str) -> str | None:
    """``prev`` + ``nxt`` as one paragraph if ``nxt`` continues ``prev``."""
    if prev.startswith(("#", "|", "```", "<!--")) or not nxt[:1].isalpha():
        return None
    if not nxt[:1].islower():
        if (
            len(prev) >= _DANGLING_MIN_CHARS
            and _DANGLING_WORD_RE.search(prev)
            and nxt.rstrip().endswith((".", "!", "?", "…", "”", '"', "’"))
            and not _CAPTION_LEAD_RE.match(nxt)
            and not _BULLET_START_RE.match(nxt)
        ):
            return prev + " " + nxt
        return None
    if prev.endswith(("\x02", "\xad")):
        return prev[:-1] + "\x02" + nxt
    if prev.endswith("-") and not prev.endswith("--"):
        return prev[:-1] + "\x02" + nxt
    if len(prev) >= _CONTINUED_MIN_CHARS and not prev.endswith(_OPEN_END) and not _BULLET_START_RE.match(nxt):
        return prev + " " + nxt
    return None


def _merge_continuations(blocks: list[_Block]) -> list[_Block]:
    """Rejoin paragraphs split across column breaks within one page. Figure
    markers between the halves move after the rejoined paragraph."""
    out: list[_Block] = []
    for b in blocks:
        if b.kind == "para":
            k = len(out) - 1
            while k >= 0 and out[k].kind == "fig":
                k -= 1
            if k >= 0 and out[k].kind == "para":
                merged = _joined(out[k].text, b.text)
                if merged is not None:
                    out[k] = _Block("para", merged, out[k].top, out[k].x0, out[k].h)
                    continue
        out.append(b)
    return out


def _merge_across_pages(pages: list[list[_Block]]) -> None:
    """Rejoin a paragraph that runs from the foot of one page onto the next
    (in place). Only used without page anchors, which must stay between."""
    for i in range(1, len(pages)):
        prev_page, page = pages[i - 1], pages[i]
        tail = next((b for b in reversed(prev_page) if b.kind != "fig"), None)
        head_k = next((k for k, b in enumerate(page) if b.kind != "fig"), None)
        if tail is None or head_k is None or tail.kind != "para" or page[head_k].kind != "para":
            continue
        merged = _joined(tail.text, page[head_k].text)
        if merged is not None:
            tail.text = merged
            del page[head_k]
