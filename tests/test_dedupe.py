"""One copy per book (dedupe.py + batch integration).

The reference library held 993 ebook files for about 500 books: epub and
pdf of the same title, the same file in two bundle folders, ``(1)`` copies.
On it, these rules collapse 993 files to 485 books with no false merges,
after two real near-misses that are encoded below: a book's fifth and sixth
editions (edition only in a run-together filename) and a study guide's
volumes 1 and 2 (same title).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from markdown_sidekick import batch
from markdown_sidekick.batch import BatchOptions, plan_batch, run_batch
from markdown_sidekick.dedupe import edition_of, find_duplicates

from pdfgen import Text, make_layout_pdf
from test_library_trust import _GROKKING, make_epub

ENGINE = {"enable_ocr": False, "enable_audio": False}


def _epub(path: Path, title: str, author: str = "Ada Lovelace", isbn: str = "", subtitle: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    xml = f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>"
    if subtitle:
        xml = f'<dc:title id="t1">{title}</dc:title><dc:title id="t2">{subtitle}</dc:title>' \
              '<meta refines="#t1" property="title-type">main</meta>' \
              '<meta refines="#t2" property="title-type">subtitle</meta>' \
              f"<dc:creator>{author}</dc:creator>"
    if isbn:
        xml += f'<dc:identifier opf:scheme="ISBN">{isbn}</dc:identifier>'
    # Real editions never share bytes; identical files ARE always merged.
    xml += f"<dc:description>{path.name}</dc:description>"
    return make_epub(path, xml)


def _pdf(path: Path, title: str | None, body: str = "Body text of a real page.") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_layout_pdf([[Text(body, 72, 700)]], title=title))
    return path


def _dupes(paths, **kw) -> dict[str, str]:
    return {p.name: k.name for p, k in find_duplicates(list(paths), **kw).items()}


class TestEditionsAndVolumes:
    def test_edition_of(self):
        cases = {
            "C# in Depth, Fourth Edition": "4",
            "Python Crash Course 3rd ed.": "3",
            "reactandreactnative sixthedition": "6",
            "moderncomputervisionwithpytorch2e": "2",
            "ccnacertificationstudyguidevolume1 exam200-301": "v1",
            "Study Guide, Vol. II": "v2",
            "Edited by Someone": "",
            "Plain Title": "",
        }
        for text, expected in cases.items():
            assert edition_of(text) == expected, text


class TestFindDuplicates:
    def test_epub_and_pdf_of_one_book(self, tmp_path):
        e = _epub(tmp_path / "a" / "grokking_algorithms.epub", "Grokking Algorithms")
        p = _pdf(tmp_path / "b" / "GrokkingAlgorithms.pdf", "Grokking Algorithms: An illustrated guide")
        assert _dupes([p, e]) == {p.name: e.name}

    def test_exact_copies_in_two_folders(self, tmp_path):
        a = _pdf(tmp_path / "Bundle A" / "x.pdf", None, "Unique content one")
        b = tmp_path / "Bundle B" / "y.pdf"
        b.parent.mkdir()
        shutil.copy(a, b)
        assert len(_dupes([a, b])) == 1

    def test_editions_are_never_merged(self, tmp_path):
        third = _epub(tmp_path / "cs3.epub", "C# in Depth", "Jon Skeet", subtitle="Third Edition")
        fourth = _epub(tmp_path / "cs4.epub", "C# in Depth", "Jon Skeet", subtitle="Fourth Edition")
        assert _dupes([third, fourth]) == {}

    def test_edition_only_in_a_run_together_filename(self, tmp_path):
        five = _epub(tmp_path / "reactandreactnative_fifthedition.epub", "React and React Native")
        six = _epub(tmp_path / "reactandreactnative_sixthedition.epub", "React and React Native")
        six_pdf = _pdf(tmp_path / "reactandreactnative_sixthedition.pdf", None, "other bytes")
        assert _dupes([five, six, six_pdf]) == {six_pdf.name: six.name}

    def test_volumes_are_never_merged(self, tmp_path):
        v1 = _epub(tmp_path / "ccnastudyguidevolume1_exam.epub", "CCNA Certification Study Guide")
        v2 = _epub(tmp_path / "ccnastudyguidevolume2_exam.epub", "CCNA Certification Study Guide")
        assert _dupes([v1, v2]) == {}

    def test_same_title_different_authors(self, tmp_path):
        a = _epub(tmp_path / "a.epub", "Introduction to Algorithms", "Thomas Cormen")
        b = _epub(tmp_path / "b.epub", "Introduction to Algorithms", "Udi Manber")
        assert _dupes([a, b]) == {}

    def test_copy_suffix_filenames(self, tmp_path):
        a = _pdf(tmp_path / "wiresharkforsecurityprofessionals.pdf", None, "first bytes")
        b = _pdf(tmp_path / "wiresharkforsecurityprofessionals (1).pdf", None, "second bytes differ")
        assert _dupes([a, b]) == {b.name: a.name}

    def test_short_generic_names_are_not_evidence(self, tmp_path):
        a = _pdf(tmp_path / "One" / "manual.pdf", None, "manual for a dishwasher")
        b = _pdf(tmp_path / "Two" / "manual.pdf", None, "manual for a car")
        assert _dupes([a, b]) == {}

    def test_preference_and_unreadable_formats(self, tmp_path):
        e = _epub(tmp_path / "practicallinuxadministration.epub", "Practical Linux Administration")
        p = _pdf(tmp_path / "practicallinuxadministration.pdf", None)
        m = tmp_path / "practicallinuxadministration.mobi"
        m.write_bytes(b"BOOKMOBI")
        assert _dupes([m, p, e]) == {m.name: e.name, p.name: e.name}
        assert _dupes([m, p, e], prefer=(".pdf", ".epub")) == {m.name: p.name, e.name: p.name}
        # A format nothing reads is never kept over one that is read.
        assert _dupes([m, p], prefer=(".mobi", ".pdf")) == {m.name: p.name}

    def test_keep_hint_makes_reruns_stable(self, tmp_path):
        import os

        a = _epub(tmp_path / "Short" / "book-one-title.epub", "Book One Title")
        b = tmp_path / "A Much Longer Folder" / "book-one-title.epub"
        b.parent.mkdir()
        shutil.copy(a, b)
        assert find_duplicates([a, b]) == {b: a}  # the shorter path wins by default
        kept = find_duplicates([a, b], keep_hint={os.path.normcase(str(b.resolve()))})
        assert kept == {a: b}


class TestBatchIntegration:
    def _library(self, tmp_path) -> Path:
        lib = tmp_path / "lib"
        make_epub((lib / "EPUB").mkdir(parents=True) or lib / "EPUB" / "grokkingalgorithms.epub", _GROKKING)
        _pdf(lib / "PDF" / "grokkingalgorithms.pdf", "Grokking Algorithms")
        (lib / "PDF" / "grokkingalgorithms.mobi").write_bytes(b"BOOKMOBI")
        return lib

    def test_duplicates_become_rows_naming_the_kept_copy(self, tmp_path):
        lib = self._library(tmp_path)
        out = tmp_path / "out"
        summary = run_batch([lib], BatchOptions(output_root=out, engine=ENGINE, naming="title"))
        assert (summary.total, summary.converted, summary.skipped) == (3, 1, 2)
        assert summary.by_reason == {"duplicate": 2}
        rows = json.loads((out / batch.MANIFEST_NAME).read_text(encoding="utf-8"))["rows"]
        dup = [r for r in rows if r["reason"] == "duplicate"]
        assert all(r["duplicate_of"].endswith("grokkingalgorithms.epub") for r in dup)
        assert [p.name for p in out.rglob("*.md")] == ["Grokking Algorithms.md"]

    def test_no_dedupe_converts_every_copy(self, tmp_path):
        lib = self._library(tmp_path)
        summary = run_batch([lib], BatchOptions(output_root=tmp_path / "o", engine=ENGINE, dedupe=False))
        assert summary.converted == 2 and summary.by_reason == {"unsupported_format": 1}

    def test_dry_run_writes_nothing(self, tmp_path):
        lib = self._library(tmp_path)
        out = tmp_path / "out"
        plan = plan_batch([lib], BatchOptions(output_root=out, engine=ENGINE))
        assert (plan["total"], plan["to_convert"]) == (3, 1)
        assert plan["skipped"] == {"duplicate": 2}
        assert not out.exists()
