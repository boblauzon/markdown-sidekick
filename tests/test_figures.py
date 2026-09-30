"""Figure extraction tests using generated PDFs with embedded images."""

from __future__ import annotations

from pathlib import Path

import pytest

from markdown_sidekick import figures
from markdown_sidekick.figures import FigureRef, insert_figure_links, strip_figure_markers

from pdfgen import Image, Text, make_layout_pdf, make_pdf

pytestmark = pytest.mark.skipif(
    not figures.figures_available(), reason="pypdfium2 not available"
)


@pytest.fixture()
def pdf_with_image(tmp_path):
    p = tmp_path / "figured.pdf"
    p.write_bytes(
        make_pdf(["First page with a figure.", "Second page, text only."], image_on_page=0)
    )
    return p


class TestExtraction:
    def test_extracts_image_named_by_page_and_index(self, pdf_with_image, tmp_path):
        figs = figures.extract_pdf_figures(pdf_with_image, tmp_path / "images")
        assert len(figs) == 1
        fig = figs[0]
        assert (fig.page, fig.index) == (1, 1)
        assert fig.path.exists()
        assert fig.path.parent == tmp_path / "images"
        assert fig.path.stem == "fig_p1_1"
        assert fig.path.suffix in (".png", ".jpg")
        assert (fig.width, fig.height) == (160, 130)

    def test_images_under_120px_on_a_side_are_skipped(self, tmp_path):
        p = tmp_path / "small.pdf"
        p.write_bytes(
            make_layout_pdf(
                [[Text("t", 72, 700), Image((400, 100), (72, 300, 300, 75)),
                  Image((100, 400), (400, 300, 60, 240), seed=2)]]
            )
        )
        assert figures.extract_pdf_figures(p, tmp_path / "images") == []

    def test_images_outside_the_trimbox_are_skipped(self, tmp_path):
        p = tmp_path / "spread.pdf"
        p.write_bytes(
            make_layout_pdf(
                [[Text("t", 72, 700), Image((200, 200), (-300, 300, 200, 200)),
                  Image((200, 200), (100, 300, 200, 200), seed=5)]],
                trimbox=(36, 36, 576, 756),
            )
        )
        figs = figures.extract_pdf_figures(p, tmp_path / "images")
        assert [(f.page, f.index) for f in figs] == [(1, 1)]

    def test_repeated_image_written_once(self, tmp_path):
        page = lambda: [Text("x", 72, 700), Image((200, 150), (72, 300, 200, 150))]  # noqa: E731
        p = tmp_path / "logo.pdf"
        p.write_bytes(make_layout_pdf([page(), page(), page()]))
        figs = figures.extract_pdf_figures(p, tmp_path / "images")
        assert len(figs) == 1 and figs[0].page == 1

    def test_page_scans_are_not_figures(self, tmp_path):
        # Every page of a scanned book is one page-sized image: those are the
        # pages themselves. A real figure on a scanned page still counts.
        pages = [[Image((600, 780), (0, 0, 612, 792), seed=n)] for n in range(3)]
        pages[1].append(Image((200, 150), (100, 300, 200, 150), seed=9))
        p = tmp_path / "scan.pdf"
        p.write_bytes(make_layout_pdf(pages))
        figs = figures.extract_pdf_figures(p, tmp_path / "images")
        assert [(f.page, f.index) for f in figs] == [(2, 2)]

    def test_full_bleed_photo_in_a_normal_book_is_a_figure(self, tmp_path):
        pages = [[Text(f"Page {n} text.", 72, 700)] for n in range(4)]
        pages[2] = [Image((600, 780), (0, 0, 612, 792))]
        p = tmp_path / "book.pdf"
        p.write_bytes(make_layout_pdf(pages))
        assert [f.page for f in figures.extract_pdf_figures(p, tmp_path / "images")] == [3]

    def test_textonly_pdf_yields_nothing(self, tmp_path):
        p = tmp_path / "plain.pdf"
        p.write_bytes(make_pdf(["Just text."]))
        assert figures.extract_pdf_figures(p, tmp_path / "images") == []


class TestLinkInsertion:
    def _fig(self, page: int, index: int = 1) -> FigureRef:
        return FigureRef(page, Path(f"fig_p{page}_{index}.jpg"), 300, 200, index=index)

    def test_marker_is_replaced_in_place(self):
        md = "before\n\n<!-- figure 2.1 -->\n\nafter\n"
        out = insert_figure_links(md, [self._fig(2)])
        assert out == "before\n\n![Figure 2.1](images/fig_p2_1.jpg)\n\nafter\n"

    def test_marker_without_figure_is_removed_cleanly(self):
        out = insert_figure_links("a\n\n<!-- figure 9.9 -->\n\nb\n", [self._fig(2)])
        assert "<!-- figure" not in out
        assert "a\n\nb" in out  # no doubled gap left behind

    def test_links_after_matching_anchor_without_marker(self):
        md = "<!-- page 1 -->\n\ntext one\n\n<!-- page 2 -->\n\ntext two\n"
        out = insert_figure_links(md, [self._fig(2)])
        assert out.index("![Figure 2.1]") > out.index("<!-- page 2 -->")
        assert "Extracted figures" not in out

    def test_no_position_appends_section(self):
        out = insert_figure_links("plain document\n", [self._fig(3)])
        assert "## Extracted figures" in out
        assert "![Figure 3.1]" in out

    def test_link_path_is_url_quoted(self):
        out = insert_figure_links("<!-- figure 1.1 -->\n", [self._fig(1)], "images/My Book")
        assert "(images/My%20Book/fig_p1_1.jpg)" in out

    def test_caption_becomes_alt_text(self):
        fig = self._fig(1)
        fig.caption = "A bar chart [of sales]"
        out = insert_figure_links("<!-- figure 1.1 -->\n", [fig])
        assert out.startswith("![A bar chart (of sales)](")

    def test_no_figures_strips_markers(self):
        assert insert_figure_links("doc\n\n<!-- figure 1.1 -->\n\nend\n", []) == "doc\n\nend\n"
        assert insert_figure_links("doc\n", []) == "doc\n"

    def test_strip_figure_markers(self):
        assert strip_figure_markers("a\n\n<!-- figure 3.2 -->\n\nb\n") == "a\n\nb\n"
        assert strip_figure_markers("<!-- page 3 -->\n") == "<!-- page 3 -->\n"


class TestEndToEnd:
    def test_markers_from_layout_engine_match_extracted_files(self, tmp_path):
        from markdown_sidekick import pdflayout

        p = tmp_path / "doc.pdf"
        p.write_bytes(
            make_layout_pdf(
                [[Text("Intro text above the figure.", 72, 720),
                  Image((300, 200), (72, 300, 300, 200)),
                  Text("Closing text far below.", 72, 100)]]
            )
        )
        md = pdflayout.extract_markdown(p, figure_markers=True).markdown
        figs = figures.extract_pdf_figures(p, tmp_path / "images")
        out = insert_figure_links(md, figs)
        assert out.index("Intro text") < out.index("![Figure 1.1](images/fig_p1_1.") < out.index("Closing text")
