"""Page-anchor extraction tests using generated PDFs."""

from __future__ import annotations

import pytest

from markdown_sidekick import ocr
from markdown_sidekick.cleanup import clean_markdown
from markdown_sidekick.converter import ConversionEngine

from pdfgen import make_pdf

pytestmark = pytest.mark.skipif(
    not ocr.pdfium_available(), reason="pypdfium2 not available"
)


@pytest.fixture()
def two_page_pdf(tmp_path):
    p = tmp_path / "two.pdf"
    p.write_bytes(
        make_pdf(
            [
                "Page one says hello to the reader of this document.",
                "Page two continues the very same story with more words.",
            ]
        )
    )
    return p


class TestPdfTextAnchors:
    def test_per_page_text_with_anchors(self, two_page_pdf):
        md = ocr.OcrEngine().pdf_text_to_markdown(two_page_pdf)
        assert "<!-- page 1 -->" in md
        assert "<!-- page 2 -->" in md
        assert "Page one says hello" in md
        assert md.index("page 1") < md.index("Page one") < md.index("page 2")

    def test_converter_routes_to_pdftext_when_enabled(self, two_page_pdf):
        engine = ConversionEngine(enable_ocr=False, enable_audio=False, page_anchors=True)
        result = engine.convert_file(two_page_pdf)
        assert result.ok
        assert result.engine == "pdftext"
        assert "<!-- page 2 -->" in result.markdown

    def test_converter_default_stays_markitdown(self, two_page_pdf):
        engine = ConversionEngine(enable_ocr=False, enable_audio=False)
        result = engine.convert_file(two_page_pdf)
        assert result.ok
        assert result.engine == "markitdown"

    def test_cleanup_preserves_anchors(self):
        text = (
            "<!-- page 7 -->\n\n"
            "A long hard-wrapped prose line that certainly exceeds the sixty five character floor\n"
            "and continues here.\n"
        )
        out, _stats = clean_markdown(text)
        assert "<!-- page 7 -->" in out
