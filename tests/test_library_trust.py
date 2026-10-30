"""v1.3 "correct and trustworthy": real book metadata in front matter,
verified saves, and a launcher that never opens the GUI for a flag.

The shapes come from a 504-book library run through the MCP server: every
front-matter title was the filename, entity-encoded titles leaked through
(``CCNA&#174;``), first-heading guesses named "Preface", and two outputs that
antivirus quarantined were still reported as saved.
"""

from __future__ import annotations

import builtins
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from markdown_sidekick import errors, export, metadata
from markdown_sidekick.converter import ConversionEngine
from markdown_sidekick.export import (
    SaveVerificationError,
    build_front_matter,
    export_book,
    export_single,
    resolve_title,
    tidy_filename_title,
    write_text_verified,
)

ROOT = Path(__file__).resolve().parents[1]

_CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c1</title></head>
<body><h1>Chapter 1: Introduction to algorithms</h1>
<p>Binary search is an algorithm whose input is a sorted list of elements.</p>
<p>Every chapter of this book builds on the one before it, with worked examples.</p>
</body></html>"""


def _opf(metadata_xml: str, unique_id: str = "bookid") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="{unique_id}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    {metadata_xml}
  </metadata>
  <manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>"""


def make_epub(path: Path, metadata_xml: str, unique_id: str = "bookid") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER)
        zf.writestr("OEBPS/content.opf", _opf(metadata_xml, unique_id))
        zf.writestr("OEBPS/c1.xhtml", _CHAPTER)
    return path


_GROKKING = """
    <dc:title>Grokking Algorithms: An illustrated guide for programmers and other curious people</dc:title>
    <dc:creator opf:role="aut">Aditya Y. Bhargava</dc:creator>
    <dc:publisher>Manning Publications</dc:publisher>
    <dc:date>2016-05-01T00:00:00+00:00</dc:date>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">urn:uuid:e1d1256e-119c-400a-a9b7-000f933bb34a</dc:identifier>
    <dc:identifier opf:scheme="ISBN">978-1-61729-223-1</dc:identifier>
"""


# ---------------------------------------------------------------------------
# Reading the source's own metadata
# ---------------------------------------------------------------------------
class TestEpubMetadata:
    def test_opf_fields_and_subtitle_split(self, tmp_path):
        meta = metadata.epub_metadata(make_epub(tmp_path / "Grokking_Algorithms.epub", _GROKKING))
        assert meta == {
            "title": "Grokking Algorithms",
            "subtitle": "An illustrated guide for programmers and other curious people",
            "authors": ["Aditya Y. Bhargava"],
            "publisher": "Manning Publications",
            "date": "2016-05-01",
            "language": "en",
            "identifier": "isbn:9781617292231",
        }

    def test_epub3_refines_title_type_and_creator_role(self, tmp_path):
        xml = """
            <dc:title id="t1">C# in Depth</dc:title>
            <dc:title id="t2">Fourth Edition</dc:title>
            <meta refines="#t1" property="title-type">main</meta>
            <meta refines="#t2" property="title-type">subtitle</meta>
            <dc:creator id="a1">Jon Skeet</dc:creator>
            <dc:creator id="e1">Some Editor</dc:creator>
            <meta refines="#e1" property="role" scheme="marc:relators">edt</meta>
            <dc:creator id="a2">Second Author</dc:creator>
            <dc:identifier id="bookid">urn:uuid:1234</dc:identifier>
        """
        meta = metadata.epub_metadata(make_epub(tmp_path / "c.epub", xml))
        assert meta["title"] == "C# in Depth"
        assert meta["subtitle"] == "Fourth Edition"
        assert meta["authors"] == ["Jon Skeet", "Second Author"]  # the editor is not an author
        assert meta["identifier"] == "urn:uuid:1234"  # no ISBN: the unique identifier

    def test_double_encoded_entities_are_decoded(self, tmp_path):
        xml = """
            <dc:title>CCNA&amp;#174; Certification Study Guide</dc:title>
            <dc:creator>Hacker&amp;#8217;s Friend</dc:creator>
        """
        meta = metadata.epub_metadata(make_epub(tmp_path / "ccna.epub", xml))
        assert meta["title"] == "CCNA® Certification Study Guide"
        assert meta["authors"] == ["Hacker’s Friend"]

    def test_placeholders_and_damaged_packages(self, tmp_path):
        xml = "<dc:title>Real Title</dc:title><dc:creator>Unknown</dc:creator>"
        meta = metadata.epub_metadata(make_epub(tmp_path / "u.epub", xml))
        assert meta == {"title": "Real Title"}
        broken = tmp_path / "broken.epub"
        broken.write_bytes(b"PK\x03\x04 not really a zip")
        assert metadata.epub_metadata(broken) == {}
        assert metadata.read_metadata(tmp_path / "notes.txt") == {}

    def test_packed_author_fields_are_split(self):
        # Real creator fields from a 515-epub library.
        cases = {
            "Saurabh Shrivastava | Neelanjali Srivastav | Dhiraj Thakur":
                ["Saurabh Shrivastava", "Neelanjali Srivastav", "Dhiraj Thakur"],
            "Priyanka Vergadia;Valliappa Lakshmanan": ["Priyanka Vergadia", "Valliappa Lakshmanan"],
            "Ben Auffarth & Leonid Kuligin": ["Ben Auffarth", "Leonid Kuligin"],
            "L. Y. Pratt and N. E. Malcolm": ["L. Y. Pratt", "N. E. Malcolm"],
            "Bas Meijer, Lorin Hochstein, Rene Moser": ["Bas Meijer", "Lorin Hochstein", "Rene Moser"],
            # One person each: surname-first, and a suffix.
            "Samara, Timothy": ["Samara, Timothy"],
            "John Smith, Jr.": ["John Smith, Jr."],
        }
        for raw, expected in cases.items():
            assert metadata.split_authors([raw]) == expected, raw
        assert metadata.split_authors(["A Person", "A Person"]) == ["A Person"]

    def test_publisher_edition_underscore(self):
        assert metadata.tidy_title("AWS for Solutions Architects_Third Edition") == (
            "AWS for Solutions Architects, Third Edition"
        )
        assert metadata.tidy_title("Generative AI with LangChain _Second Edition") == (
            "Generative AI with LangChain, Second Edition"
        )
        assert metadata.tidy_title("snake_case Explained") == "snake_case Explained"

    def test_split_subtitle_only_on_colon_space(self):
        assert metadata.split_subtitle("Kubernetes: Up and Running") == ("Kubernetes", "Up and Running")
        assert metadata.split_subtitle("Real-World Go - Second Edition") == ("Real-World Go - Second Edition", "")
        assert metadata.split_subtitle("Time 10:30") == ("Time 10:30", "")


class TestConverterCarriesMetadata:
    def test_epub_title_comes_from_the_opf(self, tmp_path):
        src = make_epub(tmp_path / "Grokking_Algorithms.epub", _GROKKING)
        result = ConversionEngine(enable_ocr=False, enable_audio=False).convert_file(src)
        assert result.ok, result.error
        assert result.doc_title == "Grokking Algorithms"
        assert result.title_source == "opf"
        assert result.doc_author == "Aditya Y. Bhargava"
        assert result.doc_meta["subtitle"].startswith("An illustrated guide")
        assert result.doc_meta["identifier"] == "isbn:9781617292231"


# ---------------------------------------------------------------------------
# Titles and front matter
# ---------------------------------------------------------------------------
class TestTitleResolution:
    def test_section_headings_never_become_the_title(self):
        for heading in ("Preface", "Contributors", "Acknowledgments", "Table of Contents",
                        "Copyright and credit page", "Dedication", "Other books in this series"):
            title, source = resolve_title(f"# {heading}\n\ntext\n", "Grokking_Algorithms (1).epub")
            assert (title, source) == ("Grokking Algorithms", "filename"), heading

    def test_order_of_precedence(self):
        assert resolve_title("# Heading\n", "a.pdf", "Meta Title", "pdf_info") == ("Meta Title", "pdf_info")
        assert resolve_title("# A Real Book\n\nbody", "x_y.pdf") == ("A Real Book", "heading")
        assert resolve_title("plain\n", "x_y.pdf") == ("x y", "filename")

    def test_tidy_filename_title(self):
        assert tidy_filename_title("Algorithms_and_Data_Structures_for_Massi") == (
            "Algorithms and Data Structures for Massi"
        )
        assert tidy_filename_title("name_V2") == "name"
        assert tidy_filename_title("Report - Copy (2)") == "Report"
        # Run-together names can't be re-spaced safely: left alone.
        assert tidy_filename_title("ccnacertificationstudyguide") == "ccnacertificationstudyguide"


class TestFrontMatterIsStrictYaml:
    yaml = pytest.importorskip("yaml")

    def test_awkward_values_round_trip(self):
        fields = {
            "title": "*NIX Administration",
            "subtitle": "- A Field Guide",
            "edition": "1984",
            "flag": "yes",
            "quote": 'He said "hi" \\ bye',
            "colon": "Kubernetes: Up and Running",
            "tag": "!important",
            "authors": ["Ada Lovelace", "O'Brien, Pat", "null"],
        }
        parsed = self.yaml.safe_load(build_front_matter(fields).strip().strip("-"))
        assert parsed == fields

    def test_exported_header_parses_and_carries_metadata(self, tmp_path):
        out = tmp_path / "book.md"
        meta = {
            "subtitle": "An illustrated guide",
            "authors": ["Aditya Y. Bhargava", "Second Author"],
            "publisher": "Manning Publications",
            "date": "2016-05-01",
            "language": "en",
            "identifier": "isbn:9781617292231",
        }
        export_single("# Preface\n\nbody\n", out, source="Grokking_Algorithms.epub",
                      title="Grokking Algorithms", author="Aditya Y. Bhargava, Second Author",
                      title_source="opf", meta=meta)
        text = out.read_text(encoding="utf-8")
        header = self.yaml.safe_load(text.split("---")[1])
        assert header["title"] == "Grokking Algorithms"
        assert header["title_source"] == "opf"
        assert header["authors"] == ["Aditya Y. Bhargava", "Second Author"]
        assert header["identifier"] == "isbn:9781617292231"
        assert header["source"] == "Grokking_Algorithms.epub"

    def test_book_manifest_records_metadata(self, tmp_path):
        import json

        md = "# Chapter 1: A\n\nalpha text\n\n# Chapter 2: B\n\nbeta text\n"
        res = export_book(md, tmp_path / "b", source="b.epub", title="Book", title_source="opf",
                          meta={"authors": ["One", "Two"], "identifier": "isbn:123"})
        manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
        assert manifest["title_source"] == "opf"
        assert manifest["authors"] == ["One", "Two"]
        part = self.yaml.safe_load(res.paths[0].read_text(encoding="utf-8").split("---")[1])
        assert part["author"] == "One, Two" and part["identifier"] == "isbn:123"


# ---------------------------------------------------------------------------
# Verified saves
# ---------------------------------------------------------------------------
@pytest.fixture()
def fast_retries(monkeypatch):
    monkeypatch.setattr(export, "_RETRY_DELAYS", (0, 0, 0))


def _winerror(code: int, msg: str = "simulated") -> OSError:
    exc = OSError(22, msg)
    exc.winerror = code  # what Windows sets for a Win32 failure
    return exc


def _patch_reads(monkeypatch, fail_with):
    """Make read-backs (mode "rb") call ``fail_with(path)``; writes pass."""
    real_open = builtins.open

    def fake_open(file, mode="r", *args, **kwargs):
        if mode == "rb":
            return fail_with(file) or real_open(file, mode, *args, **kwargs)
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(export, "open", fake_open, raising=False)


class TestVerifiedWrites:
    def test_round_trip_is_atomic_and_keeps_line_endings(self, tmp_path):
        out = tmp_path / "deep" / "doc.md"
        write_text_verified(out, "line one\nline two\n")
        assert out.read_text(encoding="utf-8") == "line one\nline two\n"
        assert out.read_bytes() == "line one\nline two\n".replace("\n", os.linesep).encode()
        assert [p.name for p in out.parent.iterdir()] == ["doc.md"]  # no temp file left

    def test_antivirus_block_on_read_back_is_ms_205(self, tmp_path, monkeypatch, fast_retries):
        def blocked(_):
            raise _winerror(225, "Operation did not complete successfully because the file contains a virus")

        _patch_reads(monkeypatch, blocked)
        with pytest.raises(SaveVerificationError) as info:
            write_text_verified(tmp_path / "book.md", "content\n")
        assert info.value.ms_code == "MS-205"
        assert errors.classify_os_error(info.value) == "MS-205"

    def test_transient_read_failure_is_retried(self, tmp_path, monkeypatch, fast_retries):
        calls = {"n": 0}

        def flaky(_):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError(22, "Invalid argument")  # OneDrive mid-sync
            return None

        _patch_reads(monkeypatch, flaky)
        write_text_verified(tmp_path / "book.md", "content\n")
        assert calls["n"] == 3

    def test_file_that_never_reads_back_is_ms_206(self, tmp_path, monkeypatch, fast_retries):
        def missing(path):
            raise FileNotFoundError(2, "No such file", str(path))

        _patch_reads(monkeypatch, missing)
        monkeypatch.setattr(export, "_defender_flagged", lambda p: False)
        with pytest.raises(SaveVerificationError) as info:
            write_text_verified(tmp_path / "book.md", "content\n")
        assert info.value.ms_code == "MS-206"
        assert "missing" in str(info.value)

    def test_vanished_file_that_defender_flagged_is_ms_205(self, tmp_path, monkeypatch, fast_retries):
        # What really happened to the library run's two security books:
        # Defender deleted each .md moments after it was written, so the
        # read-back found it missing, not a virus error code.
        def missing(path):
            raise FileNotFoundError(2, "No such file", str(path))

        _patch_reads(monkeypatch, missing)
        asked = []
        monkeypatch.setattr(export, "_defender_flagged", lambda p: asked.append(p) or True)
        with pytest.raises(SaveVerificationError) as info:
            write_text_verified(tmp_path / "Advanced Penetration Testing.md", "content\n")
        assert info.value.ms_code == "MS-205" and len(asked) == 1

    def test_defender_query_passes_the_path_out_of_band(self, tmp_path, monkeypatch):
        import subprocess

        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw["env"]
            return subprocess.CompletedProcess(cmd, 0, stdout="clean\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        weird = tmp_path / "x'; Remove-Item -Recurse C:\\ ; '.md"
        assert export._defender_flagged(weird) is False
        assert str(weird.resolve()) == seen["env"]["MS_SIDEKICK_SAVED"]
        assert all("Remove-Item" not in part for part in seen["cmd"])  # never in the command text

    def test_rename_retried_while_a_scanner_holds_the_file(self, tmp_path, monkeypatch, fast_retries):
        real_replace = os.replace
        calls = {"n": 0}

        def busy(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(13, "Access is denied")
            real_replace(src, dst)

        monkeypatch.setattr(export.os, "replace", busy)
        write_text_verified(tmp_path / "book.md", "content\n")
        assert calls["n"] == 3 and (tmp_path / "book.md").exists()

    def test_rename_still_refused_falls_back_to_writing_in_place(self, tmp_path, monkeypatch, fast_retries):
        def refused(src, dst):
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(export.os, "replace", refused)
        out = tmp_path / "book.md"
        out.write_text("old\n", encoding="utf-8")
        write_text_verified(out, "new\n")
        assert out.read_text(encoding="utf-8") == "new\n"
        assert [p.name for p in tmp_path.iterdir()] == ["book.md"]

    def test_virus_deleted_winerror_is_classified(self):
        assert errors.classify_os_error(_winerror(226)) == "MS-205"
        assert errors.classify_os_error(_winerror(32)) == "MS-204"


# ---------------------------------------------------------------------------
# The launcher: a flag never opens the GUI
# ---------------------------------------------------------------------------
class TestLauncher:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "app.py"), *args],
            capture_output=True, text=True, timeout=60, cwd=ROOT,
        )

    def test_help_prints_usage_and_exits(self):
        proc = self._run("--help")
        assert proc.returncode == 0
        assert "MarkdownSidekick-cli.exe" in proc.stdout

    def test_unknown_option_is_an_error_not_a_window(self):
        proc = self._run("--no-such-flag")
        assert proc.returncode == 2
        assert "Unknown option: --no-such-flag" in proc.stderr

    def test_version(self):
        from markdown_sidekick import __version__

        proc = self._run("--version")
        assert proc.returncode == 0 and __version__ in proc.stdout
