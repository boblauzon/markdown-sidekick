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


class TestSplitForAi:
    def test_headingless_document_still_splits_to_budget(self):
        from markdown_sidekick.export import split_for_ai

        text = "\n\n".join("Paragraph %d. %s" % (i, "word " * 200) for i in range(40))
        secs = split_for_ai(text, max_tokens=2000)
        assert len(secs) > 1
        assert all(s.est_tokens <= 2000 for s in secs)  # the budget is a hard cap
        assert secs[0].title.endswith("(part 1)")
        # Nothing lost: total content round-trips (modulo whitespace).
        joined = "".join(s.markdown for s in secs)
        assert "Paragraph 39" in joined and "Paragraph 0" in joined

    def test_no_blank_lines_still_splits_to_budget(self):
        from markdown_sidekick.export import split_for_ai

        # One giant hard-wrapped paragraph (e.g. a whisper transcript): no
        # blank lines anywhere, so splitting falls back to line boundaries.
        text = "\n".join("line %d %s" % (i, "word " * 30) for i in range(400))
        secs = split_for_ai(text, max_tokens=1000)
        assert len(secs) > 1
        assert all(s.est_tokens <= 1000 for s in secs)

    def test_unbalanced_fence_does_not_disable_splitting(self):
        from markdown_sidekick.export import split_for_ai

        # A stray unclosed ``` (a real OCR artifact) must not pin the fence
        # state and swallow the whole document into one over-budget part.
        text = "```\n" + "\n\n".join(("word " * 200) for _ in range(40))
        secs = split_for_ai(text, max_tokens=2000)
        assert len(secs) > 1
        assert all(s.est_tokens <= 2000 for s in secs)

    def test_never_splits_inside_fence(self):
        from markdown_sidekick.export import split_for_ai

        fenced = "```python\n" + ("x = 1\n" * 300) + "```\n"
        text = ("prose\n\n" * 5) + fenced + ("after\n\n" * 5)
        secs = split_for_ai(text, max_tokens=200)
        for s in secs:
            assert s.markdown.count("```") % 2 == 0

    def test_chaptered_book_uses_chapters(self):
        from markdown_sidekick.export import split_for_ai

        secs = split_for_ai(_BOOK, max_tokens=30_000)
        assert [s.title for s in secs][:2] == ["Front matter", "Chapter 1: Getting Started"]

    def test_ai_targets_defined(self):
        from markdown_sidekick.export import AI_TARGETS

        assert set(AI_TARGETS) >= {"Claude", "ChatGPT", "Gemini", "Local LLM"}
        assert all(v > 0 for v in AI_TARGETS.values())


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


class TestBinaryNoise:
    """markitdown converts unrecognized binary garbage 'ok' — the assessor
    must flag the salad, without ever flagging real (even messy) content."""

    def test_byte_salad_flagged(self):
        # Deterministic stand-in for garbage bytes decoded as latin-1:
        # every codepoint 0-255, including the C0/C1 control ranges.
        salad = ("".join(chr(i) for i in range(256)) + " ") * 30
        r = assess_markdown(salad)
        assert r.binary_noise
        assert r.noise_ratio >= 0.10
        assert r.score <= 20
        assert any("binary noise" in i for i in r.issues)
        assert r.as_dict()["binary_noise"] is True

    def test_replacement_char_flood_flagged(self):
        # UTF-8 decode-with-replace of random bytes: FFFD everywhere.
        r = assess_markdown("x��z " * 300)
        assert r.binary_noise

    def test_symbol_salad_without_controls_flagged(self):
        # Controls stripped, but tokens are still non-word salad (mixed
        # accented letters and non-ASCII symbols, no real words).
        r = assess_markdown("ÅÙ¬ø∂¤¦¨ " * 100)
        assert r.binary_noise

    def test_prose_and_code_not_flagged(self):
        doc = (
            "# Title\n\nNormal prose with punctuation, e.g. costs of $5-10!\n\n"
            "```python\nif x <= 3 and y != {}:\n    print(f'{x:>4}|{y}')\n```\n\n"
        ) * 20
        r = assess_markdown(doc)
        assert not r.binary_noise
        assert r.word_ratio > 0.9

    def test_cjk_not_flagged(self):
        r = assess_markdown("# 標題\n\n" + "這是一段中文內容。 " * 100)
        assert not r.binary_noise

    def test_short_snippet_never_flagged(self):
        # Below the size guard even 100% noise stays unflagged — a tiny
        # output is not evidence of a corrupt source.
        r = assess_markdown("��� ��")
        assert not r.binary_noise

    def test_sparse_fffd_in_real_doc_not_flagged(self):
        # A handful of lost characters in an otherwise-real document (the
        # artifact cleanup targets) must not read as binary garbage.
        doc = ("A real paragraph of readable text. " * 50) + "wor�d s�pots\n"
        r = assess_markdown(doc)
        assert not r.binary_noise
