"""Tests for the export (splitting/front matter) and quality modules."""

from __future__ import annotations

import json
import random

import pytest

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

    def test_part_divider_pages_fold_into_the_next_chapter(self):
        md = (
            "# Introduction\n\nWhy grids.\n\n# GETTING STARTED\n\n<!-- page 9 -->\n\n"
            "# Elements of a Grid\n\nMargins and columns.\n\n# Gallery\n\n"
            "![Figure 30.1](images/fig_p30_1.jpg)\n\n# Index\n\nterms\n"
        )
        secs = split_chapters(md)
        # The empty divider joins the next chapter; a figures-only chapter
        # is content and stays on its own.
        assert [s.title for s in secs] == ["Introduction", "Elements of a Grid", "Gallery", "Index"]
        assert secs[1].markdown.startswith("# GETTING STARTED")
        assert "Margins and columns." in secs[1].markdown

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

        # Chapters that can't share a part keep chapter boundaries as breaks
        # (each ~20 tokens here); a roomier budget packs them.
        secs = split_for_ai(_BOOK, max_tokens=25)
        assert [s.title for s in secs] == [
            "Front matter", "Chapter 1: Getting Started", "Chapter 2: Going Deeper"
        ]
        secs = split_for_ai(_BOOK, max_tokens=30)
        assert [s.title for s in secs] == [
            "Front matter – Chapter 1: Getting Started", "Chapter 2: Going Deeper"
        ]

    def test_small_chapters_are_packed_up_to_the_budget(self):
        from markdown_sidekick.export import split_chapters, split_for_ai

        # 60 one-page "principles" (the UPoD shape): one file each was 134
        # parts at every budget. Packed, each part fills toward the budget.
        book = "".join(f"# Principle {i}\n\n" + ("word " * 180) + "\n\n" for i in range(60))
        secs = split_for_ai(book, max_tokens=2_000)
        assert 5 <= len(secs) < 60
        assert all(s.est_tokens <= 2_000 for s in secs)  # still a hard cap
        assert secs[0].title.startswith("Principle 0 – Principle ")
        # Parts break only at chapter boundaries, and nothing is lost.
        assert all(s.markdown.startswith("# Principle ") for s in secs)
        chapters = split_chapters(book, max_tokens=2_000)
        assert sum(s.markdown.count("# Principle ") for s in secs) == len(chapters) == 60

    def test_packing_never_splits_a_fence_or_joins_an_oversize_part(self):
        from markdown_sidekick.export import split_for_ai

        fenced = "```python\n" + ("x = 1\n" * 900) + "```\n"
        book = "# A\n\nshort\n\n# B\n\n" + fenced + "\n# C\n\nshort\n\n# D\n\nshort\n"
        secs = split_for_ai(book, max_tokens=500)
        titles = [s.title for s in secs]
        assert "B" in titles  # the indivisible listing stands alone
        assert all(s.markdown.count("```") % 2 == 0 for s in secs)
        assert titles[-1] == "C – D"

    def test_heading_only_section_travels_with_what_follows(self):
        from markdown_sidekick.export import Section, _pack

        body = "word " * 300 + "\n"  # ~375 tokens
        secs = [
            Section("One", "# One\n\n" + body),
            Section("Opener", "## Opener\n"),  # a chapter-opener page
            Section("Two", "# Two\n\n" + body),
        ]
        assert [s.title for s in _pack(secs, max_tokens=400)] == ["One", "Opener – Two"]

    def test_hard_split_never_ends_a_part_on_a_heading(self):
        from markdown_sidekick.export import split_for_ai

        text = ("prose " * 60 + "\n\n") * 3 + "## Next Topic\n\n" + ("prose " * 60 + "\n\n") * 3
        for s in split_for_ai(text, max_tokens=250):
            last = [l for l in s.markdown.strip().split("\n") if l.strip()][-1]
            assert not last.startswith("#")

    _HEADER = "| Part | Description | Price |"
    _RULE = "|---|:---|---:|"
    _ROWS = [f"| P-{i:05d} | Widget number {i} with an ordinary description | {i}.50 |" for i in range(2000)]

    def _table_doc(self) -> str:
        return (
            "# Parts catalogue\n\nIntro paragraph.\n\n"
            + "\n".join([self._HEADER, self._RULE, *self._ROWS])
            + "\n\nClosing paragraph.\n"
        )

    def test_long_table_repeats_its_header_in_every_part(self):
        from markdown_sidekick.export import split_for_ai

        for budget in (2_000, 12_000):
            secs = split_for_ai(self._table_doc(), max_tokens=budget)
            assert len(secs) > 1
            assert all(s.est_tokens <= budget for s in secs)  # the repeat is counted
            rows = []
            parts_with_rows = 0
            for s in secs:
                lines = s.markdown.splitlines()
                body = [ln for ln in lines if ln.startswith("| P-")]
                if not body:
                    continue
                parts_with_rows += 1
                first = lines.index(body[0])
                assert lines[first - 2 : first] == [self._HEADER, self._RULE]
                assert lines.count(self._HEADER) == 1
                rows += body
            assert parts_with_rows > 1
            assert rows == self._ROWS  # every row once, in order

    def test_text_around_a_long_table_gets_no_header(self):
        from markdown_sidekick.export import split_for_ai

        # Fits a part, but not with any of the table: a part starts on it.
        closing = "Closing words. " * 130
        text = "\n".join([self._HEADER, self._RULE, *self._ROWS[:200]]) + f"\n\n{closing}\n"
        secs = split_for_ai(text, max_tokens=500, pack=False)
        assert secs[-1].markdown.startswith("Closing words.")
        assert secs[0].markdown.startswith(self._HEADER)

    def test_table_without_outer_pipes_repeats_its_header_too(self):
        from markdown_sidekick.export import split_for_ai

        rows = [f"item {i} | value {i}" for i in range(400)]
        text = "\n".join(["Name | Value", "---|---", *rows]) + "\n"
        secs = split_for_ai(text, max_tokens=200)
        assert len(secs) > 1
        assert all(s.markdown.startswith("Name | Value\n---|---\n") for s in secs)
        assert all(s.est_tokens <= 200 for s in secs)

    def test_a_table_that_fits_stays_whole_after_prose(self):
        from markdown_sidekick.export import split_for_ai

        prose = "prose " * 100
        table = "\n".join([self._HEADER, self._RULE, *self._ROWS[:8]])
        secs = split_for_ai(f"{prose}\n\n{table}\n\n{prose}\n", max_tokens=250)
        holding = [s for s in secs if "| P-" in s.markdown]
        assert len(holding) == 1
        assert holding[0].markdown.count(self._HEADER) == 1

    def test_tables_in_code_and_pipes_in_prose_get_no_repeat(self):
        from markdown_sidekick.export import split_for_ai

        # Pipes with no rule row, and a thematic break under one, are not tables.
        lines = [f"line {i} has a | pipe in it and some more words" for i in range(200)]
        for text in ("\n".join(lines), "\n".join(["a | b", "---", *lines])):
            secs = split_for_ai(text + "\n", max_tokens=200)
            assert len(secs) > 1
            assert sum(s.markdown.count(lines[0]) for s in secs) == 1
            assert sum(s.markdown.count("a | b") for s in secs) <= 1
        # A table inside a fence is code; the fence is never split at all.
        fenced = "```\n| a | b |\n|---|---|\n" + "\n".join(f"| {i} | x |" for i in range(300)) + "\n```\n"
        secs = split_for_ai("intro\n\n" + fenced + "\noutro\n", max_tokens=100)
        assert sum(s.markdown.count("| a | b |") for s in secs) == 1

    def test_a_header_too_big_to_repeat_keeps_the_budget(self):
        from markdown_sidekick.export import split_for_ai

        header = "| " + " | ".join(f"column {i}" for i in range(60)) + " |"
        rule = "|" + "---|" * 60
        rows = [f"| {'row %d ' % i * 8}|" for i in range(30)]
        text = "\n".join([header, rule, *rows]) + "\n"
        budget = (len(header) + len(rule) + len(rows[0])) // 4 - 5  # no room for all three
        secs = split_for_ai(text, max_tokens=budget)
        assert all(s.est_tokens <= budget for s in secs)
        assert "".join(s.markdown for s in secs).count("row 29") == 8

    def test_whole_book_within_budget_is_one_file(self, tmp_path):
        from markdown_sidekick.export import export_book

        res = export_book(_BOOK, tmp_path / "b", source="b.pdf", ai_sections=True)
        assert [p.name for p in res.paths] == ["b.md"]

    def test_packed_front_matter_part_is_not_numbered_00(self, tmp_path):
        from markdown_sidekick.export import export_book

        book = "Preface text.\n\n" + "".join(
            f"# Chapter {i}\n\n" + ("word " * 300) + "\n\n" for i in range(1, 7)
        )
        res = export_book(book, tmp_path / "b", source="b.pdf", ai_sections=True, max_tokens=900)
        names = [p.name for p in res.paths]
        assert names[0].startswith("01-front-matter-chapter-")
        assert not any(n.startswith("00-") for n in names)

    def test_ai_targets_defined(self):
        from markdown_sidekick.export import AI_TARGETS

        assert set(AI_TARGETS) >= {"Claude", "ChatGPT", "Gemini", "Gemini Notebook", "Local LLM"}
        assert all(v > 0 for v in AI_TARGETS.values())


class TestGeminiNotebook:
    def test_target_is_registered_within_the_source_word_limit(self):
        from markdown_sidekick import export

        assert export.NOTEBOOK_TARGETS <= set(export.AI_TARGETS)
        # A word is at least one character: a part of <= 480k characters can
        # never pass the 500,000-words-per-source limit, whatever the script,
        # and the remaining 20k characters absorb the source header.
        assert export.AI_TARGETS["Gemini Notebook"] * 4 <= 480_000

    def test_one_source_per_chapter(self):
        from markdown_sidekick.export import split_for_notebook

        # Chapters that the chat targets would pack together stay separate:
        # each is a source the notebook can be scoped to on its own.
        secs = split_for_notebook(_BOOK)
        assert [s.title for s in secs] == [
            "Front matter", "Chapter 1: Getting Started", "Chapter 2: Going Deeper"
        ]

    def test_packs_only_as_much_as_the_source_limit_needs(self):
        from markdown_sidekick.export import NOTEBOOK_MAX_SOURCES, split_for_notebook

        book = "".join(f"# Principle {i}\n\n" + ("word " * 180) + "\n\n" for i in range(60))
        secs = split_for_notebook(book)
        # Within the limit, but not collapsed into a handful of huge parts.
        assert NOTEBOOK_MAX_SOURCES // 2 < len(secs) <= NOTEBOOK_MAX_SOURCES
        sizes = [s.markdown.count("# Principle ") for s in secs]
        assert max(sizes) - min(sizes) <= 1  # evenly packed
        assert sum(sizes) == 60 and all(s.markdown.startswith("# Principle ") for s in secs)

    def test_parts_respect_the_per_source_cap(self):
        from markdown_sidekick.export import split_for_notebook

        text = "\n\n".join("Paragraph %d. %s" % (i, "word " * 200) for i in range(40))
        secs = split_for_notebook(text, max_tokens=2000)
        assert len(secs) > 1
        assert all(s.est_tokens <= 2000 for s in secs)
        # Too big for the source limit even at the cap: the cap wins.
        secs = split_for_notebook(text, max_tokens=2000, max_sources=2)
        assert len(secs) > 2 and all(s.est_tokens <= 2000 for s in secs)

    def test_export_writes_upload_ready_sources(self, tmp_path):
        from markdown_sidekick.export import export_book

        md = _BOOK.replace("More prose.", "More prose.\n\n![Figure 2.1](images/fig_p2_1.jpg)")
        res = export_book(
            md, tmp_path / "b", source="book.pdf", notebook=True,
            title="The Grid Book: A Subtitle", author="Ann Author", summary="About grids.",
        )
        # Only sources in the folder, named after the book (a notebook lists
        # every book's sources side by side).
        assert res.index_path is None and res.manifest_path is None
        assert sorted(p.name for p in (tmp_path / "b").iterdir()) == [
            "grid-book-00-front-matter.md",
            "grid-book-01-chapter-1-getting-started.md",
            "grid-book-02-chapter-2-going-deeper.md",
        ]
        first, second, third = (p.read_text(encoding="utf-8") for p in res.paths)
        # A readable header, not YAML (a "converted:" date would be cited as
        # the book's date); summary and part list only on the first source.
        assert not first.startswith("---")
        assert first.startswith("*The Grid Book: A Subtitle* by Ann Author — part 1 of 3")
        assert "Summary: About grids." in first
        assert "2. Chapter 1: Getting Started" in first
        assert second.startswith(
            "*The Grid Book: A Subtitle* by Ann Author — part 2 of 3: Chapter 1: Getting Started"
        )
        assert "Summary:" not in second and "converted:" not in second
        assert "# not a heading\nx = 1" in second  # fences intact
        # Image links can't resolve in a notebook: keep the label as text.
        assert "[Figure 2.1]" in third and "](images/" not in third

    def test_front_matter_off_writes_bare_content(self, tmp_path):
        from markdown_sidekick.export import export_book

        res = export_book(_BOOK, tmp_path / "b", source="b.pdf", notebook=True, front_matter=False)
        assert res.paths[1].read_text(encoding="utf-8").startswith("# Chapter 1")

    def test_unsplit_document_is_one_source_with_header(self, tmp_path):
        from markdown_sidekick.export import export_book

        res = export_book("# Memo\n\nShort body.\n", tmp_path / "b", source="memo.docx", notebook=True)
        assert [p.name for p in res.paths] == ["memo.md"]
        text = res.paths[0].read_text(encoding="utf-8")
        assert text.startswith("*Memo*\n\n---\n\n# Memo")

    def test_figure_notes_leave_code_alone(self):
        from markdown_sidekick.export import notebook_figures

        md = (
            "![Figure 3.1](images/fig_p3_1.jpg)\n\n![A bar chart of sales](x.png)\n\n"
            "![](y.png) and `![inline](code.png)` here\n\n```md\n![example](z.png)\n```\n"
        )
        assert notebook_figures(md) == (
            "[Figure 3.1]\n\n[Figure: A bar chart of sales]\n\n"
            "[Figure] and `![inline](code.png)` here\n\n```md\n![example](z.png)\n```\n"
        )

    def test_source_prefix_is_the_short_main_title(self):
        from markdown_sidekick.export import _source_prefix

        assert _source_prefix("The Pragmatic Programmer: Your Journey to Mastery") == "pragmatic-programmer"
        assert _source_prefix("Clean Code - A Handbook") == "clean-code"
        assert _source_prefix("Clean Code in Python, Second Edition") == "clean-code-in-python"
        assert _source_prefix("Microservices with Go, 2nd Edition") == "microservices-with-go"
        assert _source_prefix("An Extraordinarily Long Title About Many Different Things") == (
            "extraordinarily-long-title-about-many"
        )
        # A series stays distinguishable: its titles share the first words.
        assert _source_prefix("Universal Principles of Branding") == "universal-principles-of-branding"
        assert _source_prefix("The C++ Programmer's Mindset") == "c-programmers-mindset"
        assert _source_prefix("The") == "the"


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
        # Front matter is 00, so chapter N lands in the file numbered N.
        assert [p.name for p in res.paths] == [
            "00-front-matter.md",
            "01-chapter-1-getting-started.md",
            "02-chapter-2-going-deeper.md",
        ]
        part = res.paths[1].read_text(encoding="utf-8")
        assert "book:" in part and "part: 2 of 3" in part
        index = res.index_path.read_text(encoding="utf-8")
        assert "[Chapter 1: Getting Started](01-chapter-1-getting-started.md)" in index
        manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
        assert manifest["source"] == "book.pdf"
        assert len(manifest["files"]) == 3

    def test_book_without_lead_text_starts_at_01(self, tmp_path):
        res = export_book("# A\n\na\n\n# B\n\nb\n", tmp_path / "b", source="s.pdf")
        assert [p.name for p in res.paths] == ["01-a.md", "02-b.md"]

    def test_pdf_title_and_author_win_over_guessing(self, tmp_path):
        res = export_book(
            _BOOK, tmp_path / "book", source="makingandbreakingthegrid.pdf",
            title="Making and Breaking the Grid", author="Timothy Samara",
        )
        part = res.paths[1].read_text(encoding="utf-8")
        assert "book: Making and Breaking the Grid" in part
        assert "author: Timothy Samara" in part
        manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
        assert (manifest["title"], manifest["author"]) == (
            "Making and Breaking the Grid", "Timothy Samara"
        )
        assert "*Timothy Samara*" in res.index_path.read_text(encoding="utf-8")
        out = tmp_path / "one.md"
        export_single("plain body\n", out, source="x.pdf", title="Real Title")
        assert "title: Real Title" in out.read_text(encoding="utf-8")

    def test_front_matter_values_are_yaml_safe(self):
        fm = build_front_matter({"title": "Bad\x02 Title\x1a\n  here", "empty": "\x07"})
        assert fm == "---\ntitle: Bad Title here\n---\n\n"

    def test_image_counts_in_front_matter_and_manifest(self, tmp_path):
        md = (
            "# Chapter 1: Pictures\n\n![Figure 1.1](images/fig_p1_1.jpg)\n\n"
            "![Figure 2.1](images/fig_p2_1.png)\n\n# Chapter 2: Words\n\nNo images.\n"
        )
        res = export_book(md, tmp_path / "b", source="s.pdf")
        first = res.paths[0].read_text(encoding="utf-8")
        assert "image_count: 2" in first
        assert "image_count" not in res.paths[1].read_text(encoding="utf-8")
        manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
        assert manifest["total_images"] == 2
        assert [f["image_count"] for f in manifest["files"]] == [2, 0]

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

    def test_prepress_and_shadow_residue_flagged(self):
        doc = (
            "# T\n\n700065 - Grid_001-077.indd 1 3/23/17 5:23 PM\n"
            "Job No: 05-30592 Title: RP-Graphic Design\n"
            "DDrraawwiinngg CCoommiiccss LLaabb\n\nThe bookkeeper agreed.\n"
        )
        r = assess_markdown(doc)
        assert r.prepress_residue == 2
        assert r.doubled_words == 3
        assert any("prepress" in i for i in r.issues)
        assert any("doubled" in i for i in r.issues)
        assert r.as_dict()["prepress_residue"] == 2

    def test_genuinely_doubled_text_is_not_shadow(self):
        # Corpus finding: a placeholder "IIIIIIII" cost a book its score;
        # hex colours are doubled on purpose too.
        r = assess_markdown("# T\n\nSwatch FFEEDD beside IIIIIIII and AATTCCGG.\n")
        assert r.doubled_words == 0


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

    def test_salad_with_no_spaces_flagged(self):
        # markitdown's lossy decode of bytes that are all 0x80 or above: too few
        # words for the other signals, so changing script is the only tell.
        rng = random.Random(42)
        raw = bytes(rng.randrange(0x80, 0x100) for _ in range(16_384))
        text = raw.decode("utf-8", errors="ignore")
        assert len(text.split()) < 40
        r = assess_markdown(text)
        assert r.binary_noise
        assert r.script_switch >= 0.5
        assert r.as_dict()["script_switch"] == round(r.script_switch, 3)

    @pytest.mark.parametrize(
        "text",
        [
            "Python语言是一种广泛使用的API接口编程语言，它的HTTP库非常好用。使用JSON格式传输数据。",
            "これはテストの文章です。カタカナとひらがな、そして漢字を混ぜて書きます。データベースをPythonで操作する。",
            # Latin terms and a Greek letter glued to the words around them. A
            # rule counting words that mix scripts refused all of these as corrupt.
            "Pythonでデータベースを操作する。HTTPリクエストはJSON形式で送信されます。\n応答時間は100μs以下です。\n",
            "使用Python语言编写API接口。\n通过HTTP协议传输JSON数据。\n响应时间小于100μs。\n",
            "대한민국(大韓民國)에서 Python을 배운다. HTTP와 JSON을 쓴다. α값은 작다. ",
            "Δx ℝn 𝑓x ωt λ𝑥 μF ",
            "프로그래밍 언어 Python을 사용하여 데이터를 처리한다. HTTP 요청은 JSON 형식으로 전송된다. ",
            "यह हिंदी भाषा में एक नमूना पाठ है जो डिटेक्टर की जाँच करता है। ",
            "نص تجريبي باللغة العربية. يستخدم البرنامج ملف PDF لتحويل النص. ",
            "ðə kwɪk braʊn fɒks dʒʌmps ˈoʊvər ðə ˈleɪzi dɔɡ ",
            # A multi-language manual: many scripts, but each word stays in one.
            "Press the button. Нажмите кнопку. Πατήστε το κουμπί. اضغط على الزر. 電源を押してください。 שלום\n",
            # The same with no spaces at all: one long "word", still few switches.
            "PressthebuttonНажмитекнопкуΠατήστετοκουμπίاضغطعلىالزر電源を押してくださいשלום한국어",
            # Kanji and kana alternate constantly with no spaces, so they must
            # count as one script, or a few foreign terms would look like salad.
            "日本語の文章を読む。データとαとЖとאを書く。私の本を見る。",
            # In-word switching at its most extreme, but between only two scripts.
            "Δx Δy Δt ωt kΩ μF μm nΩ dθ dφ sinθ cosθ tanφ eλ eπ λx ",
            "Pаypаl ассount vеrify сliсk hеrе now frее оffеr ",
            "ℝn ℓp 𝑓x 𝐀b ﬁx ",
        ],
    )
    def test_real_multiscript_writing_not_flagged(self, text):
        r = assess_markdown(text * 200)
        assert not r.binary_noise

    def test_too_few_letters_to_judge_by_switches(self):
        # A page of numbers with one odd word: half its letter pairs change
        # script, but five changes prove nothing.
        r = assess_markdown("1 2 3 4 " * 60 + "an " * 5 + "aБγאدあ")
        assert r.script_switch == 0.5
        assert not r.binary_noise


class TestSummaryInExport:
    def test_single_file_gets_summary_field(self, tmp_path):
        out = tmp_path / "doc.md"
        export_single("# T\n\nbody\n", out, source="doc.pdf", summary="What it is: a test.")
        text = out.read_text(encoding="utf-8")
        assert 'summary: "What it is: a test."' in text  # colon forces quoting
        assert text.index("\nsummary:") < text.index("\nsource:")

    def test_blank_summary_leaves_no_field(self, tmp_path):
        out = tmp_path / "doc.md"
        export_single("# T\n\nbody\n", out, source="doc.pdf", summary="")
        assert "summary" not in out.read_text(encoding="utf-8")

    def test_book_carries_summary_in_parts_index_and_manifest(self, tmp_path):
        res = export_book(_BOOK, tmp_path / "book", source="book.pdf", summary="A book about things.")
        part = res.paths[1].read_text(encoding="utf-8")
        assert "book_summary: A book about things." in part
        assert "\nsummary:" not in part  # parts describe the book, not themselves
        index = res.index_path.read_text(encoding="utf-8")
        assert "\nA book about things.\n" in index
        manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
        assert manifest["summary"] == "A book about things."
