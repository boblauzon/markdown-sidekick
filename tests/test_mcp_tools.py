"""MCP tool tests via the in-memory fastmcp client (no subprocess)."""

from __future__ import annotations

import asyncio
import http.server
import json
import threading

import pytest

fastmcp = pytest.importorskip("fastmcp")

from markdown_sidekick import mcp_server  # noqa: E402


def _call(tool: str, **kwargs):
    async def run():
        async with fastmcp.Client(mcp_server.mcp) as client:
            result = await client.call_tool(tool, kwargs)
            return result

    return asyncio.run(run())


def _text(result) -> str:
    return result.content[0].text


@pytest.fixture()
def big_html(tmp_path):
    p = tmp_path / "big.html"
    # Paragraphs must be unique — identical repeated lines would (correctly)
    # be removed by the cleanup pipeline's boilerplate pass.
    chapters = "".join(
        f"<h1>Chapter {i}</h1>"
        + "".join(f"<p>Sentence {i}.{j} carries distinct body text.</p>" for j in range(40))
        for i in range(1, 4)
    )
    p.write_text(chapters, encoding="utf-8")
    return p


class TestConvertLocalFile:
    def test_basic(self, big_html):
        out = _text(_call("convert_local_file", file_path=str(big_html)))
        assert "Chapter 1" in out

    def test_truncation(self, big_html):
        out = _text(_call("convert_local_file", file_path=str(big_html), max_chars=500))
        assert "Truncated at 500" in out

    def test_save_to(self, big_html, tmp_path):
        dest = tmp_path / "saved.md"
        out = _text(_call("convert_local_file", file_path=str(big_html), save_to=str(dest)))
        assert "Saved Markdown to" in out and "Quality" in out
        assert dest.exists()

    def test_missing_file(self):
        out = _text(_call("convert_local_file", file_path="Z:/nope/missing.pdf"))
        assert out.startswith("Error")


class TestOutlineAndSections:
    def test_outline_lists_sections(self, big_html):
        result = _call("convert_outline", file_path=str(big_html))
        data = json.loads(_text(result))
        assert data["quality"]["headings"] == 3
        titles = [s["title"] for s in data["sections"]]
        assert "Chapter 2" in titles

    def test_section_fetch(self, big_html):
        data = json.loads(_text(_call("convert_outline", file_path=str(big_html))))
        idx = next(i for i, s in enumerate(data["sections"]) if s["title"] == "Chapter 3")
        out = _text(_call("convert_section", file_path=str(big_html), section_index=idx))
        assert out.startswith("# Chapter 3")
        assert "Chapter 2" not in out

    def test_section_bad_index(self, big_html):
        out = _text(_call("convert_section", file_path=str(big_html), section_index=99))
        assert out.startswith("Error")

    def test_sections_fit_max_tokens(self, big_html):
        # The outline's budget is a hard cap: chapters over max_tokens are
        # sub-split so an AI client can trust every section fits its context.
        data = json.loads(
            _text(_call("convert_outline", file_path=str(big_html), max_tokens=200))
        )
        assert len(data["sections"]) > 3  # chapters were sub-split
        assert all(s["est_tokens"] <= 200 for s in data["sections"])
        # Same max_tokens on convert_section keeps indices aligned.
        last = len(data["sections"]) - 1
        out = _text(
            _call(
                "convert_section",
                file_path=str(big_html),
                section_index=last,
                max_tokens=200,
            )
        )
        assert out.strip() and "Error" not in out[:20]


class TestConvertUrl:
    def test_fetches_and_converts(self, tmp_path):
        (tmp_path / "page.html").write_text(
            "<h1>Remote Title</h1><p>Remote body.</p>", encoding="utf-8"
        )
        handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
            *a, directory=str(tmp_path), **kw
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            out = _text(_call("convert_url", url=f"http://127.0.0.1:{port}/page.html"))
            assert "Remote Title" in out
        finally:
            server.shutdown()

    def test_rejects_non_http(self):
        out = _text(_call("convert_url", url="file:///etc/passwd"))
        assert out.startswith("Error")


class TestCapabilities:
    def test_capabilities(self):
        data = json.loads(_text(_call("list_capabilities")))
        assert "image_ocr" in data
