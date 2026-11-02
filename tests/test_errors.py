"""Tests for plain-language conversion-error explanations."""

from __future__ import annotations

import random

from markdown_sidekick.converter import ConversionEngine, explain_error


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
    """Byte salad that decodes as text — the shape markitdown 'converts' to
    the literal string 'None' (or a '| None |' table) without any error."""
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


class TestEmptyOutputGuard:
    """A non-trivial source 'converting' to no real text must be an error,
    not a silent success (markitdown emits str(None) for binary garbage)."""

    def _engine(self) -> ConversionEngine:
        return ConversionEngine(enable_ocr=False, enable_audio=False)

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
