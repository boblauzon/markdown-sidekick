"""CLI tests — run the real pipeline against small generated inputs."""

from __future__ import annotations

import json

import pytest

from markdown_sidekick import cli


@pytest.fixture()
def html_doc(tmp_path):
    p = tmp_path / "sample.html"
    p.write_text(
        "<h1>Alpha</h1><p>First chapter body.</p>"
        "<h1>Beta</h1><p>Second chapter body.</p>",
        encoding="utf-8",
    )
    return p


class TestConvert:
    def test_single_file_with_front_matter(self, html_doc, tmp_path, capsys):
        rc = cli.main(["convert", str(html_doc), "--out", str(tmp_path / "out")])
        assert rc == 0
        out_file = tmp_path / "out" / "sample.md"
        text = out_file.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "Alpha" in text
        assert "ok " in capsys.readouterr().out

    def test_split_chapters_book_folder(self, html_doc, tmp_path):
        rc = cli.main(
            ["convert", str(html_doc), "--out", str(tmp_path / "out"), "--split-chapters"]
        )
        assert rc == 0
        book = tmp_path / "out" / "sample"
        assert (book / "index.md").exists()
        assert (book / "manifest.json").exists()
        parts = sorted(p.name for p in book.glob("0*.md"))
        assert parts == ["01-alpha.md", "02-beta.md"]

    def test_json_output_with_quality(self, html_doc, tmp_path, capsys):
        rc = cli.main(
            ["convert", str(html_doc), "--out", str(tmp_path / "o"), "--json", "--quality"]
        )
        assert rc == 0
        record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert record["ok"] is True
        assert record["quality"]["headings"] == 2
        assert record["written"]

    def test_missing_file_exits_nonzero(self, tmp_path, capsys):
        rc = cli.main(["convert", str(tmp_path / "nope.pdf")])
        assert rc == 1
        assert "ERROR" in capsys.readouterr().out

    def test_no_front_matter(self, html_doc, tmp_path):
        rc = cli.main(
            ["convert", str(html_doc), "--out", str(tmp_path / "o"), "--no-front-matter"]
        )
        assert rc == 0
        text = (tmp_path / "o" / "sample.md").read_text(encoding="utf-8")
        assert not text.startswith("---")


class TestCapabilities:
    def test_reports_json(self, capsys):
        rc = cli.main(["capabilities"])
        assert rc == 0
        info = json.loads(capsys.readouterr().out)
        assert "ocr" in info and "audio" in info
