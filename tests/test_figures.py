"""Figure extraction tests using a generated PDF with an embedded image."""

from __future__ import annotations

from pathlib import Path

import pytest

from markdown_sidekick import figures
from markdown_sidekick.figures import FigureRef, insert_figure_links

from pdfgen import make_pdf

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
    def test_extracts_image_as_png(self, pdf_with_image, tmp_path):
        figs = figures.extract_pdf_figures(pdf_with_image, tmp_path / "assets")
        assert len(figs) == 1
        fig = figs[0]
        assert fig.page == 1
        assert fig.path.exists()
        assert fig.path.suffix == ".png"
        assert (fig.width, fig.height) == (120, 100)

    def test_tiny_images_skipped(self, pdf_with_image, tmp_path):
        figs = figures.extract_pdf_figures(
            pdf_with_image, tmp_path / "assets", min_pixels=1_000_000
        )
        assert figs == []

    def test_textonly_pdf_yields_nothing(self, tmp_path):
        p = tmp_path / "plain.pdf"
        p.write_bytes(make_pdf(["Just text."]))
        assert figures.extract_pdf_figures(p, tmp_path / "assets") == []


class TestLinkInsertion:
    def _fig(self, page: int) -> FigureRef:
        return FigureRef(page, Path(f"fig-p{page:04d}-abc.png"), 300, 200)

    def test_links_after_matching_anchor(self):
        md = "<!-- page 1 -->\n\ntext one\n\n<!-- page 2 -->\n\ntext two\n"
        out = insert_figure_links(md, [self._fig(2)])
        anchor2 = out.index("<!-- page 2 -->")
        assert out.index("![Figure (page 2") > anchor2
        assert "Extracted figures" not in out

    def test_no_anchors_appends_section(self):
        out = insert_figure_links("plain document\n", [self._fig(3)])
        assert "## Extracted figures" in out
        assert "![Figure (page 3" in out

    def test_no_figures_is_identity(self):
        assert insert_figure_links("doc\n", []) == "doc\n"
