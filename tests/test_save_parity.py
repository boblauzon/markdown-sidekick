"""One save step for every front-end (ADR-002).

The same book saved from the CLI, the MCP server, a library batch, the
classic window and the new window must carry the same front matter. MCP
``save_to`` once titled 504 books after their filenames: it was the one
front-end that didn't pass the book's details on.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from markdown_sidekick import cli, export
from markdown_sidekick.batch import BatchOptions, run_batch
from markdown_sidekick.converter import ConversionEngine
from markdown_sidekick.settings import Settings

from test_library_trust import _GROKKING, make_epub

ENGINE = {"enable_ocr": False, "enable_audio": False}
# Everything except the per-run fields (converted date, token estimate).
FIELDS = ("title", "subtitle", "title_source", "author", "publisher", "date", "language", "identifier",
          "source", "converter")


def _front(path: Path) -> dict:
    block = path.read_text(encoding="utf-8").split("---\n")[1]
    pairs = (line.partition(": ") for line in block.splitlines())
    return {k: v for k, _, v in pairs if k in FIELDS}


@pytest.fixture()
def book(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))  # default settings everywhere
    return make_epub(tmp_path / "Grokking_Algorithms.epub", _GROKKING)


def _cli(book: Path, out: Path) -> Path:
    assert cli.main(["convert", str(book), "--out", str(out), "--no-ocr"]) == 0
    return out / f"{book.stem}.md"


def _mcp(book: Path, out: Path) -> Path:
    fastmcp = pytest.importorskip("fastmcp")
    import asyncio

    from markdown_sidekick import mcp_server

    dest = out / f"{book.stem}.md"

    async def call():
        async with fastmcp.Client(mcp_server.mcp) as client:
            return await client.call_tool("convert_local_file", {"file_path": str(book), "save_to": str(dest)})

    assert "verified on disk" in asyncio.run(call()).content[0].text
    return dest


def _batch(book: Path, out: Path) -> Path:
    summary = run_batch([book], BatchOptions(output_root=out, workers=1, engine=ENGINE))
    assert summary.converted == 1, summary
    return out / f"{book.stem}.md"


def _classic_window(book: Path, out: Path) -> Path:
    from markdown_sidekick import ui
    from markdown_sidekick.cleanup import clean_markdown

    result = ConversionEngine(**ENGINE).convert_file(book)
    text, _ = clean_markdown(result.markdown, engine=result.engine)
    window = SimpleNamespace(  # the parts of the window _save_one uses
        settings=Settings(),
        files={book: result},
        _export_text_for=lambda path: text,
        _with_figures=lambda path, text, images_dir, rel_dir: text,
        _summarize_for_export=lambda *args: "",
    )
    assert ui.MarkdownSidekickApp._save_one(window, book, out, "single", "Claude", set()) == 1
    return out / f"{book.stem}.md"


def _new_window(book: Path, out: Path) -> Path:
    from markdown_sidekick.webui import bridge

    dest = out / f"{book.stem}.md"

    class Window:
        def run_js(self, script):
            pass

        def create_file_dialog(self, dialog_type, **kwargs):
            return (str(dest),)

    api = bridge.Api(Settings(**ENGINE))
    api._window = Window()
    api._add([str(book)])
    item = next(iter(api._items.values()))
    deadline = time.monotonic() + 60
    while not (item.result is not None and not item.preparing and not api._busy):
        assert time.monotonic() < deadline, "conversion did not finish"
        time.sleep(0.05)
    api.save()
    return dest


FRONT_ENDS = {"cli": _cli, "mcp": _mcp, "batch": _batch, "classic window": _classic_window,
              "new window": _new_window}


def test_every_front_end_writes_the_same_front_matter(book, tmp_path):
    fronts = {name: _front(save(book, tmp_path / name.replace(" ", "-"))) for name, save in FRONT_ENDS.items()}
    reference = fronts["cli"]
    assert reference["title"] == "Grokking Algorithms" and reference["title_source"] == "opf"
    assert reference["author"] == "Aditya Y. Bhargava" and reference["publisher"] == "Manning Publications"
    for name, front in fronts.items():
        assert front == reference, name


class TestSaveDispatch:
    DETAILS = {"engine": "markitdown", "title": "Real Title", "author": "A. Author", "title_source": "opf",
               "meta": {"publisher": "Pub"}}
    TEXT = "".join(f"# Chapter {n}\n\n" + f"Chapter {n} has its own sentence number {n}.\n\n" for n in range(1, 5))

    def test_single_file(self, tmp_path):
        res = export.save(self.TEXT, tmp_path / "a.md", source="a.epub", details=self.DETAILS)
        assert res.files == [tmp_path / "a.md"]
        text = (tmp_path / "a.md").read_text(encoding="utf-8")
        assert "title: Real Title" in text and "publisher: Pub" in text and "(markitdown)" in text

    def test_chapters_write_index_and_manifest(self, tmp_path):
        res = export.save(self.TEXT, tmp_path / "book", source="a.epub", style="chapters", details=self.DETAILS)
        assert res.index_path and res.manifest_path and len(res.paths) == 4
        assert res.files == res.paths + [res.index_path, res.manifest_path]

    def test_notebook_target_writes_sources_only(self, tmp_path):
        res = export.save(self.TEXT, tmp_path / "book", source="a.epub", style="ai", ai_target="Gemini Notebook",
                          details=self.DETAILS)
        assert res.index_path is None and res.manifest_path is None
        assert all(p.name.startswith("real-title-") for p in res.paths)

    def test_ai_parts_fit_the_target(self, tmp_path):
        text = "\n\n".join(f"Sentence {n} of a long heading-less document." for n in range(4000))
        res = export.save(text, tmp_path / "book", source="a.txt", style="ai", ai_target="Local LLM")
        assert len(res.paths) > 1
        assert all(export.estimate_tokens(p.read_text(encoding="utf-8")) <= export.AI_TARGETS["Local LLM"] + 200
                   for p in res.paths)  # + the part's front matter

    def test_unknown_style_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            export.save("x", tmp_path / "a.md", source="a.txt", style="pdf")

    def test_figure_dirs_and_claimed_names(self, tmp_path):
        assert export.figure_dirs(tmp_path / "a.md", "single") == (tmp_path / "images" / "a", "images/a")
        assert export.figure_dirs(tmp_path / "a", "ai") == (tmp_path / "a" / "images", "images")
        used: set[str] = set()
        assert [export.claim_name("notes", used) for _ in range(3)] == ["notes", "notes-2", "notes-3"]
