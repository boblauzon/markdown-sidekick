"""Tests for plain-language conversion-error explanations."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from markdown_sidekick.converter import ConversionEngine, explain_error
from markdown_sidekick.quality import assess_markdown


class TestExplainError:
    def test_password_protected_pdf(self):
        what, fix = explain_error("PDFPasswordIncorrect: password required")
        assert "password" in what.lower()
        assert "retry" in fix.lower()

    def test_encrypted_variant(self):
        what, _ = explain_error("FileNotDecryptedError: File has not been decrypted")
        assert "password-protected or encrypted" in what

    def test_locked_file(self):
        what, fix = explain_error(
            "PermissionError: [WinError 32] The process cannot access the file "
            "because it is being used by another process"
        )
        assert "locked or in use" in what
        assert "close" in fix.lower()

    def test_missing_file(self):
        what, _ = explain_error("File does not exist.")
        assert "can't be found" in what

    def test_directory(self):
        what, fix = explain_error("Path is a directory, not a file.")
        assert "folder" in what
        assert "inside" in fix

    def test_unsupported_format(self):
        what, _ = explain_error("UnsupportedFormatException: .xyz is not supported")
        assert "file type" in what

    def test_corrupt_file(self):
        what, _ = explain_error("BadZipFile: File is not a zip file")
        assert "corrupt" in what

    def test_memory(self):
        what, _ = explain_error("MemoryError: ")
        assert "too large" in what

    def test_undecodable_media(self):
        what, _ = explain_error("InvalidDataError: moov atom not found")
        assert "decoded" in what

    def test_unknown_error_falls_back_generically(self):
        what, fix = explain_error("SomeNovelError: flux capacitor misaligned")
        assert "unexpected error" in what
        assert "technical details" in fix.lower()

    def test_priority_password_over_permission(self):
        # An encrypted-file error mentioning permissions should still read as
        # a password problem — hint order is most-specific first.
        what, _ = explain_error("PDFEncryptionError: permission denied, encrypted")
        assert "password-protected or encrypted" in what

    def test_filename_words_do_not_steer_diagnosis(self):
        # A locked file whose NAME contains "password" is a lock error, not
        # an encryption error — quoted paths are scrubbed before matching.
        what, fix = explain_error(
            "PermissionError: [Errno 13] Permission denied: "
            "'C:\\Users\\Rob\\passwords-export.xlsx'"
        )
        assert "locked or in use" in what
        assert "close" in fix.lower()

    def test_markitdown_wrapped_truncated_pdf(self):
        # The exact shape markitdown produces for a truncated PDF.
        what, _ = explain_error(
            "FileConversionException: File conversion failed after 1 attempts:\n"
            " - PdfConverter threw PSEOF with message: Unexpected EOF\n"
        )
        assert "corrupt" in what

    def test_empty_output(self):
        what, fix = explain_error(
            "EmptyOutputError: the converter produced no text from this 20,000-byte file"
        )
        assert "no text" in what
        assert "corrupt" in fix.lower()


def _textish_garbage(n: int) -> bytes:
    """Byte salad that looks like text — the shape markitdown 'converts' without
    any error: to whichever byte pairs happen to be valid UTF-8 (0.1.8+), or to
    the literal string 'None' / a '| None |' table (through 0.1.7)."""
    rng = random.Random(42)
    out = bytearray()
    for _ in range(n):
        r = rng.random()
        if r < 0.12:
            out.append(ord(" "))
        elif r < 0.14:
            out.append(ord("\n"))
        else:
            out.append(rng.randrange(0xA0, 0x100))
    return bytes(out)


def _salad_text() -> str:
    """What markitdown 0.1.8+ makes of `_textish_garbage`."""
    return _textish_garbage(20_000).decode("utf-8", errors="ignore")


def _stub_markitdown(monkeypatch, engine: ConversionEngine, output: str) -> None:
    monkeypatch.setattr(
        engine._md, "convert", lambda _path: SimpleNamespace(text_content=output)
    )


class TestEmptyOutputGuard:
    """A non-trivial source 'converting' to no real text must be an error, not
    a silent success (see the markitdown versions in `_textish_garbage`)."""

    def _engine(self) -> ConversionEngine:
        return ConversionEngine(enable_ocr=False, enable_audio=False)

    @pytest.mark.parametrize("output", ["None", "| None |\n| --- |\n| None |\n", ""])
    def test_str_none_leak_reports_error(self, tmp_path, monkeypatch, output):
        # requirements.txt still allows markitdown <= 0.1.7, and the real
        # library no longer reaches this shape, so it has to be stubbed.
        engine = self._engine()
        _stub_markitdown(monkeypatch, engine, output)
        p = tmp_path / "garbage.docx"
        p.write_bytes(_textish_garbage(20_000))
        res = engine.convert_file(p)
        assert res.error_code == "MS-106"

    @pytest.mark.parametrize(
        "ext, code",
        [
            (".txt", "MS-106"),
            (".docx", "MS-106"),
            (".csv", "MS-106"),
            (".xlsx", "MS-106"),
            (".epub", "MS-106"),
            (".bin", "MS-106"),
            (".pdf", "MS-110"),  # any textless PDF with OCR off: try OCR first
        ],
    )
    def test_garbage_is_an_error_under_any_extension(self, tmp_path, ext, code):
        p = tmp_path / f"garbage{ext}"
        p.write_bytes(_textish_garbage(20_000))
        res = self._engine().convert_file(p)
        assert not res.ok
        assert res.error_code == code

    @pytest.mark.parametrize("ext", [".txt", ".csv"])
    @pytest.mark.parametrize(
        "low, high", [(0, 0x100), (0x80, 0x100)], ids=["full-range", "high-bytes-no-spaces"]
    )
    def test_other_byte_ranges_report_error(self, tmp_path, ext, low, high):
        rng = random.Random(7)
        p = tmp_path / f"garbage{ext}"
        p.write_bytes(bytes(rng.randrange(low, high) for _ in range(20_000)))
        res = self._engine().convert_file(p)
        assert res.error_code == "MS-106"

    @pytest.mark.parametrize(
        "text, encoding",
        [
            ("Le coeur de l'été déjà passé, où êtes-vous allé à Noël ? ", "latin-1"),
            ("Это пример текста на русском языке для проверки детектора. ", "cp1251"),
            ("これはテストの文章です。日本語のカタカナとひらがな、漢字を混ぜます。\n", "shift_jis"),
            ("这是一段用于测试编码检测器的中文文本，内容没有特别的意义。\n", "gb18030"),
            ("A perfectly ordinary paragraph of plain English text. ", "utf-16"),
        ],
    )
    def test_real_text_in_legacy_encodings_stays_ok(self, tmp_path, text, encoding):
        # Not UTF-8, so these are the guard's likeliest false positives. Only
        # the verdict is checked: markitdown's charset detection can still pick
        # a neighbouring code page, which is not this guard's concern.
        p = tmp_path / "legacy.txt"
        p.write_bytes((text * 800).encode(encoding))
        res = self._engine().convert_file(p)
        assert res.ok
        assert not assess_markdown(res.markdown).binary_noise

    def test_valid_utf8_that_only_looks_noisy_keeps_its_warning(self, tmp_path, monkeypatch):
        # Every byte decoded, so nothing failed: this is what the file says.
        salad = _salad_text() * 4
        engine = self._engine()
        _stub_markitdown(monkeypatch, engine, salad)
        p = tmp_path / "salad.txt"
        p.write_text(salad, encoding="utf-8", newline="")
        assert p.stat().st_size >= 16_384
        res = engine.convert_file(p)
        assert res.ok
        assert assess_markdown(res.markdown).binary_noise

    @pytest.mark.parametrize(
        "name, magic",
        [
            ("real.pdf", b"%PDF-1.7\n"),
            ("real.docx", b"PK\x03\x04"),
            ("real.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
        ],
    )
    def test_real_container_with_garbled_text_keeps_its_warning(
        self, tmp_path, monkeypatch, name, magic
    ):
        # Real books with broken text layers reach noise_ratio >= 0.10, and most
        # of a damaged document can still be useful: warn, don't discard it.
        engine = self._engine()
        _stub_markitdown(monkeypatch, engine, _salad_text())
        p = tmp_path / name
        p.write_bytes(magic + _textish_garbage(20_000))
        res = engine.convert_file(p)
        assert res.ok
        assert assess_markdown(res.markdown).binary_noise

    def test_garbage_html_keeps_its_warning(self, tmp_path):
        # The HTML parser decodes anything, and a real legacy-encoded page can
        # come out just as noisy, so this tier only warns.
        rng = random.Random(7)
        p = tmp_path / "garbage.html"
        p.write_bytes(bytes(rng.randrange(256) for _ in range(20_000)))
        res = self._engine().convert_file(p)
        assert res.ok
        assert assess_markdown(res.markdown).binary_noise

    def test_small_garbage_stays_a_warning(self, tmp_path):
        # Below the size floor the guard stands aside, as it does for empty output.
        p = tmp_path / "small.txt"
        p.write_bytes(_textish_garbage(8_000))
        res = self._engine().convert_file(p)
        assert res.ok
        assert assess_markdown(res.markdown).binary_noise

    def test_garbage_docx_reports_error(self, tmp_path):
        p = tmp_path / "garbage.docx"
        p.write_bytes(_textish_garbage(20_000))
        res = self._engine().convert_file(p)
        assert not res.ok
        assert "EmptyOutputError" in (res.error or "")

    def test_garbage_txt_reports_error(self, tmp_path):
        p = tmp_path / "garbage.txt"
        p.write_bytes(_textish_garbage(20_000))
        res = self._engine().convert_file(p)
        assert not res.ok
        assert "produced no text" in (res.error or "")

    def test_small_empty_file_stays_ok(self, tmp_path):
        # Below the size floor an empty conversion is not evidence of
        # corruption — small-but-real empty documents pass through.
        p = tmp_path / "empty.txt"
        p.write_bytes(b"   \n\n  ")
        res = self._engine().convert_file(p)
        assert res.ok

    def test_real_text_stays_ok(self, tmp_path):
        p = tmp_path / "real.txt"
        p.write_bytes(("A perfectly ordinary paragraph of text.\n" * 500).encode())
        res = self._engine().convert_file(p)
        assert res.ok
        assert "ordinary paragraph" in res.markdown
