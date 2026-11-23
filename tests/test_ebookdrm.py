"""Store download tickets (.acsm) and DRM-protected epubs: recognised and
explained (MS-117 / MS-118), never converted to junk or called "corrupt".

The ticket is the shape Kobo issues (a real one, with its transaction and
HMAC replaced); the encryption.xml shapes are ADEPT's and the IDPF font
obfuscation many DRM-free epubs carry.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from markdown_sidekick import batch, ebookdrm, errors
from markdown_sidekick.batch import BatchOptions, run_batch
from markdown_sidekick.converter import ConversionEngine
from markdown_sidekick.dedupe import find_duplicates

from test_library_trust import _GROKKING, make_epub

ENGINE = {"enable_ocr": False, "enable_audio": False}

TICKET = """<fulfillmentToken fulfillmentType="buy" auth="user" xmlns="http://ns.adobe.com/adept">
  <distributor>urn:uuid:00000000-0000-0000-0000-000000000000</distributor>
  <operatorURL>https://acs4.kobo.com/fulfillment</operatorURL>
  <transaction>00000000-0000-0000-0000-000000000001</transaction>
  <purchase>2026-11-23T14:47:51+00:00</purchase>
  <expiration>2026-11-23T20:47:51+00:00</expiration>
  <resourceItemInfo>
    <resource>urn:uuid:00000000-0000-0000-0000-000000000002</resource>
    <resourceItem>0</resourceItem>
    <metadata>
      <dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">Your Brain's Not Broken</dc:title>
      <dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">Tamara Rosier, PhD</dc:creator>
      <dc:publisher xmlns:dc="http://purl.org/dc/elements/1.1/">Baker Publishing Group</dc:publisher>
      <dc:format xmlns:dc="http://purl.org/dc/elements/1.1/">application/epub+zip</dc:format>
    </metadata>
    <licenseToken><permissions><display/></permissions></licenseToken>
  </resourceItemInfo>
  <hmac>AAAA</hmac>
</fulfillmentToken>
"""


def _encryption(algorithm: str, uri: str) -> str:
    return f"""<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="{algorithm}"/>
    <enc:CipherData><enc:CipherReference URI="{uri}"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>"""


def _with(epub: Path, **extra: str) -> Path:
    with zipfile.ZipFile(epub, "a") as zf:
        for name, text in extra.items():
            zf.writestr("META-INF/" + name.replace("_", "."), text)
    return epub


def adept_epub(path: Path) -> Path:
    return _with(
        make_epub(path, _GROKKING),
        rights_xml="<adept:rights xmlns:adept='http://ns.adobe.com/adept'/>",
        encryption_xml=_encryption("http://www.w3.org/2001/04/xmlenc#aes128-cbc", "OEBPS/c1.xhtml"),
    )


def _pdf_with_encrypt(path: Path, encrypt: str, *, at_start: bool = False) -> Path:
    """A PDF whose encryption dictionary uses ``encrypt`` (its /Filter) —
    in an incremental update at the end, or up front as a linearized
    file has it, with a megabyte of page content between."""
    enc = f"9 0 obj\n<< {encrypt} /V 3 /R 3 /Length 128 >>\nendobj\n".encode()
    body = b"1 0 obj\n<< /Type /Catalog >>\nendobj\n" + b"% filler\n" * 120_000
    parts = [b"%PDF-1.6\n", enc, body] if at_start else [b"%PDF-1.6\n", body, enc]
    parts.append(b"trailer\n<< /Root 1 0 R /Encrypt 9 0 R >>\n%%EOF\n")
    path.write_bytes(b"".join(parts))
    return path


def adept_pdf(path: Path) -> Path:
    return _pdf_with_encrypt(path, "/Filter /EBX_HANDLER /ADEPT_LICENSE (abc)")


class TestPdfDrm:
    def test_handlers(self, tmp_path):
        assert ebookdrm.drm_scheme(adept_pdf(tmp_path / "a.pdf")) == "Adobe"
        assert ebookdrm.drm_scheme(_pdf_with_encrypt(tmp_path / "l.pdf", "/Filter /EBX_HANDLER", at_start=True)) == "Adobe"
        assert ebookdrm.drm_scheme(_pdf_with_encrypt(tmp_path / "f.pdf", "/Filter/FOPN_fLock")) == "FileOpen"
        assert ebookdrm.drm_scheme(_pdf_with_encrypt(tmp_path / "s.pdf", "/Filter /Adobe.APS")) == "Adobe LiveCycle"

    def test_a_password_is_not_drm(self, tmp_path):
        assert ebookdrm.drm_scheme(_pdf_with_encrypt(tmp_path / "p.pdf", "/Filter /Standard")) == ""

    def test_the_name_in_page_text_is_not_drm(self, tmp_path):
        p = tmp_path / "t.pdf"
        p.write_bytes(b"%PDF-1.4\n(Adobe's EBX_HANDLER protects books) Tj\n"
                      b"(/Filter /EBX_HANDLER) Tj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n")
        assert ebookdrm.drm_scheme(p) == ""

    def test_junk_before_the_header_is_still_checked(self, tmp_path):
        p = adept_pdf(tmp_path / "j.pdf")
        p.write_bytes(b"\r\n\x00junk\n" + p.read_bytes())
        assert ebookdrm.drm_scheme(p) == "Adobe"

    def test_converts_to_ms118_not_ms101(self, tmp_path):
        result = ConversionEngine(**ENGINE).convert_file(adept_pdf(tmp_path / "locked.pdf"))
        assert not result.ok and result.error_code == "MS-118" and "Adobe" in result.error

    def test_real_pdf_is_readable(self, tmp_path):
        from pdfgen import make_pdf

        p = tmp_path / "real.pdf"
        p.write_bytes(make_pdf(["A normal page of text."]))
        assert ebookdrm.drm_scheme(p) == ""


class TestTicket:
    def test_reads_the_book_it_is_for(self, tmp_path):
        t = tmp_path / "URLLink.acsm"
        t.write_text(TICKET, encoding="utf-8")
        assert ebookdrm.read_ticket(t) == {
            "title": "Your Brain's Not Broken",
            "author": "Tamara Rosier, PhD",
            "publisher": "Baker Publishing Group",
            "store": "acs4.kobo.com",
            "expires": "2026-11-23T20:47:51+00:00",
        }

    def test_other_xml_is_not_a_ticket(self, tmp_path):
        x = tmp_path / "data.xml"
        x.write_text("<root><a>1</a></root>", encoding="utf-8")
        assert ebookdrm.read_ticket(x) is None and not ebookdrm.is_ticket(x)

    def test_converts_to_ms117_naming_the_book(self, tmp_path):
        t = tmp_path / "URLLink.acsm"
        t.write_text(TICKET, encoding="utf-8")
        result = ConversionEngine(**ENGINE).convert_file(t)
        assert not result.ok and result.error_code == "MS-117"
        assert "Your Brain's Not Broken" in result.error and "acs4.kobo.com" in result.error
        assert result.doc_title == "Your Brain's Not Broken"

    def test_a_ticket_saved_as_xml_is_still_recognised(self, tmp_path):
        t = tmp_path / "ticket.xml"
        t.write_text(TICKET, encoding="utf-8")
        assert ConversionEngine(**ENGINE).convert_file(t).error_code == "MS-117"

    def test_a_junk_acsm_still_gets_the_explanation(self, tmp_path):
        t = tmp_path / "broken.acsm"
        t.write_bytes(b"\x00\x01 not xml")
        result = ConversionEngine(**ENGINE).convert_file(t)
        assert result.error_code == "MS-117"


class TestDrm:
    def test_plain_epub_is_readable(self, tmp_path):
        assert ebookdrm.drm_scheme(make_epub(tmp_path / "b.epub", _GROKKING)) == ""

    def test_font_obfuscation_is_not_drm(self, tmp_path):
        for alg in ("http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"):
            epub = _with(make_epub(tmp_path / f"f{len(alg)}.epub", _GROKKING),
                         encryption_xml=_encryption(alg, "OEBPS/fonts/a.otf"))
            assert ebookdrm.drm_scheme(epub) == ""
            assert ConversionEngine(**ENGINE).convert_file(epub).ok

    def test_schemes(self, tmp_path):
        assert ebookdrm.drm_scheme(adept_epub(tmp_path / "a.epub")) == "Adobe"
        lcp = _with(make_epub(tmp_path / "l.epub", _GROKKING), license_lcpl="{}",
                    encryption_xml=_encryption("http://www.w3.org/2001/04/xmlenc#aes256-cbc", "OEBPS/c1.xhtml"))
        assert ebookdrm.drm_scheme(lcp) == "Readium LCP"
        other = _with(make_epub(tmp_path / "o.epub", _GROKKING),
                      encryption_xml=_encryption("http://www.w3.org/2001/04/xmlenc#aes256-cbc", "OEBPS/c1.xhtml"))
        assert ebookdrm.drm_scheme(other) == "unknown"

    def test_a_marker_file_without_encrypted_text_is_readable(self, tmp_path):
        epub = _with(make_epub(tmp_path / "m.epub", _GROKKING),
                     rights_xml="<adept:rights xmlns:adept='http://ns.adobe.com/adept'/>")
        assert ebookdrm.drm_scheme(epub) == ""
        assert ConversionEngine(**ENGINE).convert_file(epub).ok

    def test_encrypted_fonts_alone_leave_the_text_readable(self, tmp_path):
        epub = _with(make_epub(tmp_path / "f.epub", _GROKKING),
                     encryption_xml=_encryption("http://www.w3.org/2001/04/xmlenc#aes128-cbc", "OEBPS/fonts/a.OTF"))
        assert ebookdrm.drm_scheme(epub) == ""

    def test_a_corrupt_encryption_entry_never_raises(self, tmp_path):
        epub = make_epub(tmp_path / "c.epub", _GROKKING)
        with zipfile.ZipFile(epub, "a", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("META-INF/encryption.xml", "<encryption>" + "x" * 5000 + "</encryption>")
        raw = bytearray(epub.read_bytes())
        start = raw.rfind(b"META-INF/encryption.xml", 0, raw.rfind(b"PK\x01\x02")) + len("META-INF/encryption.xml")
        raw[start + 10:start + 40] = b"\xff" * 30  # break the deflate stream, keep the directory
        epub.write_bytes(bytes(raw))
        assert ebookdrm.drm_scheme(epub) == ""
        result = ConversionEngine(**ENGINE).convert_file(epub)  # never raises
        assert result.error_code != "MS-118"

    def test_not_a_zip_is_left_to_the_routes(self, tmp_path):
        bad = tmp_path / "bad.epub"
        bad.write_bytes(b"not a zip at all" * 100)
        assert ebookdrm.drm_scheme(bad) == ""

    def test_converts_to_ms118_not_ms101(self, tmp_path):
        result = ConversionEngine(**ENGINE).convert_file(adept_epub(tmp_path / "locked.epub"))
        assert not result.ok and result.error_code == "MS-118"
        assert "Adobe" in result.error

    def test_classification_beats_encrypt(self):
        assert errors.classify_conversion("DrmProtectedError: encrypted (Adobe DRM)") == "MS-118"
        assert errors.classify_conversion("DownloadTicketError: x") == "MS-117"


class TestLibrary:
    def test_ticket_is_a_skipped_row_naming_the_book(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "URLLink.acsm").write_text(TICKET, encoding="utf-8")
        make_epub(lib / "grokking.epub", _GROKKING)
        out = tmp_path / "out"
        summary = run_batch([lib], BatchOptions(output_root=out, engine=ENGINE))
        assert (summary.converted, summary.by_reason) == (1, {"download_ticket": 1})
        rows = json.loads((out / batch.MANIFEST_NAME).read_text(encoding="utf-8"))["rows"]
        row = next(r for r in rows if r["source"].endswith(".acsm"))
        assert (row["code"], row["title"], row["authors"]) == (
            "MS-117", "Your Brain's Not Broken", ["Tamara Rosier, PhD"])

    def test_a_ticket_is_never_the_kept_copy_of_its_book(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "Grokking_Algorithms_book.acsm").write_text(TICKET, encoding="utf-8")
        adept_epub(lib / "Grokking_Algorithms_book.epub")
        out = tmp_path / "out"
        summary = run_batch([lib], BatchOptions(output_root=out, engine=ENGINE))
        assert summary.by_reason == {"download_ticket": 1, "drm_protected": 1}

    def test_an_oversized_acsm_is_not_read_whole(self, tmp_path):
        big = tmp_path / "video.acsm"
        big.write_bytes(b"<fulfillmentToken>" + b" " * (2 << 20))
        assert ebookdrm.read_ticket(big) is None
        assert ConversionEngine(**ENGINE).convert_file(big).error_code == "MS-117"

    def test_locked_books_are_skipped_rows_not_failures(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        adept_epub(lib / "locked.epub")
        adept_pdf(lib / "other.pdf")
        out = tmp_path / "out"
        summary = run_batch([lib], BatchOptions(output_root=out, engine=ENGINE))
        assert (summary.failed, summary.by_reason) == (0, {"drm_protected": 2})
        rows = json.loads((out / batch.MANIFEST_NAME).read_text(encoding="utf-8"))["rows"]
        epub = next(r for r in rows if r["source"].endswith(".epub"))
        assert (epub["code"], epub["title"]) == ("MS-118", "Grokking Algorithms")
        # Still skipped (not "unchanged", not failed) on the next run.
        again = run_batch([lib], BatchOptions(output_root=out, engine=ENGINE))
        assert again.by_reason == {"drm_protected": 2}

    def test_a_locked_book_made_drm_free_converts_next_run(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        book = adept_epub(lib / "book.epub")
        out = tmp_path / "out"
        run_batch([lib], BatchOptions(output_root=out, engine=ENGINE))
        make_epub(book, _GROKKING)  # replaced by the DRM-free download
        assert run_batch([lib], BatchOptions(output_root=out, engine=ENGINE)).converted == 1

    def test_dedupe_keeps_the_drm_free_copy(self, tmp_path):
        locked = adept_epub(tmp_path / "a.epub")  # same ISBN, shorter path, preferred format
        free = make_epub((tmp_path / "zz").mkdir() or tmp_path / "zz" / "grokking-free.epub", _GROKKING)
        assert find_duplicates([locked, free]) == {locked: free}
