"""Tests for the export (splitting/front matter) and quality modules."""

from __future__ import annotations

import json

from markdown_sidekick.export import (
    build_front_matter,
    document_title,
    export_book,
    export_single,
    slugify,
    split_chapters,
)
from markdown_sidekick.quality import assess_markdown

_BOOK = (
    "Preamble before any chapter.\n\n"
    "# Chapter 1: Getting Started\n\nIntro prose.\n\n"
    "```python\n# not a heading\nx = 1\n```\n\n"
    "# Chapter 2: Going Deeper\n\nMore prose.\n\n"
    "## Section A\n\ntext a\n\n## Section B\n\ntext b\n"
)


class TestSplit:
    def test_splits_on_h1_ignoring_fences(self):
        secs = split_chapters(_BOOK)
        assert [s.title for s in secs] == [
            "Front matter",
            "Chapter 1: Getting Started",
            "Chapter 2: Going Deeper",
        ]

    def test_single_heading_not_split(self):
        secs = split_chapters("# Only One\n\nbody\n")
        assert len(secs) == 1 and secs[0].title == ""

    def test_oversize_chapter_subsplit_at_h2(self):
        big = "# A\n\nsmall\n\n# B\n\n" + "\n\n".join(
            f"## Part {i}\n\n" + ("word " * 3000) for i in range(3)
        )
        secs = split_chapters(big, max_tokens=2000)
        titles = [s.title for s in secs]
        assert "A" in titles
        assert any(t.startswith("B — Part 0") for t in titles)
        assert any(t.startswith("B — Part 2") for t in titles)

    def test_slugify(self):
        assert slugify("Chapter 2: SOLID & Friends!") == "chapter-2-solid-friends"

    def test_document_title(self):
        assert document_title("# The Title\n\nbody", "fb") == "The Title"
        assert document_title("plain first line\n# Later", "fb") == "fb"


class TestFrontMatter:
    def test_quoting(self):
        fm = build_front_matter({"title": "A: B", "n": 3, "skip": ""})
        assert 'title: "A: B"' in fm
        assert "n: 3" in fm
        assert "skip" not in fm
        assert fm.startswith("---\n") and fm.rstrip().endswith("---")


class TestExport:
    def test_export_single_with_front_matter(self, tmp_path):
        out = tmp_path / "doc.md"
        export_single("# T\n\nbody\n", out, source="doc.pdf", engine="markitdown")
        text = out.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "source: doc.pdf" in text
        assert text.endswith("body\n")

    def test_export_book_writes_parts_index_manifest(self, tmp_path):
        res = export_book(_BOOK, tmp_path / "book", source="book.pdf", engine="ocr")
        assert len(res.paths) == 3
        assert res.paths[1].name == "02-chapter-1-getting-started.md"
        part = res.paths[1].read_text(encoding="utf-8")
        assert "book:" in part and "part:" in part
        index = res.index_path.read_text(encoding="utf-8")
        assert "[Chapter 1: Getting Started](02-chapter-1-getting-started.md)" in index
        manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
        assert manifest["source"] == "book.pdf"
        assert len(manifest["files"]) == 3

    def test_export_book_without_chapters_falls_back_to_single(self, tmp_path):
        res = export_book("no headings here\n", tmp_path / "b", source="x.pdf")
        assert len(res.paths) == 1
        assert res.index_path is None

    def test_duplicate_titles_get_unique_names(self, tmp_path):
        md = "# Same\n\na\n\n# Same\n\nb\n"
        res = export_book(md, tmp_path / "d", source="s.pdf", front_matter=False)
        names = [p.name for p in res.paths]
        assert len(names) == len(set(names))


class TestQuality:
    def test_clean_document_scores_high(self):
        r = assess_markdown("# T\n\nGood prose.\n\n```python\nx = 1\n```\n")
        assert r.score >= 95
        assert r.fence_parity_ok
        assert r.fenced_blocks == 1

    def test_artifacts_lower_score(self):
        bad = ("Intro • 5\n" * 30) + ("•\n" * 20) + "word �� word\n" + "x" * 30000
        r = assess_markdown(bad)
        assert r.score < 70
        assert r.toc_residue >= 30
        assert any("TOC" in i for i in r.issues)
        assert any("no headings" in i for i in r.issues)

    def test_unbalanced_fences_detected(self):
        r = assess_markdown("```python\nx = 1\n")
        assert not r.fence_parity_ok

    def test_empty(self):
        r = assess_markdown("")
        assert r.score == 0

    def test_summary_and_dict(self):
        r = assess_markdown("# T\n\nbody\n")
        assert "Quality" in r.summary()
        assert r.as_dict()["headings"] == 1
