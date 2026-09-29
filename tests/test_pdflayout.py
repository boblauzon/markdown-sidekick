"""Column-aware PDF extraction tests (pdflayout) on generated PDFs.

Each fixture draws its text in a content-stream order that differs from the
reading order where that matters, so the tests prove the engine reads the
page GEOMETRY rather than trusting the stream.
"""

from __future__ import annotations

import re

import pytest

from markdown_sidekick import pdflayout
from markdown_sidekick.converter import ConversionEngine

from pdfgen import Image, Text, make_layout_pdf

pytestmark = pytest.mark.skipif(
    not pdflayout.layout_available(), reason="pypdfium2 not available"
)


def _extract(tmp_path, pages, name="doc.pdf", **kwargs):
    pdf_kwargs = {k: kwargs.pop(k) for k in ("trimbox", "outline", "title") if k in kwargs}
    path = tmp_path / name
    path.write_bytes(make_layout_pdf(pages, **pdf_kwargs))
    return pdflayout.extract_markdown(path, **kwargs)


def _two_columns(prefix_left="Left", prefix_right="Right"):
    """Four lines per column, drawn row-interleaved (L1 R1 L2 R2 ...)."""
    words = ["one", "two", "three", "four"]
    items = []
    for n, w in enumerate(words):
        y = 700 - 11 * n
        items.append(Text(f"{prefix_left} column sentence {w} of the story", 72, y))
        items.append(Text(f"{prefix_right} column sentence {w} of the tale", 330, y))
    return items


class TestReadingOrder:
    def test_columns_read_top_to_bottom_not_across(self, tmp_path):
        md = _extract(tmp_path, [_two_columns()]).markdown
        assert md.index("Left column sentence four") < md.index("Right column sentence one")

    def test_multi_column_prose_is_never_a_table(self, tmp_path):
        md = _extract(tmp_path, [_two_columns()]).markdown
        assert "|" not in md

    def test_margin_notes_follow_the_body(self, tmp_path):
        # A narrow notes column beside the body, notes drawn FIRST in the stream.
        body = [Text(f"Body paragraph line {w} continues the argument here", 72, 700 - 12 * i)
                for i, w in enumerate(["alpha", "beta", "gamma", "delta", "epsilon"])]
        notes = [Text("1 A margin note", 420, 700), Text("about the source.", 420, 689)]
        md = _extract(tmp_path, [notes + body]).markdown
        assert md.index("epsilon") < md.index("A margin note")
        # ... and never spliced into the middle of a body sentence.
        assert "note" not in md[: md.index("epsilon")]

    def test_key_value_rows_read_across(self, tmp_path):
        rows = [("Alpha", "The first letter of the Greek alphabet."),
                ("Beta", "The second letter, used for test versions."),
                ("Gamma", "The third letter, and a radiation type.")]
        items = []
        for n, (term, definition) in enumerate(rows):
            y = 700 - 30 * n
            items.append(Text(definition, 200, y))  # definitions drawn first
            items.append(Text(term, 72, y))
        md = _extract(tmp_path, [items]).markdown
        order = [md.index(s) for s in ("Alpha", "first letter", "Beta", "second letter", "Gamma")]
        assert order == sorted(order)


class TestPrepressAndShadow:
    def test_trimbox_clips_slug_text(self, tmp_path):
        page = [
            Text("700065 - Book_001-077.indd 1 3/23/17 5:23 PM", 40, 12, size=7),
            Text("Job:03-700065 Title:RP - Some Book", 40, 780, size=7),
            Text("The real body text of the page.", 72, 600),
        ]
        res = _extract(tmp_path, [page], trimbox=(36, 36, 576, 756))
        assert "real body text" in res.markdown
        assert ".indd" not in res.markdown and "Job:" not in res.markdown
        assert res.stats.clipped_chars > 0

    def test_shadow_layer_is_deduplicated(self, tmp_path):
        # pdfium itself drops a copy that is an IDENTICAL text object; real
        # shadow plates are chunked differently from the type above them,
        # which is the case the engine's own glyph dedupe must handle.
        # "Shadow Ti" is 113.376pt wide in 24pt Helvetica.
        page = [
            Text("Shadow Ti", 100.8, 599.2, size=24),  # the shadow plate...
            Text("tle Here", 100.8 + 113.376, 599.2, size=24),
            Text("Plain body text below the title.", 100, 500),
            Text("Shadow Title Here", 100, 600, size=24),  # ...and the type
        ]
        res = _extract(tmp_path, [page])
        assert res.markdown.count("Shadow Title Here") == 1
        assert "SShh" not in res.markdown
        assert res.stats.duplicate_chars >= len("ShadowTitleHere")

    def test_repeated_letters_are_not_duplicates(self, tmp_path):
        md = _extract(tmp_path, [[Text("All bookkeeping committees agree: 1100 cells.", 72, 700)]]).markdown
        assert "All bookkeeping committees agree: 1100 cells." in md


class TestParagraphs:
    def test_line_end_hyphen_rejoins(self, tmp_path):
        lines = [
            "This long line of body prose deliberately ends in a devi-",
            "ation from the usual pattern, and the text keeps going on",
            "until the paragraph is over.",
        ]
        md = _extract(tmp_path, [[Text(t, 72, 700 - 12 * i) for i, t in enumerate(lines)]]).markdown
        assert "deviation from the usual pattern" in md

    def test_real_compound_keeps_its_hyphen(self, tmp_path):
        lines = [
            "Designs with long-term appeal age well, and a well known rule of",
            "thumb says that the most loved designs share a common long-",
            "term quality that is hard to fake in a rush.",
        ]
        md = _extract(tmp_path, [[Text(t, 72, 700 - 12 * i) for i, t in enumerate(lines)]]).markdown
        assert "common long-term quality" in md

    def test_paragraph_continues_across_a_column_break(self, tmp_path):
        left = ["The first column carries a sentence that runs right to the",
                "bottom of the column and then keeps going in the next"]
        right = ["column without a break, which the reader never notices at all.",
                 "A new sentence starts afresh here."]
        items = [Text(t, 72, 700 - 12 * i) for i, t in enumerate(left)]
        items += [Text(t, 330, 700 - 12 * i) for i, t in enumerate(right)]
        md = _extract(tmp_path, [items]).markdown
        assert "keeps going in the next column without a break" in md

    def test_paragraph_continues_across_a_page_break(self, tmp_path):
        pages = [
            [Text("Body text on the first page carries on until the page ends in the", 72, 100)],
            [Text("middle of a sentence, and resumes on the following page.", 72, 700)],
        ]
        md = _extract(tmp_path, pages).markdown
        assert "ends in the middle of a sentence" in md
        # With page anchors the anchor must stay between the halves.
        md = _extract(tmp_path, pages, name="b.pdf", anchors=True).markdown
        assert md.index("ends in the") < md.index("<!-- page 2 -->") < md.index("middle of")

    def test_new_sentence_after_a_break_is_not_merged(self, tmp_path):
        pages = [
            [Text("A complete paragraph that ends properly with a period.", 72, 100)],
            [Text("another block that happens to start in lowercase.", 72, 700)],
        ]
        md = _extract(tmp_path, pages).markdown
        assert "period.\n\nanother block" in md

    def test_wrapped_lines_become_one_paragraph(self, tmp_path):
        lines = [
            "Paragraphs in print are wrapped at the column edge, so each visual",
            "line is only a fragment of the sentence that the reader follows and",
            "the converter must join them back together.",
        ]
        md = _extract(tmp_path, [[Text(t, 72, 700 - 12 * i) for i, t in enumerate(lines)]]).markdown
        assert "visual line is only a fragment" in md
        assert "reader follows and the converter" in md


class TestCode:
    def test_monospace_block_is_fenced_with_indentation(self, tmp_path):
        page = [
            Text("The function below computes an area.", 72, 720),
            Text("def area(r):", 72, 690, mono=True),
            Text("return 3.14 * r * r", 72 + 4 * 6, 678, mono=True),  # 4 Courier cells in
            Text("That is all there is to it.", 72, 640),
        ]
        md = _extract(tmp_path, [page]).markdown
        fence = re.search(r"```\n(.*?)\n```", md, re.S)
        assert fence, md
        assert fence.group(1).split("\n") == ["def area(r):", "    return 3.14 * r * r"]


    def test_wrapped_monospace_url_stays_in_its_sentence(self, tmp_path):
        page = [
            Text("We can build a simple pub/sub system on top of Active Support", 72, 700),
            Text("Notifications (https://api.example.org/Notifications.html) - the", 72, 688, mono=True),
            Text("instrumentation framework built into Rails.", 72, 676),
        ]
        md = _extract(tmp_path, [page]).markdown
        assert "```" not in md
        assert "Active Support Notifications (https://api.example.org" in md

    def test_command_after_a_colon_stays_code(self, tmp_path):
        page = [
            Text("Install the package with the following command:", 72, 700),
            Text("$ pip install markdown-sidekick", 72, 688, mono=True),
            Text("Then run it.", 72, 660),
        ]
        md = _extract(tmp_path, [page]).markdown
        assert "```\n$ pip install markdown-sidekick\n```" in md


class TestLists:
    def test_bullets_stay_with_their_items(self, tmp_path):
        # Tightly-leaded list with the marker set well apart from the text:
        # the markers must not become a column of their own.
        items = ["First item text", "Second item text", "Third item text", "Fourth item text"]
        page = []
        for n, text in enumerate(items):
            y = 700 - 11 * n
            page.append(Text("\x95", 72, y))  # WinAnsi bullet
            page.append(Text(text, 90, y))
        md = _extract(tmp_path, [page]).markdown
        for text in items:
            assert re.search(r"^•\s+" + text, md, re.M), md

    def test_bulleted_code_names_are_a_list_not_a_listing(self, tmp_path):
        page = [Text("Create the following directories:", 72, 720)]
        for n, name in enumerate(["rating", "metadata", "movie"]):
            page.append(Text(f"\x95 {name}", 90, 700 - 11 * n, mono=True))
        md = _extract(tmp_path, [page]).markdown
        assert "```" not in md
        assert re.search(r"^• ?metadata$", md, re.M), md


class TestTables:
    def test_numeric_grid_becomes_a_table(self, tmp_path):
        rows = [("mm", "points", "picas", "inches"),
                ("1.586", "4.513", "0.375", "1/16"),
                ("3.175", "9.034", "0.75", "1/8"),
                ("4.763", "13.552", "1.125", "3/16"),
                ("6.35", "18.068", "1.5", "1/4")]
        items = [Text(cell, x, 700 - 14 * r)
                 for r, row in enumerate(rows) for cell, x in zip(row, (72, 150, 228, 306))]
        res = _extract(tmp_path, [items])
        line = next(l for l in res.markdown.split("\n") if "1.586" in l)
        assert line.startswith("|") and "4.513" in line and "1/16" in line
        assert res.stats.tables == 1


class TestFurniture:
    def test_running_header_and_folio_removed(self, tmp_path):
        pages = [
            [Text("THE BOOK TITLE", 72, 760, size=8),
             Text(f"Unique body text for page number {n + 1} right here.", 72, 600),
             Text(str(n + 1), 300, 30, size=8)]
            for n in range(5)
        ]
        md = _extract(tmp_path, pages).markdown
        assert "THE BOOK TITLE" not in md
        assert not re.search(r"^\d+$", md, re.M)
        for n in range(5):
            assert f"page number {n + 1} right here" in md

    def _folioed(self, n_pages, extra):
        """Pages with a real folio at the foot, plus ``extra(n)`` items."""
        return [
            [Text(f"Body text for page {n + 1} in the middle of the page.", 72, 500),
             Text(str(n + 1), 300, 30, size=8)] + extra(n)
            for n in range(n_pages)
        ]

    def test_numbers_in_the_margin_that_are_not_folios_survive(self, tmp_path):
        # A table row and a lone value near the foot, and list numbers at
        # the head of several pages: none of them match the folio sequence.
        colours = ["red", "blue", "green", "gold", "grey"]

        def extra(n):
            items = [Text("2.", 72, 760), Text(f"Mix the {colours[n]} paint well.", 90, 760)]
            if n == 1:
                items += [Text("215", 72, 60), Text("750", 200, 60), Text("42", 400, 80)]
            return items

        md = _extract(tmp_path, self._folioed(5, extra)).markdown
        assert "215" in md and "750" in md and "42" in md
        for colour in colours:
            assert f"2. Mix the {colour} paint well." in md
        assert not re.search(r"^[1-5]$", md, re.M)  # the real folios still go

    def test_ocr_pages_do_not_shift_the_folio_sequence(self, tmp_path):
        def extra(n):
            return [Text("Repeat for step 2", 72, 40)] if n == 0 else []

        res = _extract(
            tmp_path, self._folioed(7, extra), ocr_pages=frozenset({2}),
            ocr_page=lambda page: "Scanned words.",
        )
        assert "Repeat for step 2" in res.markdown
        assert not re.search(r"^[1-7]$", res.markdown, re.M)


class TestOutline:
    def _book(self):
        return [
            [Text("A Cover Line", 72, 700, size=20)],
            [Text("Chapter One", 72, 720, size=20),
             Text("Body of the first chapter goes here.", 72, 680),
             Text("A Section", 72, 640, size=14),
             Text("Body of the section goes here.", 72, 610)],
            [Text("Chapter Two", 72, 720, size=20),
             Text("Body of the second chapter.", 72, 680)],
        ]

    def test_bookmarks_become_headings(self, tmp_path):
        outline = [(0, "Cover", 0), (0, "Chapter One", 1), (1, "A Section", 1), (0, "Chapter Two", 2)]
        md = _extract(tmp_path, self._book(), outline=outline).markdown
        assert "# Chapter One" in md and "## A Section" in md and "# Chapter Two" in md
        # The printed title is replaced by the heading, not duplicated...
        assert md.count("Chapter One") == 1 and md.count("A Section") == 1
        # ...and front-matter bookmarks never become headings.
        assert "# Cover" not in md and "A Cover Line" in md
        assert md.index("A Cover Line") < md.index("# Chapter One") < md.index("## A Section")

    def test_duplicate_category_index_is_ignored(self, tmp_path):
        outline = [(0, "Contents", 0), (1, "Chapter One", 1), (1, "Chapter Two", 2),
                   (0, "Contents by Category", 0), (1, "Category Z", 1),
                   (2, "Chapter One", 1), (2, "Chapter Two", 2)]
        md = _extract(tmp_path, self._book(), outline=outline).markdown
        assert md.count("# Chapter One") == 1
        assert "Category Z" not in md

    def test_title_from_metadata_when_sane(self, tmp_path):
        res = _extract(tmp_path, self._book(), title="A Real Book Title")
        assert res.title == "A Real Book Title"
        res = _extract(tmp_path, self._book(), name="b.pdf", title="9781610581899.pdf")
        assert res.title == ""


class TestFigureMarkers:
    def _page(self):
        return [
            Text("A caption-less page with one figure below.", 72, 720),
            Image((160, 130), (72, 300, 200, 160)),
            Image((100, 100), (330, 300, 80, 80), seed=3),  # too small: no marker
        ]

    def test_marker_where_the_figure_sits(self, tmp_path):
        md = _extract(tmp_path, [self._page()], figure_markers=True).markdown
        assert md.count("<!-- figure ") == 1
        assert md.index("one figure below") < md.index("<!-- figure 1.1 -->")

    def test_figure_between_paragraphs_keeps_them_apart(self, tmp_path):
        page = [
            Text("Chapter One", 72, 720, size=20),
            Text("Words before the figure.", 72, 690),
            Image((300, 200), (72, 400, 300, 200)),
            Text("Words after the figure.", 72, 300),
        ]
        md = _extract(tmp_path, [page], figure_markers=True).markdown
        assert md.index("before the figure.") < md.index("<!-- figure 1.1 -->") < md.index("Words after")

    def test_no_markers_unless_requested(self, tmp_path):
        assert "<!-- figure" not in _extract(tmp_path, [self._page()]).markdown


class TestDocumentRepair:
    def test_ligature_control_char_resolved_from_document_vocabulary(self):
        stats = pdflayout.LayoutStats()
        text = "A di\x81erent approach. Different designs differ."
        out = pdflayout._repair_ligatures(text, stats)
        assert out == "A different approach. Different designs differ."
        assert stats.ligatures_repaired == 1

    def test_ligature_resolved_from_seed_words(self):
        out = pdflayout._repair_ligatures("An e\x07ective plan.", pdflayout.LayoutStats())
        assert out == "An effective plan."

    def test_unresolvable_control_char_dropped(self):
        assert pdflayout._repair_ligatures("x\x05y", pdflayout.LayoutStats()) == "xy"

    def test_inline_strings_are_repaired_and_yaml_safe(self):
        # Titles taken from a title page skip the body's text repairs unless
        # cleaned — a raw \x02 made the front matter unparseable YAML.
        assert pdflayout._clean_inline("Extra\x02ordinary E\x1acient\n Layouts\ud800") == (
            "Extraordinary Efficient Layouts"
        )

    def test_hyphen_resolution(self):
        text = "a devi\x02ation and a long\x02term view; long-term plans"
        assert pdflayout._resolve_hyphens(text) == "a deviation and a long-term view; long-term plans"

    def test_dangling_hyphen_never_swallows_a_paragraph_gap(self):
        # Regression: a break left open at a paragraph's end once glued the
        # hyphen onto the next block's code fence ("oth-```"), unbalancing
        # every fence after it.
        text = "we also have oth\x02\n\n```\ncode = 1\n```\n"
        assert pdflayout._resolve_hyphens(text) == "we also have oth-\n\n```\ncode = 1\n```\n"


class TestIntegration:
    def test_converter_routes_digital_pdf_to_layout(self, tmp_path):
        path = tmp_path / "c.pdf"
        path.write_bytes(make_layout_pdf([_two_columns()], title="Columns Book"))
        result = ConversionEngine(enable_ocr=False, enable_audio=False).convert_file(path)
        assert result.ok and result.engine == "pdflayout"
        assert result.doc_title == "Columns Book"

    def test_ocr_pages_use_the_callback(self, tmp_path):
        pages = [[Text("Digital text page.", 72, 700)], []]
        res = _extract(
            tmp_path, pages, anchors=True, ocr_pages=frozenset({1}),
            ocr_page=lambda page: "TEXT RECOGNISED BY OCR",
        )
        assert "<!-- page 2 (ocr) -->" in res.markdown
        assert "TEXT RECOGNISED BY OCR" in res.markdown
        assert "<!-- page 1 (text) -->" in res.markdown
