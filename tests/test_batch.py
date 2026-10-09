"""Library-scale batch conversion (batch.py + `cli batch`).

The reference run converted a 504-book library with 504 tool calls, its own
inventory filter, de-duplicator and retitler. These tests encode what one
batch call must now guarantee: every input in exactly one manifest row,
cheap re-runs, title-based names that never overwrite another file, and a
compact summary.
"""

from __future__ import annotations

import csv
import json
import threading
from pathlib import Path

import pytest

from markdown_sidekick import batch, cli
from markdown_sidekick.batch import BatchOptions, discover, run_batch, safe_filename

from test_library_trust import _GROKKING, make_epub

ENGINE = {"enable_ocr": False, "enable_audio": False}


def _html(path: Path, title: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"<p>{title} paragraph {i} has its own distinct sentence.</p>" for i in range(6))
    path.write_text(f"<html><body><h1>{title}</h1>{body}</body></html>", encoding="utf-8")
    return path


@pytest.fixture()
def library(tmp_path):
    lib = tmp_path / "lib"
    make_epub((lib / "Manning").mkdir(parents=True) or lib / "Manning" / "Grokking_Algorithms.epub", _GROKKING)
    _html(lib / "Notes" / "meeting-notes.html", "Meeting Notes")
    _html(lib / "report.htm", "Quarterly Report")
    (lib / "Manning" / "old.mobi").write_bytes(b"BOOKMOBI")
    # Never walked into: tool environments, hidden folders.
    _html(lib / ".venv" / "Lib" / "page.html", "Venv Page")
    _html(lib / "node_modules" / "pkg" / "readme.html", "Package Readme")
    _html(lib / ".hidden" / "x.html", "Hidden Page")
    # Not picked up by default: earlier conversions, screenshots.
    (lib / "Notes" / "old.md").write_text("# Old\n", encoding="utf-8")
    (lib / "Notes" / "shot.png").write_bytes(b"\x89PNG")
    return lib


def _opts(tmp_path, **kw) -> BatchOptions:
    kw.setdefault("workers", 1)
    kw.setdefault("engine", ENGINE)
    return BatchOptions(output_root=tmp_path / "out", **kw)


class TestDiscover:
    def test_default_scope(self, library, tmp_path):
        names = sorted(j.source.name for j in discover([library], _opts(tmp_path)))
        assert names == ["Grokking_Algorithms.epub", "meeting-notes.html", "old.mobi", "report.htm"]

    def test_include_exclude_and_output_root_inside_input(self, library, tmp_path):
        opts = BatchOptions(output_root=library / "_md", engine=ENGINE, include=("*.png", "*.html"),
                            exclude=("Notes/meeting*",))
        _html(library / "_md" / "earlier.html", "Earlier Output")
        names = sorted(j.source.name for j in discover([library], opts))
        assert names == ["shot.png"]  # meeting-notes excluded; _md is the output root

    def test_explicit_files_are_taken_as_given(self, library, tmp_path):
        md = library / "Notes" / "old.md"
        jobs = discover([md, library / "Manning" / "Grokking_Algorithms.epub", md], _opts(tmp_path))
        assert [j.source.name for j in jobs] == ["old.md", "Grokking_Algorithms.epub"]
        assert all(j.rel_dir == Path() for j in jobs)


class TestSafeFilename:
    def test_windows_rules(self):
        assert safe_filename("Kubernetes: Up and Running") == "Kubernetes - Up and Running"
        assert safe_filename('What <is> "this"? a/b\\c|d*') == "What is this abcd"
        assert safe_filename("Trailing dots...  ") == "Trailing dots"
        assert safe_filename("CON") == "CON_"
        assert safe_filename("nul.txt") == "nul.txt_"
        assert safe_filename("") == "untitled"

    def test_long_names_cut_at_a_word(self):
        name = safe_filename("A very long title " * 10, max_len=40)
        assert len(name) <= 40 and not name.endswith(" ") and name.startswith("A very long title")


class TestRun:
    def test_every_input_in_one_row_and_manifests(self, library, tmp_path):
        opts = _opts(tmp_path, naming="title")
        summary = run_batch([library], opts)
        assert (summary.total, summary.converted, summary.skipped, summary.failed) == (4, 3, 1, 0)
        assert summary.by_reason == {"unsupported_format": 1}
        out = opts.output_root
        # Mirrored folders, title-based names.
        assert (out / "Manning" / "Grokking Algorithms.md").exists()
        assert (out / "Notes" / "Meeting Notes.md").exists()
        assert (out / "Quarterly Report.md").exists()
        manifest = json.loads((out / batch.MANIFEST_NAME).read_text(encoding="utf-8"))
        assert len(manifest["rows"]) == 4
        grok = next(r for r in manifest["rows"] if r["source"].endswith("Grokking_Algorithms.epub"))
        assert grok["title_source"] == "opf" and grok["identifier"] == "isbn:9781617292231"
        assert grok["status"] == "converted" and grok["quality"] is not None
        with open(out / batch.MANIFEST_CSV, encoding="utf-8-sig", newline="") as fh:
            table = list(csv.DictReader(fh))
        assert len(table) == 4 and {r["status"] for r in table} == {"converted", "skipped"}

    def test_rerun_skips_unchanged_and_reconverts_a_changed_source(self, library, tmp_path):
        opts = _opts(tmp_path, naming="title")
        run_batch([library], opts)
        again = run_batch([library], _opts(tmp_path, naming="title"))
        assert again.converted == 0 and again.by_reason == {"unchanged": 3, "unsupported_format": 1}

        _html(library / "report.htm", "Quarterly Report")  # same title, new content
        third = run_batch([library], _opts(tmp_path, naming="title"))
        assert third.converted == 1
        # Rewritten in place: it owns the file it wrote last time.
        assert sorted(p.name for p in opts.output_root.glob("Quarterly Report*.md")) == ["Quarterly Report.md"]

    def test_changed_settings_reconvert(self, library, tmp_path):
        run_batch([library], _opts(tmp_path))
        assert run_batch([library], _opts(tmp_path, clean=False)).converted == 3

    def test_name_collisions_never_overwrite(self, tmp_path):
        lib = tmp_path / "lib"
        make_epub((lib.mkdir() or lib) / "a.epub", _GROKKING)
        make_epub(lib / "b.epub", _GROKKING.replace("Aditya Y. Bhargava", "Someone Else"))
        out = tmp_path / "out"
        out.mkdir()
        (out / "Grokking Algorithms.md").write_text("the user's own notes\n", encoding="utf-8")
        # Same ISBN: dedupe would (rightly) keep one; this is about naming.
        summary = run_batch([lib], _opts(tmp_path, naming="title", dedupe=False))
        assert summary.converted == 2
        names = sorted(p.name for p in out.glob("*.md"))
        assert names == [
            "Grokking Algorithms - Aditya Y. Bhargava.md",
            "Grokking Algorithms - Someone Else.md",
            "Grokking Algorithms.md",
        ]
        assert (out / "Grokking Algorithms.md").read_text(encoding="utf-8") == "the user's own notes\n"

    def test_overwrite_lifts_the_rule(self, tmp_path):
        lib = tmp_path / "lib"
        make_epub((lib.mkdir() or lib) / "a.epub", _GROKKING)
        out = tmp_path / "out"
        out.mkdir()
        (out / "Grokking Algorithms.md").write_text("old\n", encoding="utf-8")
        run_batch([lib], _opts(tmp_path, naming="title", overwrite=True))
        assert "title: Grokking Algorithms" in (out / "Grokking Algorithms.md").read_text(encoding="utf-8")

    def test_title_author_naming(self, tmp_path):
        lib = tmp_path / "lib"
        make_epub((lib.mkdir() or lib) / "a.epub", _GROKKING)
        run_batch([lib], _opts(tmp_path, naming="title_author"))
        assert (tmp_path / "out" / "Grokking Algorithms - Aditya Y. Bhargava.md").exists()

    def test_failures_are_rows_with_codes(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "broken.docx").write_bytes(bytes(range(256)) * 40)  # not a zip: corrupt
        _html(lib / "fine.html", "Fine Page")
        summary = run_batch([lib], _opts(tmp_path))
        assert (summary.converted, summary.failed) == (1, 1)
        compact = summary.as_dict()
        assert compact["failures"][0]["source"].endswith("broken.docx")
        assert compact["failures"][0]["code"].startswith("MS-1")
        assert len(json.dumps(compact)) < 4096

    def test_cancel_leaves_a_valid_manifest(self, library, tmp_path):
        stop = threading.Event()
        stop.set()
        summary = run_batch([library], _opts(tmp_path), cancel=stop)
        assert summary.cancelled and summary.converted == 0
        assert summary.by_reason == {"unsupported_format": 1, "cancelled": 3}
        rows = json.loads((tmp_path / "out" / batch.MANIFEST_NAME).read_text(encoding="utf-8"))["rows"]
        assert len(rows) == 4

    def test_chapter_folders(self, tmp_path):
        lib = tmp_path / "lib"
        _html(lib / "book.html", "Book")
        md = "".join(
            f"<h1>Chapter {i}: Part {i}</h1>" + "".join(f"<p>Chapter {i} sentence {j} is unique.</p>" for j in range(5))
            for i in range(1, 4)
        )
        (lib / "book.html").write_text(md, encoding="utf-8")
        summary = run_batch([lib], _opts(tmp_path, style="chapters"))
        row = summary.rows[0]
        assert Path(row.output).is_dir() and (Path(row.output) / "index.md").exists()
        assert len(row.files) >= 4  # parts + index + manifest

    def test_rows_out_of_scope_stay_in_the_manifest(self, library, tmp_path):
        run_batch([library], _opts(tmp_path))
        run_batch([library / "Notes"], _opts(tmp_path))
        rows = json.loads((tmp_path / "out" / batch.MANIFEST_NAME).read_text(encoding="utf-8"))["rows"]
        assert len(rows) == 4

    def test_worker_processes(self, library, tmp_path):
        summary = run_batch([library], _opts(tmp_path, workers=2))
        assert (summary.converted, summary.failed) == (3, 0)


class TestCli:
    def test_batch_command(self, library, tmp_path, capsys):
        code = cli.main(["batch", str(library), "--out", str(tmp_path / "o"), "--workers", "1",
                         "--naming", "title", "--no-ocr", "--no-audio", "--quiet"])
        assert code == 0
        result = json.loads(capsys.readouterr().out)
        assert (result["total"], result["converted"]) == (4, 3)
        assert result["manifest"].endswith(batch.MANIFEST_NAME)

    def test_nothing_found_is_an_error(self, tmp_path, capsys):
        (tmp_path / "empty").mkdir()
        assert cli.main(["batch", str(tmp_path / "empty"), "--out", str(tmp_path / "o"), "--quiet"]) == 1
