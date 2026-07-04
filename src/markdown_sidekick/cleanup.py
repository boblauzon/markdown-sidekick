"""Post-conversion cleanup for markitdown output.

markitdown extracts text faithfully but carries over PDF artifacts: running
headers / page numbers leak mid-flow, and code listings arrive as un-fenced
plain text. These passes are intentionally *conservative* — it is far better to
leave a little noise than to delete real content, so every heuristic favours
false negatives over false positives.

The entry point is :func:`clean_markdown`. Each pass is independent and can be
toggled, so the UI can expose them individually later if desired.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

# A "running header/footer" is a short line that recurs across many pages.
_HEADER_MIN_REPEATS = 4
_HEADER_MAX_LEN = 70

# Bare page number on its own line, e.g. "  118  ".
_BARE_PAGE_RE = re.compile(r"^\s*\d{1,4}\s*$")
# "118 Indexing with LlamaIndex"  /  "Indexing with LlamaIndex 118"
_FOOTER_LEAD_NUM_RE = re.compile(r"^\s*(\d{1,4})\s+(.{1,%d}?)\s*$" % _HEADER_MAX_LEN)
_FOOTER_TRAIL_NUM_RE = re.compile(r"^\s*(.{1,%d}?)\s+(\d{1,4})\s*$" % _HEADER_MAX_LEN)


@dataclass
class CleanupStats:
    """What the cleanup pass changed — surfaced to the user as feedback."""

    removed_noise_lines: int = 0
    toc_lines_removed: int = 0
    code_blocks_fenced: int = 0
    blank_runs_collapsed: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.removed_noise_lines
            or self.toc_lines_removed
            or self.code_blocks_fenced
            or self.blank_runs_collapsed
        )

    def summary(self) -> str:
        if not self.changed:
            return "No cleanup changes."
        parts = []
        if self.removed_noise_lines:
            parts.append(f"{self.removed_noise_lines} header/page-number line(s) removed")
        if self.toc_lines_removed:
            parts.append(f"{self.toc_lines_removed} TOC/index line(s) removed")
        if self.code_blocks_fenced:
            parts.append(f"{self.code_blocks_fenced} code block(s) fenced")
        if self.blank_runs_collapsed:
            parts.append(f"{self.blank_runs_collapsed} blank run(s) collapsed")
        return "Cleanup: " + ", ".join(parts) + "."


# ---------------------------------------------------------------------------
# Pass 1 — running header / page-number removal
# ---------------------------------------------------------------------------
# Only strip bare page-number lines when there are enough of them to look like a
# real pagination stream; a lone "2024" in prose must not be deleted.
_BARE_PAGE_MIN_COUNT = 5


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
    strip_bare = len(bare) >= _BARE_PAGE_MIN_COUNT
    out: list[str] = []
    for i, line in enumerate(lines):
        if strip_bare and i in bare and _isolated_bare(i, lines, bare):
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
_WEAK_CODE = re.compile(r"[()\[\]{}=]|[,)\]}]\s*$")


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
    return bool(_ASSIGN_LINE.match(s))


def _is_weak_code(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 100 or len(s.split()) > 6:
        return False
    if _WEAK_CODE.search(s):
        return True
    # A short, quoted string literal on its own line (a wrapped argument).
    return (s[0] in "\"'") and (s[-1] in "\"'")


def fence_code_blocks(text: str, stats: CleanupStats) -> str:
    """Wrap contiguous runs of code lines in ```python fences.

    A run may only *start* on a strong code line (import/def/class/assignment),
    so a fence is never opened inside a wrapped prose sentence. Once open, it
    extends over strong lines, weak continuation lines, and a single interior
    blank. Runs shorter than ``min_run`` real lines are left untouched.

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
        if line.lstrip().startswith("```"):
            out.append(line)
            i += 1
            while i < n:
                out.append(lines[i])
                closing = lines[i].lstrip().startswith("```")
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
                out.append("```python")
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
    strip_noise: bool = True,
    strip_toc: bool = True,
    fence_code: bool = True,
    collapse_blanks: bool = True,
) -> tuple[str, CleanupStats]:
    """Run the enabled cleanup passes; return ``(cleaned_text, stats)``."""
    stats = CleanupStats()
    if not text:
        return text, stats
    if strip_noise:
        text = strip_page_noise(text, stats)
    if strip_toc:
        text = strip_toc_tables(text, stats)
    if fence_code:
        text = fence_code_blocks(text, stats)
    if collapse_blanks:
        text = collapse_blank_runs(text, stats)
    return text.strip() + "\n", stats
