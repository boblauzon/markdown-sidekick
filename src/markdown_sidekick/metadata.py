"""Book metadata read from the source file itself (stdlib only).

markitdown prints an epub's OPF metadata into the body as ``**Title:** …``
lines but hands back only the title, so the export layer used to fall back to
the filename (``Grokking_Algorithms``) for every epub. This module reads the
OPF package document directly and returns the fields the front matter needs.

Every reader returns a plain dict with any of ``title``, ``subtitle``,
``authors`` (list), ``publisher``, ``date``, ``language`` and ``identifier``
— keys whose value is unknown are left out — and never raises: metadata is a
nicety, so a damaged package yields ``{}`` and the conversion goes on.

``title_source`` values written to front matter (see :func:`export.resolve_title`):
``opf`` (epub package metadata), ``pdf_info`` (the PDF's document info),
``title_page`` (a PDF's title, copyright or first page), ``heading`` (the
document's first heading) and ``filename``.
"""

from __future__ import annotations

import html
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import debuglog

_DC = "{http://purl.org/dc/elements/1.1/}"
_OPF = "{http://www.idpf.org/2007/opf}"
_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
# An OPF is a few KB; anything this large is not one worth parsing.
_MAX_OPF_BYTES = 4 * 1024 * 1024

_ISBN_RE = re.compile(r"(?:urn:isbn:|isbn[:\s]*)?((?:97[89][\s-]?)?(?:\d[\s-]?){9}[\dXx])\b", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?")
_PLACEHOLDER_RE = re.compile(r"^(?:unknown|untitled|n/?a|none|-+)(?:\s+author)?$", re.IGNORECASE)
# "Title: Subtitle" — a colon followed by a space. Dashes are not split on:
# they are as often part of a title ("Real-World Go") as a separator.
_SUBTITLE_SPLIT_RE = re.compile(r"\s*:\s+")


def clean_value(value: str | None) -> str:
    """A metadata string with entities decoded and whitespace collapsed.

    OPF files are often double-encoded (``CCNA&amp;#174;``), so the XML
    parser leaves ``&#174;`` behind; decoding once more gives ``CCNA®``.
    """
    if not value:
        return ""
    text = html.unescape(html.unescape(value))
    text = re.sub(r"\s+", " ", text).strip()
    return "" if _PLACEHOLDER_RE.match(text) else text


# Several names packed into one creator field: "A | B", "A;B", "A & B", "A and B".
_AUTHOR_SEP_RE = re.compile(r"\s*[|;]\s*|\s+&\s+|\s+and\s+", re.IGNORECASE)
# Packt writes "Title_Third Edition" / "Title _Second Edition" in its OPFs.
_EDITION_UNDERSCORE_RE = re.compile(
    r"\s*_\s*(?=(?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|seventh|"
    r"eighth|ninth|tenth|revised|updated|new)\b[^_]*\bedition\b)",
    re.IGNORECASE,
)


def split_authors(names: list[str]) -> list[str]:
    """One entry per person, from creator fields that may each pack several.

    A comma separates people only when every part is itself a full name (two
    or more words): "Bas Meijer, Lorin Hochstein" is two people, while
    "Samara, Timothy" (surname first) and "John Smith, Jr." are one.
    """
    out: list[str] = []
    for raw in names:
        for part in _AUTHOR_SEP_RE.split(raw):
            pieces = [p.strip() for p in part.split(",")]
            if len(pieces) > 1 and all(len(p.split()) >= 2 for p in pieces):
                candidates = pieces
            else:
                candidates = [part.strip()]
            for name in candidates:
                name = clean_value(name)
                if name and name not in out:
                    out.append(name)
    return out


def tidy_title(title: str) -> str:
    """Publisher quirks in otherwise good titles: ``Title_Third Edition`` ->
    ``Title, Third Edition``."""
    return _EDITION_UNDERSCORE_RE.sub(", ", title).strip()


def split_subtitle(title: str) -> tuple[str, str]:
    """``"Grokking Algorithms: An illustrated guide"`` -> main title and
    subtitle. A title without ``": "`` (or with an empty side) is kept whole."""
    parts = _SUBTITLE_SPLIT_RE.split(title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return title, ""


def read_metadata(path: Path) -> dict:
    """Metadata for any source this module understands (currently epub)."""
    if path.suffix.lower() == ".epub":
        return epub_metadata(path)
    return {}


def epub_metadata(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            opf_name = _opf_path(zf)
            if not opf_name:
                return {}
            info = zf.getinfo(opf_name)
            if info.file_size > _MAX_OPF_BYTES:
                return {}
            root = ET.fromstring(zf.read(info))
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as exc:
        debuglog.exception("metadata.epub", exc, path=str(path))
        return {}
    meta = _find_local(root, "metadata")
    if meta is None:
        return {}
    return _from_opf_metadata(meta, root)


def _opf_path(zf: zipfile.ZipFile) -> str:
    try:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        for rootfile in container.iter(f"{_CONTAINER}rootfile"):
            full = rootfile.get("full-path", "")
            if full:
                return posixpath.normpath(full)
    except (KeyError, ET.ParseError):
        pass  # no (or a broken) container: look for the package directly
    return next((n for n in zf.namelist() if n.lower().endswith(".opf")), "")


def _find_local(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem.iter():
        if isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1] == name:
            return child
    return None


def _from_opf_metadata(meta: ET.Element, root: ET.Element) -> dict:
    # EPUB 3 qualifies elements through <meta refines="#id" property=…>.
    refines: dict[tuple[str, str], str] = {}
    for m in meta:
        if isinstance(m.tag, str) and m.tag.rsplit("}", 1)[-1] == "meta":
            target, prop = m.get("refines", ""), m.get("property", "")
            if target.startswith("#") and prop:
                refines[(target[1:], prop)] = (m.text or "").strip()

    def refined(elem: ET.Element, prop: str) -> str:
        return refines.get((elem.get("id", ""), prop), "")

    out: dict = {}

    titles = list(meta.iter(f"{_DC}title"))
    main = next((t for t in titles if refined(t, "title-type") == "main"), None)
    main = main if main is not None else (titles[0] if titles else None)
    subtitle_el = next((t for t in titles if refined(t, "title-type") == "subtitle"), None)
    title = tidy_title(clean_value(main.text if main is not None else ""))
    subtitle = tidy_title(clean_value(subtitle_el.text if subtitle_el is not None else ""))
    if title and not subtitle:
        title, subtitle = split_subtitle(title)
    if title:
        out["title"] = title
    if subtitle:
        out["subtitle"] = subtitle

    creators = list(meta.iter(f"{_DC}creator"))

    def role(elem: ET.Element) -> str:
        return (elem.get(f"{_OPF}role") or refined(elem, "role") or "").lower()

    authors = [c for c in creators if role(c) in ("", "aut")] or creators
    names = split_authors([clean_value(c.text) for c in authors])
    if names:
        out["authors"] = names

    for key in ("publisher", "language"):
        el = meta.find(f"{_DC}{key}")
        value = clean_value(el.text if el is not None else "")
        if value:
            out[key] = value

    dates = list(meta.iter(f"{_DC}date"))
    pub = next((d for d in dates if (d.get(f"{_OPF}event") or "").lower() == "publication"), None)
    date_el = pub if pub is not None else (dates[0] if dates else None)
    if date_el is not None and (m := _DATE_RE.match(clean_value(date_el.text))):
        out["date"] = m.group(0)

    identifier = _pick_identifier(list(meta.iter(f"{_DC}identifier")), root.get("unique-identifier", ""))
    if identifier:
        out["identifier"] = identifier
    return out


def _pick_identifier(ids: list[ET.Element], unique_id: str) -> str:
    """An ISBN when the package has one (it is what matches editions and
    duplicates across formats), else the package's unique identifier."""
    for el in ids:
        text = clean_value(el.text)
        scheme = (el.get(f"{_OPF}scheme") or "").lower()
        m = _ISBN_RE.search(text)
        if m and (scheme == "isbn" or "isbn" in text.lower() or len(re.sub(r"\D", "", m.group(1))) == 13):
            return "isbn:" + re.sub(r"[\s-]", "", m.group(1)).upper()
    chosen = next((el for el in ids if unique_id and el.get("id") == unique_id), None)
    if chosen is None and ids:
        chosen = ids[0]
    return clean_value(chosen.text) if chosen is not None else ""
