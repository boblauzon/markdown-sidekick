"""AI-friendly export: front matter, chapter splitting, index and manifest.

Large single-file conversions are the worst shape for AI tools — they overflow
context windows and defeat retrieval chunkers. This module turns a converted
document into either a decorated single file or a "book folder":

    <stem>/
      index.md            table of contents linking the parts
      manifest.json       machine-readable map (titles, tokens, image counts)
      00-front-matter.md  whatever precedes the first chapter heading
      01-chapter-name.md  one file per top-level heading, each with YAML
      02-...              front matter identifying the book and part
      images/             extracted figures (see figures.py), when enabled

Splitting happens on ``#`` headings (which the PDF layout engine derives from
the PDF's bookmarks, and the cleanup pipeline restores for older book
conversions); a chapter that still exceeds the token budget is sub-split at
its ``##`` boundaries. All writes are UTF-8.

The Gemini Notebook target (``notebook=True``) writes an upload-ready folder
instead: one source file per chapter named after the book, a short readable
header in place of YAML, image links reduced to their captions, and no
index.md / manifest.json (see :func:`split_for_notebook`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import debuglog

_CHARS_PER_TOKEN = 4
DEFAULT_MAX_TOKENS = 30_000

# Gemini Notebook (formerly NotebookLM) is not a chat window: each uploaded
# file becomes a *source* it retrieves from and cites, so its limits shape
# the export rather than a context budget:
#  - 500,000 words per source. A word is at least one character, so a part of
#    at most 480k characters (120k est. tokens) can never exceed it whatever
#    the script (CJK counts each character as a word); the remaining 20k
#    characters absorb the source header.
#  - sources per notebook (50 on the free plan). A chapter is the useful unit
#    — a notebook can scope a chat, study guide or Audio Overview to the
#    sources selected — so chapters are packed together only when a document
#    would otherwise take more than NOTEBOOK_MAX_SOURCES, which leaves room
#    for a second book on the free plan.
NOTEBOOK_SOURCE_TOKENS = 120_000
NOTEBOOK_MAX_SOURCES = 25

# Per-platform section budgets (est. tokens). Sized so several sections fit
# in the platform's context window with room for the conversation itself —
# except notebook targets, whose budget is the per-source cap above.
# "Claude" shares DEFAULT_MAX_TOKENS: the default budget IS the Claude budget,
# and the two must not drift apart.
AI_TARGETS: dict[str, int] = {
    "Claude": DEFAULT_MAX_TOKENS,
    "ChatGPT": 12_000,
    "Gemini": 60_000,
    "Gemini Notebook": NOTEBOOK_SOURCE_TOKENS,
    "Local LLM": 4_000,
}
# Targets exported as notebook sources (export_book(notebook=True)).
NOTEBOOK_TARGETS = frozenset({"Gemini Notebook"})

_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*```")
_HEADING_LINE_RE = re.compile(r"^#{1,6}\s+\S")
_IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_YAML_HOSTILE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_LEAD_TITLE = "Front matter"


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def count_images(text: str) -> int:
    return len(_IMAGE_LINK_RE.findall(text))


def slugify(title: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "section"


def _one_line(value: str) -> str:
    """A metadata string made safe for a single header line."""
    # YAML rejects control characters, and a lone surrogate can't be written
    # as UTF-8 — neither may reach the file from any source.
    return re.sub(r"\s*[\r\n]+\s*", " ", _YAML_HOSTILE_RE.sub("", value)).strip()


def build_front_matter(fields: dict[str, object]) -> str:
    """Minimal YAML front matter. Values are scalars; strings are quoted only
    when they contain YAML-significant characters."""
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, str):
            value = _one_line(value)
        if value is None or value == "":
            continue
        if isinstance(value, str) and re.search(r"[:#\[\]{}\"'|>&%@`,]", value):
            value = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


@dataclass
class Section:
    title: str
    markdown: str

    @property
    def est_tokens(self) -> int:
        return estimate_tokens(self.markdown)


@dataclass
class ExportResult:
    paths: list[Path] = field(default_factory=list)
    index_path: Path | None = None
    manifest_path: Path | None = None

    @property
    def files_written(self) -> int:
        return len(self.paths) + (1 if self.index_path else 0) + (
            1 if self.manifest_path else 0
        )


def _fence_map(lines: list[str]) -> tuple[list[bool], list[bool]]:
    """Per line: (is a ``` fence marker, inside a fence BEFORE this line)."""
    is_fence = [bool(_FENCE_RE.match(line)) for line in lines]
    in_fence: list[bool] = []
    state = False
    for fence in is_fence:
        in_fence.append(state)
        if fence:
            state = not state
    return is_fence, in_fence


def _heading_lines(lines: list[str], pattern: re.Pattern[str]) -> list[int]:
    """Indices of heading lines, ignoring anything inside code fences."""
    is_fence, in_fence = _fence_map(lines)
    return [
        i
        for i, line in enumerate(lines)
        if not is_fence[i] and not in_fence[i] and pattern.match(line)
    ]


def _split_at(lines: list[str], indices: list[int], titles: list[str], lead_title: str) -> list[Section]:
    sections: list[Section] = []
    if indices and indices[0] > 0:
        lead = "\n".join(lines[: indices[0]]).strip()
        if lead:
            sections.append(Section(lead_title, lead + "\n"))
    for n, start in enumerate(indices):
        end = indices[n + 1] if n + 1 < len(indices) else len(lines)
        sections.append(Section(titles[n], "\n".join(lines[start:end]).strip() + "\n"))
    return sections


# Anchors and markers are not content; image links are (a gallery chapter
# of nothing but figures is a real chapter).
_NON_TEXT_LINE_RE = re.compile(r"^\s*(?:<!--.*?-->)?\s*$")


def _is_divider(sec: Section) -> bool:
    """A heading with nothing under it — a book's part-divider page."""
    head, _, body = sec.markdown.partition("\n")
    return head.startswith("#") and all(_NON_TEXT_LINE_RE.match(l) for l in body.split("\n"))


def _fold_dividers(sections: list[Section]) -> list[Section]:
    """Fold part-divider sections ("# GETTING STARTED" alone on its page)
    into the chapter that follows, instead of writing a near-empty file.
    The divider's heading is kept at the top of that chapter's file."""
    out: list[Section] = []
    pending: list[Section] = []
    for sec in sections:
        if _is_divider(sec):
            pending.append(sec)
            continue
        if pending:
            lead = "".join(p.markdown.rstrip() + "\n\n" for p in pending)
            sec = Section(sec.title, lead + sec.markdown)
            pending = []
        out.append(sec)
    out.extend(pending)  # dividers at the very end have nothing to join
    return out


# Book-style structural headings. When several are present, ONLY they define
# split points — other "#" lines in converted books are often stray unfenced
# code comments ("# cli_main.py") and would shred the document into fragments.
_BOOK_HEADING_RE = re.compile(r"^#\s+(?:Chapter|Part|Appendix)\s+[\dIVXLC]", re.IGNORECASE)
_BOOK_MODE_MIN = 3


def split_chapters(markdown: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> list[Section]:
    """Split on ``#`` headings; sub-split oversized chapters at ``##``.

    Returns a single unsplit section when the document has fewer than two
    top-level headings — splitting a heading-less document would be arbitrary.
    """
    lines = markdown.split("\n")
    h1s = _heading_lines(lines, _H1_RE)
    book_h1s = [i for i in h1s if _BOOK_HEADING_RE.match(lines[i].strip())]
    if len(book_h1s) >= _BOOK_MODE_MIN:
        h1s = book_h1s
    if len(h1s) < 2:
        return [Section("", markdown)]
    titles = [_H1_RE.match(lines[i]).group(1) for i in h1s]  # type: ignore[union-attr]
    sections = _fold_dividers(_split_at(lines, h1s, titles, _LEAD_TITLE))

    result: list[Section] = []
    for sec in sections:
        if sec.est_tokens <= max_tokens:
            result.append(sec)
            continue
        sub_lines = sec.markdown.split("\n")
        h2s = _heading_lines(sub_lines, _H2_RE)
        if len(h2s) < 2:
            result.append(sec)  # nothing sensible to split at — keep whole
            continue
        subtitles = [
            f"{sec.title} — {_H2_RE.match(sub_lines[i]).group(1)}"  # type: ignore[union-attr]
            for i in h2s
        ]
        result.extend(_split_at(sub_lines, h2s, subtitles, sec.title))
    return result


def split_for_ai(markdown: str, max_tokens: int, *, pack: bool = True) -> list[Section]:
    """Split into sections that each fit an AI platform's token budget.

    Chapter structure is used when present (via :func:`split_chapters`, which
    already sub-splits oversized chapters at ``##``). Anything still over
    budget — including documents with no headings at all — is hard-split into
    "(part N)" pieces at blank lines outside code fences, falling back to any
    line outside a fence so blank-line-free text still splits. Every part fits
    the budget unless a single indivisible fenced block alone exceeds it —
    fences are never split.

    With ``pack`` (export), small consecutive sections are then packed into
    parts up to the budget (see :func:`_pack`): a book of 130 one-page
    principles becomes a handful of parts, not 130 files. Navigation (the
    MCP outline) passes ``pack=False`` to keep one entry per chapter.
    """
    max_chars = max_tokens * _CHARS_PER_TOKEN
    result: list[Section] = []
    for sec in split_chapters(markdown, max_tokens=max_tokens):
        if sec.est_tokens <= max_tokens:
            result.append(sec)
            continue
        chunks = _hard_split(sec.markdown.split("\n"), max_chars)
        if len(chunks) < 2:
            result.append(sec)
            continue
        base = sec.title or "Document"
        for n, chunk in enumerate(chunks, start=1):
            result.append(Section(f"{base} (part {n})", chunk + "\n"))
    return _pack(result, max_tokens) if pack else result


_PACK_TITLE_SEP = " – "


def _pack(sections: list[Section], max_tokens: int) -> list[Section]:
    """Greedily merge consecutive sections while the joined part stays within
    ``max_tokens``. Parts break only at section boundaries (so never inside
    a fence), and a section already over budget stands alone. A packed part
    is titled "First – Last" after the sections it spans."""
    def fits(group: list[Section]) -> bool:
        # The joined part's exact estimate (see estimate_tokens).
        joined = sum(len(s.markdown) for s in group) + len(group) - 1
        return joined // _CHARS_PER_TOKEN <= max_tokens

    groups: list[list[Section]] = []
    for sec in sections:
        if groups and fits(groups[-1] + [sec]):
            groups[-1].append(sec)
            continue
        # A heading-only section (a chapter opener, an empty glossary letter)
        # introduces what follows: carry it into the new part, if it fits,
        # rather than strand it at the end of this one.
        new = [sec]
        while groups and len(groups[-1]) > 1 and _is_divider(groups[-1][-1]):
            if not fits([groups[-1][-1]] + new):
                break
            new.insert(0, groups[-1].pop())
        groups.append(new)
    packed: list[Section] = []
    for group in groups:
        if len(group) == 1:
            packed.append(group[0])
            continue
        first, last = group[0].title, group[-1].title
        title = f"{first}{_PACK_TITLE_SEP}{last}" if first and last else first or last
        packed.append(Section(title, "\n".join(s.markdown for s in group)))
    return packed


def split_for_notebook(
    markdown: str,
    max_tokens: int = NOTEBOOK_SOURCE_TOKENS,
    max_sources: int = NOTEBOOK_MAX_SOURCES,
) -> list[Section]:
    """Split into Gemini Notebook sources: one per chapter where possible.

    Every part fits ``max_tokens`` with the guarantees of :func:`split_for_ai`
    (only an indivisible fenced block can exceed it). Chapters stay separate —
    each a source that can be selected on its own — unless that would take
    more than ``max_sources`` sources; then consecutive chapters are packed
    with the SMALLEST budget that brings the count within the limit, keeping
    parts as fine-grained and even as the limit allows. A document too big
    for that even at ``max_tokens`` takes as many sources as the cap needs.
    """
    sections = split_for_ai(markdown, max_tokens, pack=False)
    if len(sections) <= max_sources:
        return sections
    best = _pack(sections, max_tokens)
    if len(best) > max_sources:
        return best
    lo, hi = 1, max_tokens  # invariant: packing at hi fits (best is that packing)
    while lo < hi:
        mid = (lo + hi) // 2
        packed = _pack(sections, mid)
        if len(packed) <= max_sources:
            best, hi = packed, mid
        else:
            lo = mid + 1
    return best


def _is_lead(title: str) -> bool:
    """Is this part purely the text before the first chapter?"""
    return title == _LEAD_TITLE or title.startswith(f"{_LEAD_TITLE} (part ")


def _hard_split(lines: list[str], max_chars: int) -> list[str]:
    """Split lines into chunks of at most ``max_chars`` joined characters.

    The budget is checked BEFORE a boundary is passed, cutting at the last
    blank line outside a code fence within budget (or, for blank-line-free
    text, the last line boundary outside a fence), so a chunk only exceeds
    the budget when a single indivisible fenced block does — and then by the
    minimum possible amount. Unbalanced fences (a real OCR artifact) make
    fence state meaningless, so it is ignored rather than letting one stray
    marker disable splitting entirely. A cut never leaves a heading as a
    chunk's last line — it belongs with what it introduces (a heading over
    an indivisible listing travels with the listing).
    """
    is_fence, in_fence = _fence_map(lines)
    if sum(is_fence) % 2:
        in_fence = [False] * len(lines)
    cum = [0]
    for line in lines:
        cum.append(cum[-1] + len(line) + 1)
    n = len(lines)
    # after_heading[c]: the last non-blank line before index c is a heading.
    after_heading = [False] * (n + 1)
    last = False
    for i, line in enumerate(lines):
        if line.strip():
            last = not (in_fence[i] or is_fence[i]) and bool(_HEADING_LINE_RE.match(line))
        after_heading[i + 1] = last
    chunks: list[str] = []
    start = 0
    while start < n:
        if cum[n] - cum[start] <= max_chars:
            end = n
        else:
            best_blank = best_soft = None
            c = start + 1
            while c < n and cum[c] - cum[start] <= max_chars:
                if not in_fence[c] and not after_heading[c]:
                    best_soft = c
                    if not lines[c - 1].strip():
                        best_blank = c
                c += 1
            end = best_blank or best_soft
            if end is None:  # indivisible fenced block: minimal overflow
                while c < n and in_fence[c]:
                    c += 1
                end = c if c < n else n
        chunks.append("\n".join(lines[start:end]).strip())
        start = end
    return [chunk for chunk in chunks if chunk]


def document_title(markdown: str, fallback: str) -> str:
    for line in markdown.split("\n"):
        m = _H1_RE.match(line)
        if m:
            return m.group(1)
        if line.strip():
            break
    return fallback


_IMAGE_ALT_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_CODE_SPAN_RE = re.compile(r"(`+[^`]*`+)")


def _figure_note(m: re.Match[str]) -> str:
    alt = m.group(1).strip()
    if not alt:
        return "[Figure]"
    return f"[{alt}]" if alt.lower().startswith("figure") else f"[Figure: {alt}]"


def notebook_figures(markdown: str) -> str:
    """Reduce image links to a ``[Figure …]`` note for a notebook source.

    A notebook source is the uploaded .md alone: a relative image link can't
    resolve there and a remote one isn't fetched, so the link is noise while
    its alt text (the figure label, or an AI caption) is content. Code —
    fenced or inline — is untouched: a Markdown tutorial's ``![alt](url)`` is
    an example, not an image.
    """
    if "![" not in markdown:
        return markdown
    lines = markdown.split("\n")
    is_fence, in_fence = _fence_map(lines)
    for i, line in enumerate(lines):
        if is_fence[i] or in_fence[i] or "![" not in line:
            continue
        parts = _CODE_SPAN_RE.split(line)  # odd indices are code spans
        parts[::2] = [_IMAGE_ALT_RE.sub(_figure_note, p) for p in parts[::2]]
        lines[i] = "".join(parts)
    return "\n".join(lines)


def _notebook_header(
    book: str,
    author: str,
    *,
    part: str = "",
    summary: str = "",
    contents: list[str] | None = None,
) -> str:
    """A short plain-Markdown header for a notebook source.

    It replaces YAML front matter, which a notebook reads as body text: the
    machine fields are retrieval noise, and ``converted: <date>`` gets cited
    as the book's date. What a source needs is which book (and part) it is —
    a notebook often holds several. The first part also carries the summary
    and the list of parts, since there is no index.md to hold them.
    """
    book, author, summary = _one_line(book), _one_line(author), _one_line(summary)
    lines = [f"*{book}*" + (f" by {author}" if author else "") + (f" — {part}" if part else "")]
    if summary:
        lines += ["", f"Summary: {summary}"]
    if contents:
        lines += ["", f"This document is split into {len(contents)} sources:", ""]
        lines += [f"{n}. {_one_line(t)}" for n, t in enumerate(contents, start=1)]
    return "\n".join(lines) + "\n\n---\n\n"


_SUBTITLE_SEP_RE = re.compile(r"\s*[:–—]\s+|\s+-\s+")
_EDITION_RE = re.compile(
    r",?\s*\(?\b(?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|"
    r"seventh|eighth|ninth|tenth)\s+edition\b\)?",
    re.IGNORECASE,
)
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|an?)-(?=.)")


def _source_prefix(title: str, max_len: int = 40) -> str:
    """The book's short slug that starts each source's filename — a notebook
    lists every book's sources side by side by name, truncating long ones, so
    only the main title (no subtitle, edition or leading article) is used,
    cut at a word boundary. (Not much shorter: a series' titles share their
    first words — "Universal Principles of Branding" / "… of Design".)"""
    main = _SUBTITLE_SEP_RE.split(title, maxsplit=1)[0] or title
    main = _EDITION_RE.sub("", main).strip() or main
    main = re.sub(r"['’]", "", main)  # "Programmer's" -> programmers, not programmer-s
    slug = _LEADING_ARTICLE_RE.sub("", slugify(main, max_len=200))
    if len(slug) <= max_len:
        return slug
    cut = slug[: max_len + 1]
    return cut.rsplit("-", 1)[0] if "-" in cut else slug[:max_len]


def export_single(
    markdown: str,
    out_path: Path,
    *,
    source: str,
    engine: str = "",
    front_matter: bool = True,
    summary: str = "",
    title: str = "",
    author: str = "",
    notebook: bool = False,
) -> ExportResult:
    """Write one decorated Markdown file.

    ``summary`` (optional, from the local-AI pass) becomes a ``summary:``
    front-matter field; blank means the field is simply absent. ``title`` /
    ``author`` come from the source's own metadata when the converter could
    vouch for them; a blank title falls back to the document's first heading.
    ``notebook`` writes a Gemini Notebook source (see :func:`export_book`).
    """
    content = markdown
    if notebook:
        content = notebook_figures(markdown)
        if front_matter:
            book = title or document_title(markdown, Path(source).stem)
            content = _notebook_header(book, author, summary=summary) + content
    elif front_matter:
        content = build_front_matter(
            {
                "title": title or document_title(markdown, Path(source).stem),
                "author": author,
                "summary": summary,
                "source": source,
                "converted": date.today().isoformat(),
                "converter": "Markdown Sidekick" + (f" ({engine})" if engine else ""),
                "est_tokens": estimate_tokens(markdown),
                "image_count": count_images(markdown) or None,
            }
        ) + markdown
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    debuglog.event(
        "export.single",
        path=str(out_path),
        notebook=notebook,
        front_matter=front_matter,
        est_tokens=estimate_tokens(content),
        summary=bool(summary),
    )
    return ExportResult(paths=[out_path])


def export_book(
    markdown: str,
    out_dir: Path,
    *,
    source: str,
    engine: str = "",
    front_matter: bool = True,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    ai_sections: bool = False,
    summary: str = "",
    title: str = "",
    author: str = "",
    notebook: bool = False,
) -> ExportResult:
    """Write a book folder (split parts + index.md + manifest.json).

    ``ai_sections=True`` sizes every part to ``max_tokens`` even for
    heading-less documents (see :func:`split_for_ai`; only an indivisible
    fenced block can exceed the budget); otherwise splitting follows chapter
    structure only. Falls back to a single decorated file inside ``out_dir``
    when there is nothing to split.

    A document-level ``summary`` lands in index.md, manifest.json, and each
    part's front matter as ``book_summary`` (the parts describe the whole
    book, not themselves). Text before the first chapter heading is written
    as ``00-front-matter.md`` so chapter N lands in a file numbered N.

    ``notebook=True`` writes Gemini Notebook sources instead: parts from
    :func:`split_for_notebook` with ``max_tokens`` as the per-source cap,
    filenames prefixed with the book's slug, a readable header in place of
    YAML (summary and list of parts on the first part only), image links
    reduced to their captions, and no index.md or manifest.json — every file
    in the folder is a source to upload.
    """
    stem = Path(source).stem
    title = title or document_title(markdown, stem)
    if notebook:
        markdown = notebook_figures(markdown)
        sections = split_for_notebook(markdown, max_tokens)
    elif ai_sections:
        sections = split_for_ai(markdown, max_tokens)
    else:
        sections = split_chapters(markdown, max_tokens=max_tokens)
    out_dir.mkdir(parents=True, exist_ok=True)
    if len(sections) < 2:
        return export_single(
            markdown,
            out_dir / f"{stem}.md",
            source=source,
            engine=engine,
            front_matter=front_matter,
            summary=summary,
            title=title,
            author=author,
            notebook=notebook,
        )

    result = ExportResult()
    manifest_files = []
    total = len(sections)
    used_names: set[str] = set()
    # Number the lead front-matter file 00 so chapter numbers line up.
    # (Only a part that is purely front matter: a packed "Front matter –
    # Chapter 2" part is 01 like any other.)
    first = 0 if _is_lead(sections[0].title) else 1
    prefix = f"{_source_prefix(title)}-" if notebook else ""
    for n, sec in enumerate(sections, start=1):
        name = f"{prefix}{n - 1 + first:02d}-{slugify(sec.title or 'section')}"
        while name in used_names:  # duplicate section titles
            name += "-b"
        used_names.add(name)
        path = out_dir / f"{name}.md"
        images = count_images(sec.markdown)
        content = sec.markdown
        if notebook:
            if front_matter:
                part = f"part {n} of {total}" + (f": {sec.title}" if sec.title else "")
                content = _notebook_header(
                    title,
                    author,
                    part=part,
                    summary=summary if n == 1 else "",
                    contents=[s.title or _LEAD_TITLE for s in sections] if n == 1 else None,
                ) + sec.markdown
        elif front_matter:
            content = build_front_matter(
                {
                    "title": sec.title or title,
                    "book": title,
                    "author": author,
                    "book_summary": summary,
                    "part": f"{n} of {total}",
                    "source": source,
                    "converted": date.today().isoformat(),
                    "converter": "Markdown Sidekick" + (f" ({engine})" if engine else ""),
                    "est_tokens": sec.est_tokens,
                    "image_count": images or None,
                }
            ) + sec.markdown
        path.write_text(content, encoding="utf-8")
        result.paths.append(path)
        manifest_files.append(
            {
                "file": path.name,
                "title": sec.title,
                "est_tokens": sec.est_tokens,
                "image_count": images,
            }
        )
    debuglog.event(
        "export.book",
        dir=str(out_dir),
        mode="notebook" if notebook else "ai" if ai_sections else "chapters",
        max_tokens=max_tokens,
        parts=[[f["file"], f["est_tokens"]] for f in manifest_files],
        total_est_tokens=estimate_tokens(markdown),
        summary=bool(summary),
    )
    if notebook:
        return result  # every file is a source to upload: no index or manifest

    index_lines = [f"# {title}", ""]
    if author:
        index_lines += [f"*{author}*", ""]
    index_lines += [f"Converted from **{source}** — {total} parts.", ""]
    if summary:
        index_lines += [summary, ""]
    for entry in manifest_files:
        index_lines.append(f"- [{entry['title'] or 'Front matter'}]({entry['file']})")
    result.index_path = out_dir / "index.md"
    result.index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    manifest = {
        "title": title,
        "author": author,
        "summary": summary,
        "source": source,
        "engine": engine,
        "converted": date.today().isoformat(),
        "total_est_tokens": estimate_tokens(markdown),
        "total_images": count_images(markdown),
        "files": manifest_files,
    }
    result.manifest_path = out_dir / "manifest.json"
    result.manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result
