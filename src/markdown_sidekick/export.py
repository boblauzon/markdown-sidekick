"""AI-friendly export: front matter, chapter splitting, index and manifest.

Large single-file conversions are the worst shape for AI tools — they overflow
context windows and defeat retrieval chunkers. This module turns a converted
document into either a decorated single file or a "book folder":

    <stem>/
      index.md            table of contents linking the parts
      manifest.json       machine-readable map (titles, token estimates)
      01-chapter-name.md  one file per top-level heading, each with YAML
      02-...              front matter identifying the book and part

Splitting happens on ``#`` headings (which the cleanup pipeline restores for
book PDFs); a chapter that still exceeds the token budget is sub-split at its
``##`` boundaries. All writes are UTF-8.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_CHARS_PER_TOKEN = 4
DEFAULT_MAX_TOKENS = 30_000

# Per-platform section budgets (est. tokens). Sized so several sections fit
# in the platform's context window with room for the conversation itself.
AI_TARGETS: dict[str, int] = {
    "Claude": 30_000,
    "ChatGPT": 12_000,
    "Gemini": 60_000,
    "Local LLM": 4_000,
    "General": 24_000,
}

_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*```")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def slugify(title: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "section"


def build_front_matter(fields: dict[str, object]) -> str:
    """Minimal YAML front matter. Values are scalars; strings are quoted only
    when they contain YAML-significant characters."""
    lines = ["---"]
    for key, value in fields.items():
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


def _heading_lines(lines: list[str], pattern: re.Pattern[str]) -> list[int]:
    """Indices of heading lines, ignoring anything inside code fences."""
    indices: list[int] = []
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and pattern.match(line):
            indices.append(i)
    return indices


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
    sections = _split_at(lines, h1s, titles, "Front matter")

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


def split_for_ai(markdown: str, max_tokens: int) -> list[Section]:
    """Split into sections that each fit an AI platform's token budget.

    Chapter structure is used when present (via :func:`split_chapters`, which
    already sub-splits oversized chapters at ``##``). Anything still over
    budget — including documents with no headings at all — is hard-split at
    blank-line boundaries (never inside a code fence) into "(part N)" pieces,
    so the guarantee holds for every input.
    """
    max_chars = max_tokens * _CHARS_PER_TOKEN
    result: list[Section] = []
    for sec in split_chapters(markdown, max_tokens=max_tokens):
        if sec.est_tokens <= max_tokens:
            result.append(sec)
            continue
        pieces: list[list[str]] = [[]]
        size = 0
        in_fence = False
        for line in sec.markdown.split("\n"):
            if _FENCE_RE.match(line):
                in_fence = not in_fence
            pieces[-1].append(line)
            size += len(line) + 1
            if size >= max_chars and not in_fence and not line.strip():
                pieces.append([])
                size = 0
        chunks = ["\n".join(p).strip() for p in pieces]
        chunks = [c for c in chunks if c]
        base = sec.title or "Document"
        if len(chunks) == 1:
            result.append(sec)
            continue
        for n, chunk in enumerate(chunks, start=1):
            result.append(Section(f"{base} (part {n})", chunk + "\n"))
    return result


def document_title(markdown: str, fallback: str) -> str:
    for line in markdown.split("\n"):
        m = _H1_RE.match(line)
        if m:
            return m.group(1)
        if line.strip():
            break
    return fallback


def export_single(
    markdown: str,
    out_path: Path,
    *,
    source: str,
    engine: str = "",
    front_matter: bool = True,
) -> ExportResult:
    """Write one decorated Markdown file."""
    content = markdown
    if front_matter:
        content = build_front_matter(
            {
                "title": document_title(markdown, Path(source).stem),
                "source": source,
                "converted": date.today().isoformat(),
                "converter": "Markdown Sidekick" + (f" ({engine})" if engine else ""),
                "est_tokens": estimate_tokens(markdown),
            }
        ) + markdown
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
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
) -> ExportResult:
    """Write a book folder (split parts + index.md + manifest.json).

    ``ai_sections=True`` guarantees every part fits ``max_tokens`` even for
    heading-less documents (see :func:`split_for_ai`); otherwise splitting
    follows chapter structure only. Falls back to a single decorated file
    inside ``out_dir`` when there is nothing to split.
    """
    stem = Path(source).stem
    title = document_title(markdown, stem)
    if ai_sections:
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
        )

    result = ExportResult()
    manifest_files = []
    total = len(sections)
    used_names: set[str] = set()
    for n, sec in enumerate(sections, start=1):
        name = f"{n:02d}-{slugify(sec.title or 'section')}"
        while name in used_names:  # duplicate section titles
            name += "-b"
        used_names.add(name)
        path = out_dir / f"{name}.md"
        content = sec.markdown
        if front_matter:
            content = build_front_matter(
                {
                    "title": sec.title or title,
                    "book": title,
                    "part": f"{n} of {total}",
                    "source": source,
                    "converted": date.today().isoformat(),
                    "converter": "Markdown Sidekick" + (f" ({engine})" if engine else ""),
                    "est_tokens": sec.est_tokens,
                }
            ) + sec.markdown
        path.write_text(content, encoding="utf-8")
        result.paths.append(path)
        manifest_files.append(
            {"file": path.name, "title": sec.title, "est_tokens": sec.est_tokens}
        )

    index_lines = [f"# {title}", "", f"Converted from **{source}** — {total} parts.", ""]
    for entry in manifest_files:
        index_lines.append(f"- [{entry['title'] or 'Front matter'}]({entry['file']})")
    result.index_path = out_dir / "index.md"
    result.index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    manifest = {
        "title": title,
        "source": source,
        "engine": engine,
        "converted": date.today().isoformat(),
        "total_est_tokens": estimate_tokens(markdown),
        "files": manifest_files,
    }
    result.manifest_path = out_dir / "manifest.json"
    result.manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result
