"""Store download tickets (.acsm) and copy-protected (DRM) epubs and PDFs.

Neither can be converted, and neither should fail as a vague "no text" or
"corrupt file": an .acsm is Adobe's download ticket for a book bought from
Kobo, Google Play, a library…, not the book itself; and the epub or PDF that
ticket downloads is encrypted for one reader app. Markdown Sidekick does not
fetch the book (that registers a device with Adobe and sends the ticket off
the computer) and does not remove DRM — it says what the file is and what to do.

Stdlib only, and cheap: a ticket is a few KB of XML; the epub check reads
only a zip's directory and its small META-INF files, the PDF check only the
first 64 KB and last 256 KB, never the content. Conservative like cleanup:
a readable book wrongly refused is worse than a locked one reported as
MS-101/107, so every check needs positive evidence of encrypted content,
and nothing here raises — an unreadable file is the routes' to diagnose.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from . import debuglog

TICKET_EXTENSIONS = frozenset({".acsm"})

_ADEPT_NS = "http://ns.adobe.com/adept"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_ENC_NS = "http://www.w3.org/2001/04/xmlenc#"
_TICKET_MAX_BYTES = 1 << 20  # real tickets are ~2 KB

# Font obfuscation is not DRM: the IDPF and Adobe algorithms only mangle the
# first bytes of an embedded font so it can't be lifted out of the book. The
# text stays readable, and markitdown reads such epubs fine.
_FONT_OBFUSCATION = frozenset({
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
})
# A META-INF file that only a DRM scheme writes -> the scheme's name.
_DRM_MARKERS = {
    "meta-inf/rights.xml": "Adobe",  # ADEPT: Kobo, Google Play, libraries, B&N
    "meta-inf/license.lcpl": "Readium LCP",
    "meta-inf/sinf.xml": "Apple FairPlay",
}


def _text(el: ET.Element | None) -> str:
    return re.sub(r"\s+", " ", (el.text or "")).strip() if el is not None else ""


def read_ticket(path: str | Path) -> dict | None:
    """The title/author/publisher/store a download ticket names, or None
    when the file is not an Adobe fulfilment ticket."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(_TICKET_MAX_BYTES + 1)
        if len(data) > _TICKET_MAX_BYTES:
            return None
        root = ET.fromstring(data)
    except (OSError, ET.ParseError, ValueError):
        return None
    if root.tag != f"{{{_ADEPT_NS}}}fulfillmentToken":
        return None
    meta = root.find(f".//{{{_ADEPT_NS}}}metadata")
    operator = _text(root.find(f"{{{_ADEPT_NS}}}operatorURL"))
    host = re.sub(r"^https?://([^/:]+).*$", r"\1", operator) if operator else ""
    return {
        "title": _text(meta.find(f"{{{_DC_NS}}}title")) if meta is not None else "",
        "author": _text(meta.find(f"{{{_DC_NS}}}creator")) if meta is not None else "",
        "publisher": _text(meta.find(f"{{{_DC_NS}}}publisher")) if meta is not None else "",
        "store": host,
        "expires": _text(root.find(f"{{{_ADEPT_NS}}}expiration")),
    }


def is_ticket(path: str | Path) -> bool:
    """An .acsm by name, or any small file that parses as one."""
    p = Path(path)
    if p.suffix.lower() in TICKET_EXTENSIONS:
        return True
    try:
        if p.stat().st_size > _TICKET_MAX_BYTES:
            return False
        with open(p, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return False
    return b"<fulfillmentToken" in head and read_ticket(p) is not None


DRM_EXTENSIONS = frozenset({".epub", ".pdf"})

# A PDF's encryption dictionary names its security handler in /Filter.
# /Standard is an ordinary password (MS-101); these are DRM, whose key is
# held by a server or a reader app. The dictionary may not sit in a
# compressed object stream (PDF 32000 §7.5.7), so its name is plain text —
# near the end (after an incremental update) or the start (linearized).
_PDF_HANDLERS = {
    b"EBX_HANDLER": "Adobe",  # ADEPT: what an .acsm for a PDF delivers
    b"Adobe.APS": "Adobe LiveCycle",
    b"FOPN_": "FileOpen",
}
_PDF_FILTER_RE = re.compile(rb"/Filter\s*/(EBX_HANDLER|Adobe\.APS|FOPN_[A-Za-z0-9_]*)")
# Only an encrypted document's trailer (or xref-stream dictionary) points at
# an encryption dictionary; a handler name without this is just text.
_PDF_ENCRYPT_REF_RE = re.compile(rb"/Encrypt\s+\d+\s+\d+\s+R")
_PDF_HEAD = 64 << 10
_PDF_TAIL = 256 << 10


def _pdf_drm(path: str | Path) -> str:
    with open(path, "rb") as fh:
        head = fh.read(_PDF_HEAD)
        size = fh.seek(0, 2)
        fh.seek(max(len(head), size - _PDF_TAIL))
        tail = fh.read()
    # Readers accept the header anywhere in the first 1024 bytes.
    if b"%PDF-" not in head[:1024]:
        return ""
    if not (_PDF_ENCRYPT_REF_RE.search(tail) or _PDF_ENCRYPT_REF_RE.search(head)):
        return ""
    for chunk in (tail, head):
        if m := _PDF_FILTER_RE.search(chunk):
            name = m.group(1)
            return next(s for k, s in _PDF_HANDLERS.items() if name.startswith(k))
    return ""


_FONT_SUFFIXES = (".otf", ".ttf", ".woff", ".woff2", ".odttf")


def _epub_drm(path: str | Path) -> str:
    with zipfile.ZipFile(path) as zf:
        names = {n.lower(): n for n in zf.namelist()}
        # The marker files only NAME the scheme: a book that still carries
        # one but whose text isn't encrypted is readable.
        named = next((s for m, s in _DRM_MARKERS.items() if m in names), "")
        enc = names.get("meta-inf/encryption.xml")
        if enc is None:
            return ""
        if zf.getinfo(enc).file_size > 4 << 20:  # a book's worth of entries, not more
            return named or "unknown"
        root = ET.fromstring(zf.read(enc))
    for data in root.iter(f"{{{_ENC_NS}}}EncryptedData"):
        method = data.find(f"{{{_ENC_NS}}}EncryptionMethod")
        if method is not None and method.get("Algorithm", "") in _FONT_OBFUSCATION:
            continue
        ref = data.find(f".//{{{_ENC_NS}}}CipherReference")
        uri = (ref.get("URI", "") if ref is not None else "").lower()
        if uri.endswith(_FONT_SUFFIXES):
            continue  # an encrypted font: the text still reads
        return named or "unknown"
    return ""


def drm_scheme(path: str | Path) -> str:
    """The DRM scheme locking an epub or PDF ("Adobe", "Readium LCP",
    "Apple FairPlay", "FileOpen"…, or "unknown" for an epub's encrypted
    content of another scheme), or "" when the book is readable — including
    when it is not a zip/PDF at all, so the normal routes report what is
    actually wrong with it. A password-protected PDF is not DRM (MS-101)."""
    ext = Path(path).suffix.lower()
    check = {".pdf": _pdf_drm, ".epub": _epub_drm}.get(ext)
    if check is None:
        return ""
    try:
        return check(path)
    except Exception as exc:  # corrupt deflate, deflate64, truncated, locked…
        # Callers run outside convert_file's guard (and in dedupe/batch
        # planning): never raise; the routes report what is really wrong.
        debuglog.exception("ebookdrm.check", exc, path=str(path))
        return ""
