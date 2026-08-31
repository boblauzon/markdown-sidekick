"""Post-conversion cleanup for markitdown output.

markitdown extracts text faithfully but carries over PDF artifacts: running
headers / page numbers leak mid-flow, plain-text TOC blocks survive with their
dot leaders, code listings arrive as un-fenced plain text, ligatures stay
un-decomposed, and prose keeps the PDF's hard line wraps. These passes are
intentionally *conservative* — it is far better to leave a little noise than to
delete real content, so every heuristic favours false negatives over false
positives.

The entry point is :func:`clean_markdown`. Each pass is independent and can be
toggled, so the UI can expose them individually later if desired.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, fields

# A "running header/footer" is a short line that recurs across many pages.
_HEADER_MIN_REPEATS = 4
_HEADER_MAX_LEN = 70

# Bare page number on its own line, e.g. "  118  ".
_BARE_PAGE_RE = re.compile(r"^\s*\d{1,4}\s*$")
# "118 Indexing with LlamaIndex"  /  "Indexing with LlamaIndex 118".
# Front matter uses roman page numbers ("xviii Preface"), so accept both.
_PAGE_NUM = r"(?:\d{1,4}|[ivxlcdm]{2,8})"
_FOOTER_LEAD_NUM_RE = re.compile(r"^\s*(%s)\s+(.{1,%d}?)\s*$" % (_PAGE_NUM, _HEADER_MAX_LEN))
_FOOTER_TRAIL_NUM_RE = re.compile(r"^\s*(.{1,%d}?)\s+(%s)\s*$" % (_HEADER_MAX_LEN, _PAGE_NUM))


@dataclass
class CleanupStats:
    """What the cleanup pass changed — surfaced to the user as feedback."""

    removed_noise_lines: int = 0
    toc_lines_removed: int = 0
    code_blocks_fenced: int = 0
    blank_runs_collapsed: int = 0
    chars_normalized: int = 0
    headings_promoted: int = 0
    boilerplate_lines_removed: int = 0
    bullets_normalized: int = 0
    lines_joined: int = 0
    fences_merged: int = 0

    # Every field is a non-negative int counter, so the field list lives once:
    # a future pass's new counter is picked up here (and by `changed`) for free.
    @property
    def total_fixes(self) -> int:
        return sum(getattr(self, f.name) for f in fields(self))

    @property
    def changed(self) -> bool:
        return self.total_fixes > 0

    def brief(self) -> str:
        """One scannable fragment for the preview's info line. Character normalizations
        (ligatures, soft hyphens — routinely thousands per book) are counted
        apart from line/block fixes so they can't inflate the fix count."""
        structural = self.total_fixes - self.chars_normalized
        bits = []
        if structural:
            bits.append(f"{structural:,} fixes")
        if self.chars_normalized:
            bits.append(f"{self.chars_normalized:,} chars normalized")
        if not bits:
            return "No cleanup changes"
        return f"Cleaned ({', '.join(bits)})"

    def summary(self) -> str:
        if not self.changed:
            return "No cleanup changes."
        parts = []
        if self.removed_noise_lines:
            parts.append(f"{self.removed_noise_lines} header/page-number line(s) removed")
        if self.toc_lines_removed:
            parts.append(f"{self.toc_lines_removed} TOC/index line(s) removed")
        if self.boilerplate_lines_removed:
            parts.append(f"{self.boilerplate_lines_removed} boilerplate line(s) removed")
        if self.headings_promoted:
            parts.append(f"{self.headings_promoted} heading(s) promoted")
        if self.code_blocks_fenced:
            parts.append(f"{self.code_blocks_fenced} code block(s) fenced")
        if self.fences_merged:
            parts.append(f"{self.fences_merged} split fence(s) merged")
        if self.bullets_normalized:
            parts.append(f"{self.bullets_normalized} bullet(s) normalized")
        if self.lines_joined:
            parts.append(f"{self.lines_joined} wrapped line(s) joined")
        if self.chars_normalized:
            parts.append(f"{self.chars_normalized} character(s) normalized")
        if self.blank_runs_collapsed:
            parts.append(f"{self.blank_runs_collapsed} blank run(s) collapsed")
        return "Cleanup: " + ", ".join(parts) + "."


def _fence_line(line: str) -> bool:
    return line.lstrip().startswith("```")


# ---------------------------------------------------------------------------
# Pass 0 — character normalization
# ---------------------------------------------------------------------------
# PDF fonts embed typographic ligatures as single codepoints; they render fine
# but break search ("deﬁnition" doesn't match "definition"). Soft hyphens and
# no-break spaces are likewise invisible landmines for downstream tooling.
_CHAR_MAP = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "ft",
    "ﬆ": "st",
    "­": "",  # soft hyphen
    " ": " ",  # no-break space
    "‑": "-",  # non-breaking hyphen
}
_CHAR_TRANSLATION = str.maketrans(_CHAR_MAP)


def normalize_characters(text: str, stats: CleanupStats) -> str:
    stats.chars_normalized += sum(text.count(ch) for ch in _CHAR_MAP)
    return text.translate(_CHAR_TRANSLATION)


# Runs of U+FFFD (the replacement character) are unrecoverable extraction junk —
# in practice they are the dot leaders of TOC lines. Lone occurrences are kept:
# a single U+FFFD marks one lost character and deleting it could join words.
_REPLACEMENT_RUN = re.compile(r"�{2,}")


def scrub_replacement_runs(text: str, stats: CleanupStats) -> str:
    def _sub(m: re.Match[str]) -> str:
        stats.chars_normalized += len(m.group(0))
        return ""

    return _REPLACEMENT_RUN.sub(_sub, text)


# ---------------------------------------------------------------------------
# Pass 1 — running header / page-number removal
# ---------------------------------------------------------------------------
# Only strip bare page-number lines when there are enough of them to look like a
# real pagination stream; a lone "2024" in prose must not be deleted.
_BARE_PAGE_MIN_COUNT = 5
# Front-matter pages use lowercase roman numerals ("xvii"). The strict form
# rejects ordinary words; 2+ chars so a lone "i" (the pronoun, lowercased) or
# "x" (a variable) is never eaten.
_BARE_ROMAN_RE = re.compile(
    r"^\s*(?=[ivxlcdm]{2,8}\s*$)"
    r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})\s*$"
)
_BARE_ROMAN_MIN_COUNT = 3


def _core_text(line: str) -> str | None:
    """Return the alphabetic portion of a numbered footer line, else None.

    Runs the lead pattern once and only falls back to the trail pattern, picking
    the capture group from whichever match actually fired.
    """
    m = _FOOTER_LEAD_NUM_RE.match(line)
    if m:
        text = m.group(2).strip()
    else:
        m = _FOOTER_TRAIL_NUM_RE.match(line)
        if not m:
            return None
        text = m.group(1).strip()
    # Must contain letters (so we don't eat "1 2 3" data rows) and not look like
    # a Markdown table row.
    if not re.search(r"[A-Za-z]", text) or "|" in text:
        return None
    return text


def _find_running_headers(lines: list[str]) -> set[str]:
    """Texts that recur often enough across the doc to be headers/footers."""
    counts: Counter[str] = Counter()
    for line in lines:
        core = _core_text(line)
        if core is not None:
            counts[core.lower()] += 1
    return {text for text, n in counts.items() if n >= _HEADER_MIN_REPEATS}


def _isolated_bare(index: int, lines: list[str], bare: set[int]) -> bool:
    """True if the bare-number line at ``index`` is not adjacent to another bare
    number — page numbers are isolated; a numeric list/column is a run, so this
    keeps real consecutive-number data."""
    prev = index - 1
    while prev >= 0 and lines[prev].strip() == "":
        prev -= 1
    nxt = index + 1
    while nxt < len(lines) and lines[nxt].strip() == "":
        nxt += 1
    return prev not in bare and nxt not in bare


def strip_page_noise(text: str, stats: CleanupStats) -> str:
    lines = text.split("\n")
    running = _find_running_headers(lines)
    # Only treat bare numbers as page noise when many of them appear AND each is
    # isolated — a real paginated document has a stream of scattered page numbers;
    # a stray year/value or a numeric list (a run of numbers) must be kept.
    bare = {i for i, ln in enumerate(lines) if _BARE_PAGE_RE.match(ln)}
    roman = {i for i, ln in enumerate(lines) if _BARE_ROMAN_RE.match(ln)}
    strip_bare = len(bare) >= _BARE_PAGE_MIN_COUNT
    strip_roman = len(roman) >= _BARE_ROMAN_MIN_COUNT
    numeric = bare | roman
    out: list[str] = []
    for i, line in enumerate(lines):
        if (
            (strip_bare and i in bare or strip_roman and i in roman)
            and _isolated_bare(i, lines, numeric)
        ):
            stats.removed_noise_lines += 1
            continue
        core = _core_text(line)
        if core is not None and core.lower() in running:
            stats.removed_noise_lines += 1
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Pass 1b — strip scrambled multi-column TOC / index tables
# ---------------------------------------------------------------------------
# A scrambled TOC/index is uniquely identified by having BOTH page references
# (bare page numbers / lowercase roman numerals) AND many empty cells (the ragged
# multi-column layout). Requiring both is what separates it from a numeric data
# table (ints but no empties) and a mangled wide table (empties but no page
# numbers) — either signal alone produces false positives.
_NAV_BARE_NUMBER = re.compile(r"\d{1,4}")
# A *valid* lowercase roman numeral (1–3999). The strict form rejects ordinary
# words made of roman letters ("civil", "mild", "did", "dim", "vivid") that a
# loose [ivxlcdm]+ class would wrongly treat as page references.
_NAV_ROMAN = re.compile(r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})")
_SEPARATOR_CELL = re.compile(r"[\s:-]*")
# Bridge short non-table fragments between TOC rows so one TOC counts as one
# region; large enough only to span the wrapped title fragments markitdown emits.
_TOC_REGION_GAP = 3
# Gap between two *navigational* regions whose interstitial lines (wrapped TOC
# title fragments) should also be dropped. Only applies between two confirmed
# nav regions, so real tables are never bridged into removal.
_TOC_BRIDGE_MAX = 8
# A region is a scrambled TOC/index only if BOTH thresholds are met, and it has
# at least a few rows (so a tiny table with one numeric cell is never removed).
_TOC_PAGEREF_THRESHOLD = 0.20
_TOC_EMPTY_THRESHOLD = 0.15
_TOC_MIN_ROWS = 4


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _table_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return any("-" in c for c in cells) and all(
        _SEPARATOR_CELL.fullmatch(c) for c in cells
    )


def _is_page_ref_cell(cell: str) -> bool:
    # _NAV_ROMAN matches the empty string (all groups optional), so guard on cell.
    return bool(cell) and bool(
        _NAV_BARE_NUMBER.fullmatch(cell) or _NAV_ROMAN.fullmatch(cell)
    )


def _table_regions(lines: list[str]) -> list[tuple[int, int]]:
    """Spans [start, end] (inclusive, by index) of table activity.

    Consecutive table rows separated by at most ``_TOC_REGION_GAP`` non-table
    lines are merged into one region; the span is trimmed to the first/last table
    row so surrounding prose is never included.
    """
    regions: list[tuple[int, int]] = []
    pipes = [i for i, ln in enumerate(lines) if _is_table_row(ln)]
    if not pipes:
        return regions
    start = prev = pipes[0]
    for idx in pipes[1:]:
        if idx - prev - 1 > _TOC_REGION_GAP:
            regions.append((start, prev))
            start = idx
        prev = idx
    regions.append((start, prev))
    return regions


def _region_is_navigational(lines: list[str], start: int, end: int) -> bool:
    empty = page_ref = total = rows = 0
    for i in range(start, end + 1):
        line = lines[i]
        if not _is_table_row(line):
            continue
        cells = _table_cells(line)
        if _is_separator_row(cells):
            continue
        rows += 1
        for cell in cells:
            total += 1
            if cell == "":
                empty += 1
            elif _is_page_ref_cell(cell):
                page_ref += 1
    if total == 0 or rows < _TOC_MIN_ROWS:
        return False
    return (
        page_ref / total >= _TOC_PAGEREF_THRESHOLD
        and empty / total >= _TOC_EMPTY_THRESHOLD
    )


def _looks_like_toc_fragment(line: str) -> bool:
    """A wrapped TOC-title fragment: blank, or short and not a full sentence.

    Real prose paragraphs are long and/or end in sentence punctuation, so this
    keeps them even when they happen to fall between two TOC tables.
    """
    s = line.strip()
    if not s:
        return True
    if len(s) > 90:
        return False
    return s[-1] not in ".!?" and not _is_table_row(line)


def strip_toc_tables(text: str, stats: CleanupStats) -> str:
    """Remove table regions dominated by empty / page-number / roman cells."""
    lines = text.split("\n")
    nav_regions = [
        (s, e) for (s, e) in _table_regions(lines) if _region_is_navigational(lines, s, e)
    ]
    if not nav_regions:
        return text
    drop = set()
    for start, end in nav_regions:
        drop.update(range(start, end + 1))
    # Between two nav regions, drop only lines that look like wrapped TOC-title
    # fragments — never real prose that happens to sit between TOC tables.
    for (_s1, e1), (s2, _e2) in zip(nav_regions, nav_regions[1:]):
        if s2 - e1 - 1 <= _TOC_BRIDGE_MAX:
            for k in range(e1 + 1, s2):
                if _looks_like_toc_fragment(lines[k]):
                    drop.add(k)
    stats.toc_lines_removed += len(drop)
    return "\n".join(line for i, line in enumerate(lines) if i not in drop)


# ---------------------------------------------------------------------------
# Pass 1c — strip plain-text TOC blocks
# ---------------------------------------------------------------------------
# PDF TOCs also survive as *plain text*: "Title ....... 26" dot-leader lines
# (the leader often arrives as runs of U+FFFD or box-drawing characters) and
# "Sub-section • 26" entries. Individually each line is ambiguous; a dense
# cluster of them is unmistakably a TOC, so removal is gated on both a global
# count and a per-cluster count.
_TOC_LEADER_RE = re.compile(
    r"^\s*\S.{0,150}?[\s]*"
    r"(?:\.{5,}|[·‐-―─-▟�]{3,})"
    r"[\s.]*(\d{1,4}|[ivxlcdm]{1,8})?\s*$"
)
# "Understanding DDD • 81" — text, a mid-line bullet, then a page ref at EOL.
# Must NOT start with a bullet (that's a real list item) and the page ref must
# be a bare number/roman, so "• Python 3.6" style content is never matched.
_TOC_BULLET_PAGE_RE = re.compile(
    r"^(?![\s]*[•\-\*•])\s*\S.{0,120}?\s+•\s+(\d{1,4}|[ivxlcdm]{1,8})\s*$"
)
_TOC_HEADING_RE = re.compile(r"^\s*table of contents\s*$", re.IGNORECASE)
# The document must contain this many TOC-signal lines before anything is
# removed, and each cluster must contain this many to qualify.
_PLAIN_TOC_MIN_SIGNALS = 8
_PLAIN_TOC_MIN_CLUSTER = 4
# Signals separated by at most this many non-blank non-signal lines belong to
# one cluster (blank lines don't count — PDF extraction double-spaces text).
_PLAIN_TOC_MAX_GAP = 4


def _is_plain_toc_signal(line: str) -> bool:
    s = line.rstrip()
    if not s or _is_table_row(s):
        return False
    return bool(
        _TOC_LEADER_RE.match(s)
        or _TOC_BULLET_PAGE_RE.match(s)
        or _TOC_HEADING_RE.match(s)
    )


def strip_plain_toc(text: str, stats: CleanupStats) -> str:
    lines = text.split("\n")
    signals = [i for i, ln in enumerate(lines) if _is_plain_toc_signal(ln)]
    if len(signals) < _PLAIN_TOC_MIN_SIGNALS:
        return text
    # Cluster the signal lines, then drop each cluster's span including the
    # interstitial lines that look like wrapped TOC fragments (chapter/part
    # skeleton lines, bare page numbers, blanks). Prose sentences between
    # clusters survive because they fail the fragment test.
    clusters: list[list[int]] = [[signals[0]]]
    for idx in signals[1:]:
        gap = sum(1 for ln in lines[clusters[-1][-1] + 1 : idx] if ln.strip())
        if gap <= _PLAIN_TOC_MAX_GAP:
            clusters[-1].append(idx)
        else:
            clusters.append([idx])
    drop: set[int] = set()
    for cluster in clusters:
        if len(cluster) < _PLAIN_TOC_MIN_CLUSTER:
            continue
        drop.update(cluster)
        for k in range(cluster[0], cluster[-1] + 1):
            if k not in drop and _looks_like_toc_fragment(lines[k]):
                drop.add(k)
    stats.toc_lines_removed += len(drop)
    return "\n".join(line for i, line in enumerate(lines) if i not in drop)


# ---------------------------------------------------------------------------
# Pass 1d — chapter heading promotion + running-header removal
# ---------------------------------------------------------------------------
# Book PDFs put the chapter list in the TOC as "Chapter 3: Title" and then use
# the bare title as a running header on every page of that chapter. The *real*
# chapter opening is the first bare-title occurrence (often hard-wrapped over
# two lines). Harvesting the titles gives an exact, low-risk way to (a) turn
# the opening into a Markdown heading and (b) delete the running headers.
_CHAPTER_LINE_RE = re.compile(
    r"^\s*(Chapter|Part)\s+(\d{1,3}|[IVXLC]{1,7})\s*[:.]\s*(\S.*?)(?:\s{2,}\d{1,4})?\s*$"
)
_CHAPTER_MIN_TITLES = 3
_TITLE_MAX_WRAP_LINES = 3


def _squash_ws(s: str) -> str:
    return " ".join(s.split())


def _harvest_section_titles(text: str) -> dict[str, str]:
    """Map of bare title -> full heading text, from "Chapter N: Title" lines."""
    titles: dict[str, str] = {}
    for line in text.split("\n"):
        m = _CHAPTER_LINE_RE.match(line)
        if m is None:
            continue
        kind, num, title = m.group(1), m.group(2), _squash_ws(m.group(3))
        # Too-short titles ("Chapter 4: Summary 88") risk colliding with prose.
        if len(title) < 8:
            continue
        titles.setdefault(title, f"{kind} {num}: {title}")
    return titles


def promote_chapter_headings(text: str, titles: dict[str, str], stats: CleanupStats) -> str:
    """Promote chapter openings to ``#`` headings; drop running-header repeats.

    The first occurrence of a bare chapter title (single line, or wrapped over
    2–3 consecutive lines) becomes ``# Chapter N: Title``. Every later bare
    occurrence is a page running-header and is removed.
    """
    if len(titles) < _CHAPTER_MIN_TITLES:
        return text
    lines = text.split("\n")
    # If a chapter title already exists as a Markdown heading, the converter
    # produced real headings and a bare title line is always a running header:
    # remove every occurrence instead of promoting the first to a duplicate.
    # (Counting '#' lines is not enough — unfenced code comments look like
    # headings and would wrongly suppress promotion.)
    heading_lines = [ln for ln in lines if ln.lstrip().startswith("#")]
    promote = not any(t in h for h in heading_lines for t in titles)
    seen: set[str] = set()
    out: list[str] = []
    in_fence = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if _fence_line(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence or not line.strip():
            out.append(line)
            i += 1
            continue
        # Try to match a title starting at this line, allowing the PDF's hard
        # wrap to split it over up to _TITLE_MAX_WRAP_LINES consecutive lines.
        matched = None
        joined = ""
        for span in range(1, _TITLE_MAX_WRAP_LINES + 1):
            if i + span > n:
                break
            seg = lines[i + span - 1].strip()
            if not seg or _fence_line(lines[i + span - 1]):
                break
            joined = _squash_ws(f"{joined} {seg}".strip())
            if joined in titles:
                matched = (span, joined)
                break
            # No title starts this way — stop extending early.
            if not any(t.startswith(joined) for t in titles):
                break
        if matched is not None:
            span, title = matched
            if promote and title not in seen:
                seen.add(title)
                out.append(f"# {titles[title]}")
                stats.headings_promoted += 1
            else:
                stats.removed_noise_lines += span
            i += span
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Pass 1e — repeated boilerplate blocks
# ---------------------------------------------------------------------------
# Publisher boilerplate ("Scan the QR code … packtpub.com/unlock …") repeats
# verbatim once per chapter. A line is only boilerplate when its whole
# *neighbourhood* repeats — the same line with the same non-blank line above
# and below it. Repeated sentences in running prose ("The output is:") have
# different neighbours each time, so they are never touched.
_BOILERPLATE_MIN_REPEATS = 3
_BOILERPLATE_MIN_LEN = 20


def _neighbourhood_keys(lines: list[str]) -> list[tuple[str, str, str] | None]:
    """Per line: (previous non-blank, line, next non-blank), or None for
    blanks / fence markers / fence content, which must never be counted."""
    stripped = [ln.strip() for ln in lines]
    off_limits = [False] * len(lines)
    state = False
    for i, ln in enumerate(lines):
        if _fence_line(ln):
            state = not state
            off_limits[i] = True  # the marker itself is off-limits either way
        else:
            off_limits[i] = state
    prevs: list[str] = [""] * len(lines)
    p = ""
    for i, s in enumerate(stripped):
        prevs[i] = p
        if s and not off_limits[i]:
            p = s
    keys: list[tuple[str, str, str] | None] = [None] * len(lines)
    nxt = ""
    for i in range(len(lines) - 1, -1, -1):
        s = stripped[i]
        if off_limits[i]:
            continue
        if s:
            keys[i] = (prevs[i], s, nxt)
            nxt = s
    return keys


def strip_repeated_blocks(text: str, stats: CleanupStats) -> str:
    lines = text.split("\n")
    keys = _neighbourhood_keys(lines)
    counts: Counter[tuple[str, str, str]] = Counter(k for k in keys if k is not None)
    out: list[str] = []
    for line, key in zip(lines, keys):
        s = line.strip()
        if (
            key is not None
            and counts[key] >= _BOILERPLATE_MIN_REPEATS
            and len(s) >= _BOILERPLATE_MIN_LEN
            and re.search(r"[A-Za-z]", s)
            and not s.startswith(("#", "|", ">"))
        ):
            stats.boilerplate_lines_removed += 1
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Pass 2 — auto-fence code listings
# ---------------------------------------------------------------------------
# A *strong* line can START a code block — prose effectively never begins this
# way. A *weak* line can only EXTEND an already-open block (e.g. an argument or a
# closing bracket on its own line). This asymmetry is the key to not pulling
# wrapped prose fragments (like "... documents:") into a fence.
# Strong-line detection uses *precise structural* patterns rather than a bare
# first-word keyword test — common English words (for/with/from/class...) start
# wrapped prose lines, so matching them loosely fences real text. Each pattern
# requires syntax that prose lacks: an import form, a def/class header, a
# decorator, a compound statement ending in ':', or a single-target assignment.
_IMPORT_RE = re.compile(r"^(?:import\s+\w|from\s+\S+\s+import\b)")
_DEF_CLASS_RE = re.compile(r"^(?:async\s+)?(?:def|class)\s+\w")
_DECORATOR_RE = re.compile(r"^@\w")
_COMPOUND_RE = re.compile(
    r"^(?:if|elif|else|for|while|try|except|finally|with|async\s+for|async\s+with)\b"
)
# Single-target assignment (name / attr / index), NOT an '==' comparison and NOT
# a multi-word prose phrase before '=' ("Energy E = mc ...").
_ASSIGN_LINE = re.compile(r"^[A-Za-z_][\w.\[\]]*\s*(?:=|:=|\+=|-=|\*=|/=)(?!=)\s*\S")
# Short continuation line: brackets, a trailing comma/bracket, or a bare literal.
_WEAK_CODE = re.compile(r"[()\[\]{}=]|[,)\]};]\s*$")
# Bare flow-control keywords appear alone on code lines but never as whole
# prose lines ("return post", "pass", "end", "}").
_BARE_KEYWORD = re.compile(
    r"^(?:return\b.*|pass|break|continue|end|yield\b.*|raise\b.*|[}\])];?|//.*|\[\w+\])$"
)
# C-family statement starts. The keyword alone is not enough — "static analysis
# is a technique" is prose — so the line must also carry code punctuation.
_C_FAMILY_RE = re.compile(
    r"^(?:(?:public|private|protected|internal|static|virtual|override|async|final)\s+\w"
    r"|var\s+\w+\s*=|using\s+[A-Z][\w.]*\s*;|#include\s*[<\"]"
    r"|func\s+\w+|namespace\s+[A-Z])"
)
_CODE_PUNCT_RE = re.compile(r"[;{}()<>=]")


def _is_strong_code(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 140:
        return False
    if _IMPORT_RE.match(s) or _DECORATOR_RE.match(s):
        return True
    if _DEF_CLASS_RE.match(s) and ("(" in s or s.rstrip().endswith(":")):
        return True
    if _COMPOUND_RE.match(s) and s.rstrip().endswith(":"):
        return True
    if _C_FAMILY_RE.match(s) and _CODE_PUNCT_RE.search(s):
        return True
    return bool(_ASSIGN_LINE.match(s))


def _is_weak_code(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if _BARE_KEYWORD.match(s):
        return True
    # An indented continuation (method bodies, comments inside listings) —
    # prose paragraphs are never indented in markitdown output.
    if line[:4] == "    " and len(s.split()) <= 10:
        return True
    if len(s) > 100 or len(s.split()) > 6:
        return False
    if _WEAK_CODE.search(s):
        return True
    # A short, quoted string literal on its own line (a wrapped argument).
    return (s[0] in "\"'") and (s[-1] in "\"'")


# --- language guessing ------------------------------------------------------
# The fence label used to be hardcoded to "python", which mislabelled Go/C++/C#
# listings in non-Python books. Each pattern is scored over the block; the
# clear winner names the fence, and an unidentifiable C-like block gets a plain
# fence rather than a wrong label.
_LANG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cpp", re.compile(r"#include\s*[<\"]|\bstd::|template\s*<|->\s*\w+\s*\(|\bcout\s*<<")),
    ("go", re.compile(r"^\s*func\s+\w|^\s*package\s+\w+\s*$|:=|\bfmt\.\w+\(|\bgo\s+func\b", re.M)),
    (
        "csharp",
        # "public class" alone is Java too — only C#-specific forms count.
        re.compile(
            r"^\s*using\s+[A-Z]\w*(?:\.\w+)*\s*;|\bnamespace\s+[A-Z]"
            r"|Console\.Write|\{\s*get;\s*set;\s*\}|\basync\s+Task\b|\bpublic\s+record\b"
            r"|\bIActionResult\b|\[HttpGet|\[HttpPost",
            re.M,
        ),
    ),
    (
        "java",
        re.compile(
            r"^\s*(?:package|import)\s+[a-z]\w*(?:\.\w+){2,}\s*;|System\.out\.print"
            r"|@Override\b|\b(?:extends|implements)\s+[A-Z]",
            re.M,
        ),
    ),
    (
        "javascript",
        re.compile(r"^\s*(?:const|let)\s+\w+\s*=|=>\s*[{(]|console\.log\(|\bfunction\s+\w+\(", re.M),
    ),
    (
        "ruby",
        # A def header WITHOUT a trailing ':' is Ruby's (Python's needs one).
        re.compile(
            r"^\s*require\s+['\"]|\battr_(?:reader|writer|accessor)\b|^\s*puts\s"
            r"|\bdo\s*\|\w+\||^\s*end\s*$|^\s*def\s+(?:self\.)?\w+[?!]?(?:\(.*\))?\s*$"
            r"|^\s*elsif\b",
            re.M,
        ),
    ),
    (
        "kotlin",
        re.compile(
            r"^\s*fun\s+\w+\(|\bval\s+\w+\s*[:=]|^\s*data\s+class\s|\bcompanion\s+object\b"
            r"|^\s*import\s+(?:java|javax|kotlin|kotlinx|android)\.",
            re.M,
        ),
    ),
    (
        "python",
        # def/class must end the line with ':' — "class Person(...) {" is Kotlin.
        # JVM-package imports ("import java.util.UUID", "import org.spring...")
        # are Kotlin/Java, not Python. Bare "@word" annotations are NOT a Python
        # signal: Kotlin/Java annotations look identical to decorators.
        re.compile(
            r"^\s*(?:def\s+\w+.*:\s*$|class\s+\w+.*:\s*$"
            r"|import\s+(?!java\.|javax\.|kotlin|android\.|org\.|com\.)\w+"
            r"|from\s+\S+\s+import|elif\b)|\bself\.",
            re.M,
        ),
    ),
)


def _lang_scores(code: str) -> dict[str, int]:
    return {lang: len(pat.findall(code)) for lang, pat in _LANG_PATTERNS}


def _c_like_share(code: str) -> float:
    lines = [l for l in code.split("\n") if l.strip()]
    if not lines:
        return 0.0
    return sum(1 for l in lines if l.rstrip().endswith((";", "{", "}"))) / len(lines)


def _guess_language(code: str) -> str:
    scores = _lang_scores(code)
    best = max(scores, key=lambda k: scores[k])
    top = scores[best]
    runner_up = max(v for k, v in scores.items() if k != best)
    # A clear winner: two signals with a 2x margin, or one distinctive signal
    # with no competing language signals at all.
    if top >= runner_up * 2 and (top >= 2 or (top == 1 and runner_up == 0)):
        return best
    if _c_like_share(code) >= 0.34:
        # Clearly C-family but no clear winner: a plain fence beats a wrong label.
        return best if top >= 2 else ""
    # Mixed signals (e.g. Ruby's "self." looks Pythonic): plain fence. The
    # unambiguous single-language cases all returned above.
    return ""


def fence_code_blocks(text: str, stats: CleanupStats) -> str:
    """Wrap contiguous runs of code lines in fenced blocks.

    A run may only *start* on a strong code line (import/def/class/assignment),
    so a fence is never opened inside a wrapped prose sentence. Once open, it
    extends over strong lines, weak continuation lines, and a single interior
    blank. Runs shorter than ``min_run`` real lines are left untouched. The
    fence language is guessed from the run's content.

    Any pre-existing fenced block is passed through verbatim (so a single stray
    fence elsewhere no longer disables fencing for the whole document).
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    min_run = 2

    while i < n:
        line = lines[i]
        # Pass an existing fenced block through untouched.
        if _fence_line(line):
            out.append(line)
            i += 1
            while i < n:
                out.append(lines[i])
                closing = _fence_line(lines[i])
                i += 1
                if closing:
                    break
            continue

        if _is_strong_code(line):
            j = i
            run: list[str] = []
            blank_held: list[str] = []
            while j < n:
                cur = lines[j]
                if _is_strong_code(cur) or _is_weak_code(cur):
                    run.extend(blank_held)
                    blank_held = []
                    run.append(cur)
                    j += 1
                elif cur.strip() == "" and not blank_held:
                    blank_held = [cur]  # tolerate one interior blank
                    j += 1
                else:
                    break
            code_lines = [ln for ln in run if ln.strip()]
            if len(code_lines) >= min_run:
                out.append("```" + _guess_language("\n".join(run)))
                out.extend(run)
                out.append("```")
                stats.code_blocks_fenced += 1
                # A held-but-unused trailing blank must be reprocessed, not lost.
                i = j - len(blank_held)
                continue
        out.append(line)
        i += 1

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Pass 2b — repair fragmented / mislabelled fences
# ---------------------------------------------------------------------------
# Re-cleaning a document that was fenced by an earlier version of this module
# (or another tool) often finds listings shattered into several small fences
# with orphaned code lines between them, every code line double-spaced, and the
# label hardcoded to "python" regardless of language. This pass merges those
# fragments, tightens the spacing, and re-guesses obviously wrong labels.
_FENCE_GAP_MAX_CODE_LINES = 8


def _gap_is_code(gap: list[str]) -> bool:
    body = [ln for ln in gap if ln.strip()]
    if not body or len(body) > _FENCE_GAP_MAX_CODE_LINES:
        return False
    return all(_is_strong_code(ln) or _is_weak_code(ln) for ln in body)


def repair_fences(text: str, stats: CleanupStats) -> str:
    lines = text.split("\n")
    fences = [i for i, ln in enumerate(lines) if _fence_line(ln)]
    if len(fences) < 2:
        return text
    drop: set[int] = set()
    # Fences alternate open/close; walk close->open pairs and merge when the
    # gap between them is nothing but code/blank lines.
    k = 1  # fences[k] is a closing fence when k is odd
    while k + 1 < len(fences):
        close, reopen = fences[k], fences[k + 1]
        if close not in drop and _gap_is_code(lines[close + 1 : reopen]):
            drop.add(close)
            drop.add(reopen)
            stats.fences_merged += 1
            k += 2  # the block continues; fences[k+2] is its new closer
        else:
            k += 2
    if drop:
        lines = [ln for i, ln in enumerate(lines) if i not in drop]

    # Re-label blocks and collapse PDF double-spacing inside them.
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not _fence_line(line):
            out.append(line)
            i += 1
            continue
        indent = line[: len(line) - len(line.lstrip())]
        label = line.strip()[3:].strip()
        j = i + 1
        block: list[str] = []
        while j < n and not _fence_line(lines[j]):
            block.append(lines[j])
            j += 1
        body = [ln for ln in block if ln.strip()]
        blanks = len(block) - len(body)
        # Double-spaced listing: blanks between nearly every pair of lines.
        if len(body) >= 3 and blanks >= len(body) - 1:
            stats.blank_runs_collapsed += blanks
            block = body
        code = "\n".join(body)
        guess = _guess_language(code)
        if label in ("", "python") and guess and guess != label:
            label = guess
        elif label == "python" and not guess and body:
            # The label may be a stale hardcoded "python" from an earlier
            # fencing pass. Downgrade it only on positive evidence against —
            # a rival language outscoring Python, or C-family punctuation with
            # no Python signals. A neutral snippet keeps its label.
            sc = _lang_scores(code)
            rival = max(v for k, v in sc.items() if k != "python")
            if rival > sc["python"] or (
                sc["python"] == 0 and _c_like_share(code) >= 0.34
            ):
                label = ""
        out.append(f"{indent}```{label}")
        out.extend(block)
        if j < n:
            out.append(lines[j])  # the closing fence
        i = j + 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Pass 2c — normalize bullets
# ---------------------------------------------------------------------------
# PDF extraction emits "•  item" (kept visually but not valid Markdown syntax
# for every renderer) and sometimes shears lists apart: k lines holding only
# "•" followed by the k item texts. Both are repaired here.
_BULLET_LINE_RE = re.compile(r"^(\s*)•\s+(\S.*)$")
_LONE_BULLET_RE = re.compile(r"^\s*•\s*$")


def _bullet_item_candidate(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 120:
        return False
    if s.startswith(("#", "|", ">", "-", "*", "```", "•")):
        return False
    # Sheared list items are title-like fragments; a full sentence next to a
    # lone bullet is regular prose, not the bullet's missing text.
    if s[-1] in ".!?:":
        return False
    return not _is_strong_code(line)


def normalize_bullets(text: str, stats: CleanupStats) -> str:
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if _fence_line(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue
        # A run of k lone "•" lines followed by k short item lines: the PDF's
        # two-column shear. Re-pair them; if the counts don't line up exactly,
        # leave everything untouched.
        if _LONE_BULLET_RE.match(line):
            j = i
            k = 0
            while j < n and (_LONE_BULLET_RE.match(lines[j]) or not lines[j].strip()):
                if _LONE_BULLET_RE.match(lines[j]):
                    k += 1
                j += 1
            items: list[str] = []
            m = j
            while m < n and len(items) < k:
                if not lines[m].strip():
                    m += 1
                    continue
                if not _bullet_item_candidate(lines[m]):
                    break
                items.append(lines[m].strip())
                m += 1
            if k >= 1 and len(items) == k:
                for item in items:
                    out.append(f"- {item}")
                    out.append("")
                stats.bullets_normalized += k
                i = m
                continue
            out.append(line)
            i += 1
            continue
        m2 = _BULLET_LINE_RE.match(line)
        if m2:
            out.append(f"{m2.group(1)}- {m2.group(2)}")
            stats.bullets_normalized += 1
            i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Pass 2d — join hard-wrapped prose lines
# ---------------------------------------------------------------------------
# PDF text keeps the printed column width, so paragraphs arrive as blocks of
# ~65–100 char lines. Inside a block (no blank lines), a near-full-width line
# that isn't structural Markdown can only be a hard wrap — join it with the
# next line. Short lines (headings, list items, the last line of a paragraph)
# never trigger a join because they end their block or fall under the length
# floor. Hyphenated wraps rejoin the split word, keeping the hyphen only for
# real compounds ("parents-in-" + "law", but "Ag-" + "ile" -> "Agile").
_WRAP_MIN_LEN = 65
# Stop joining once the accumulated line is this long: no real paragraph is
# 4k chars of unpunctuated prose (degenerate OCR/transcript input is), and
# capping it keeps the repeated join-copies linear instead of quadratic.
_JOIN_MAX_LEN = 4000
_HYPHEN_WRAP_RE = re.compile(r"([A-Za-z][A-Za-z']*)-$")
# Code that escaped fencing must never be folded into prose: refuse to join
# any line that ends in a statement terminator/brace, starts like a code
# continuation, or carries a lambda arrow. A skipped join is harmless.
_JOIN_UNSAFE_RE = re.compile(r"[;{}]\s*$|^\s*(?:\[|//|\.)|=>")
# Only join a line that breaks off MID-sentence. A line ending in terminal
# punctuation may be followed by an embedded section heading ("Who this book
# is for") in documents without blank-line paragraph breaks — and Markdown
# already renders adjacent lines as one paragraph, so stopping there is free.
_SENTENCE_END = (".", "!", "?", ":", ";", '"', "”", "’", "…")


def _is_structural(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    # "<" also shields HTML comments — e.g. <!-- page 12 --> citation anchors.
    if s.startswith(("#", "|", ">", "-", "*", "```", "•", "<")):
        return True
    if re.match(r"^\d+[.)]\s", s):
        return True
    return _is_strong_code(line)


def join_wrapped_lines(text: str, stats: CleanupStats) -> str:
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    for line in lines:
        if _fence_line(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or not out:
            out.append(line)
            continue
        prev = out[-1]
        if (
            line.strip()
            # The cap short-circuits before every prev predicate below, so the
            # full-string scans and join copies are all bounded by ~4k chars —
            # without it, thousands of consecutive joins (hard-wrapped OCR text
            # with no sentence punctuation) made this pass quadratic: minutes
            # on one bad document.
            and len(prev) <= _JOIN_MAX_LEN
            and not line[:1].isspace()
            and not _is_structural(line)
            and not _is_structural(prev)
            and not _fence_line(prev)
            and prev.strip()
            and not _JOIN_UNSAFE_RE.search(line.strip())
            and not _JOIN_UNSAFE_RE.search(prev.strip())
        ):
            hyphen = _HYPHEN_WRAP_RE.search(prev.rstrip())
            if hyphen and line[:1].islower():
                fragment = hyphen.group(1)
                before = prev.rstrip()[: -len(fragment) - 1][-1:]
                # "Ag-" + "ile" -> "Agile"; but "parents-in-" + "law" is a real
                # compound (a hyphen precedes the fragment), so keep the hyphen.
                if before == "-":
                    out[-1] = prev.rstrip() + line.lstrip()
                else:
                    out[-1] = prev.rstrip()[:-1] + line.lstrip()
                stats.lines_joined += 1
                continue
            if (
                len(prev.strip()) >= _WRAP_MIN_LEN
                and not prev.rstrip().endswith(_SENTENCE_END)
            ):
                out[-1] = prev.rstrip() + " " + line.lstrip()
                stats.lines_joined += 1
                continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Pass 3 — collapse excessive blank lines
# ---------------------------------------------------------------------------
_MANY_BLANKS = re.compile(r"\n{3,}")


def collapse_blank_runs(text: str, stats: CleanupStats) -> str:
    stats.blank_runs_collapsed += len(_MANY_BLANKS.findall(text))
    return _MANY_BLANKS.sub("\n\n", text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def clean_markdown(
    text: str,
    *,
    normalize_chars: bool = True,
    strip_noise: bool = True,
    strip_toc: bool = True,
    promote_headings: bool = True,
    strip_boilerplate: bool = True,
    fence_code: bool = True,
    bullets: bool = True,
    join_wrapped: bool = True,
    collapse_blanks: bool = True,
) -> tuple[str, CleanupStats]:
    """Run the enabled cleanup passes; return ``(cleaned_text, stats)``."""
    stats = CleanupStats()
    if not text:
        return text, stats
    if normalize_chars:
        text = normalize_characters(text, stats)
    # Titles must be harvested before the TOC (their source) is stripped.
    titles = _harvest_section_titles(text) if promote_headings else {}
    if strip_noise:
        text = strip_page_noise(text, stats)
    if strip_toc:
        text = strip_toc_tables(text, stats)
        text = strip_plain_toc(text, stats)
    if promote_headings:
        text = promote_chapter_headings(text, titles, stats)
    if strip_boilerplate:
        text = strip_repeated_blocks(text, stats)
    if fence_code:
        text = repair_fences(text, stats)
        text = fence_code_blocks(text, stats)
    if bullets:
        text = normalize_bullets(text, stats)
    if join_wrapped:
        text = join_wrapped_lines(text, stats)
    if normalize_chars:
        text = scrub_replacement_runs(text, stats)
    if collapse_blanks:
        text = collapse_blank_runs(text, stats)
    return text.strip() + "\n", stats
