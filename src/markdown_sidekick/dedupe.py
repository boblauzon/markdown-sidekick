"""Find the same book held more than once in a library (stdlib + pypdfium2).

A personal library holds many books twice or more: the epub and the pdf of
one title, the same file in two bundle folders, ``name (1).pdf`` copies.
The reference library had 993 ebook files for 504 books. A batch converts
one copy per book — the preferred format — and records the others as
duplicates of it.

Evidence, strongest first:

1. identical files (same size and content hash) — always the same book;
2. the same ISBN;
3. the same normalised title, or the same normalised filename — but only
   when nothing contradicts it: the editions must agree ("C# in Depth,
   Third Edition" and "Fourth Edition" are never merged, nor is an edition
   with an unnumbered copy), and when both sides name authors they must
   share a surname.

Conservative by design, like the cleanup passes: a missed duplicate costs
one extra conversion; a false merge silently drops a book.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import debuglog, metadata

# Keep this format when a book is held in several. Formats nothing here can
# read rank after every readable one whatever their place in this order.
DEFAULT_PREFERENCE = (".epub", ".azw3", ".mobi", ".pdf")
UNREADABLE = frozenset({".mobi", ".azw", ".azw3", ".prc", ".djvu", ".kfx"})

_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
# "Third Edition", "3rd ed.", and run-together filenames: "sixthedition",
# "_2nd_edition", "pytorch2e".
_EDITION_RE = re.compile(
    r"(?:\b|(?<=[a-z]))(?:(\d{1,2})(?:st|nd|rd|th)?|(" + "|".join(_ORDINALS) + r"))[\s_-]*(?:edition\b|ed\.)"
    r"|(?<=[a-z])(\d)e$",
    re.IGNORECASE,
)
# Volume / part numbers distinguish books exactly like editions do: a study
# guide's Volume 1 and Volume 2 share a title. ("volume1", "Vol. II", "Part 2")
_VOLUME_RE = re.compile(r"(?:\b|(?<=[a-z]))(?:volume|vol\.?|part|book)[\s_-]*(\d{1,2}|[ivx]{1,4})\b", re.IGNORECASE)
_TITLE_MIN_CHARS = 8  # a title-only merge needs a distinctive title
_FNAME_MIN_CHARS = 10


@dataclass
class BookInfo:
    path: Path
    ext: str
    size: int = 0
    isbn: str = ""
    title_key: str = ""
    edition: str = ""
    surnames: frozenset[str] = frozenset()
    fname_key: str = ""


def _norm(text: str) -> str:
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"[^\w]+", " ", text).strip()
    return re.sub(r"^(?:the|a|an) ", "", text)


_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}


def edition_of(*texts: str) -> str:
    """The edition and volume a book's title/filename names, as one key:
    "3", "v2", "3v2" — "" when neither is named."""
    edition = volume = ""
    for text in texts:
        text = text or ""
        if not edition and (m := _EDITION_RE.search(text)):
            edition = m.group(1) or m.group(3) or str(_ORDINALS[m.group(2).lower()])
        if not volume and (m := _VOLUME_RE.search(text)):
            raw = m.group(1).lower()
            volume = "v" + (raw if raw.isdigit() else str(_ROMAN.get(raw, raw)))
    return edition.lstrip("0") + volume


def _surnames(authors: list[str]) -> frozenset[str]:
    out = set()
    for name in authors:
        name = name.strip()
        if not name:
            continue
        # "Samara, Timothy" is surname-first; "Timothy Samara" surname-last.
        last = name.split(",")[0] if "," in name else name.split()[-1]
        last = _norm(last)
        if last:
            out.add(last)
    return frozenset(out)


def _pdf_meta(path: Path) -> tuple[str, str]:
    """The PDF's own Title/Author (document info), when believable."""
    try:
        import pypdfium2 as pdfium

        from .pdflayout import _sane_meta

        pdf = pdfium.PdfDocument(str(path))
        try:
            info = pdf.get_metadata_dict()
        finally:
            pdf.close()
        return _sane_meta(info.get("Title")), _sane_meta(info.get("Author"))
    except Exception as exc:  # encrypted, damaged, not really a PDF: no evidence
        debuglog.exception("dedupe.pdf_meta", exc, path=str(path))
        return "", ""


def identify(path: Path) -> BookInfo:
    """Cheap identity evidence: package/document metadata, never the text."""
    from .export import tidy_filename_title

    ext = path.suffix.lower()
    info = BookInfo(path=path, ext=ext)
    try:
        info.size = path.stat().st_size
    except OSError:
        return info
    title, subtitle, authors = "", "", []
    if ext == ".epub":
        meta = metadata.epub_metadata(path)
        title, subtitle = meta.get("title", ""), meta.get("subtitle", "")
        authors = meta.get("authors", [])
        if meta.get("identifier", "").startswith("isbn:"):
            info.isbn = meta["identifier"]
    elif ext == ".pdf":
        raw_title, raw_author = _pdf_meta(path)
        title, subtitle = metadata.split_subtitle(metadata.tidy_title(metadata.clean_value(raw_title)))
        authors = metadata.split_authors([metadata.clean_value(raw_author)])
    stem_title = tidy_filename_title(path.stem)
    info.edition = edition_of(title, subtitle, stem_title)
    if title:
        key = _norm(_EDITION_RE.sub("", title).strip(" ,-:"))
        info.title_key = key if len(key.replace(" ", "")) >= _TITLE_MIN_CHARS else ""
    info.surnames = _surnames(authors)
    fname = re.sub(r"[^0-9a-z]", "", stem_title.casefold())
    info.fname_key = fname if len(fname) >= _FNAME_MIN_CHARS else ""
    return info


@dataclass
class _Group:
    members: list[int] = field(default_factory=list)
    editions: set[str] = field(default_factory=set)
    titles: set[str] = field(default_factory=set)
    surnames: set[str] = field(default_factory=set)


_SAMPLE = 1 << 20


def _content_hash(path: Path) -> str:
    """Hash of the first and last MiB. Only files of identical size are
    compared, and two different ebooks of the same byte size don't share
    both ends; reading 2 MiB instead of the whole file is what keeps a
    1,000-file library (8 GB of size-colliding copies) to seconds."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read(_SAMPLE))
        size = os.fstat(fh.fileno()).st_size
        if size > 2 * _SAMPLE:
            fh.seek(size - _SAMPLE)
            h.update(fh.read(_SAMPLE))
        elif size > _SAMPLE:
            h.update(fh.read())
    return h.hexdigest()


def find_duplicates(
    paths: list[Path],
    prefer: tuple[str, ...] = DEFAULT_PREFERENCE,
    keep_hint: set[str] | frozenset[str] = frozenset(),
) -> dict[Path, Path]:
    """Map each duplicate to the copy kept for its book.

    ``keep_hint`` (normcased paths, e.g. sources converted last run) wins
    ties between equally preferred copies, so re-runs don't flip between
    two identical files.
    """
    infos = [identify(p) for p in paths]
    groups = [_Group([i], {x.edition}, {x.title_key} - {""}, set(x.surnames)) for i, x in enumerate(infos)]
    owner = list(range(len(infos)))

    def find(i: int) -> int:
        while owner[i] != i:
            owner[i] = owner[owner[i]]
            i = owner[i]
        return i

    def union(i: int, j: int, *, certain: bool) -> None:
        a, b = find(i), find(j)
        if a == b:
            return
        ga, gb = groups[a], groups[b]
        if not certain:
            if len(ga.editions | gb.editions) > 1:  # "" vs "3" counts as a conflict
                return
            if len(ga.titles | gb.titles) > 1:
                return
            if ga.surnames and gb.surnames and not (ga.surnames & gb.surnames):
                return
        owner[b] = a
        ga.members += gb.members
        ga.editions |= gb.editions
        ga.titles |= gb.titles
        ga.surnames |= gb.surnames

    def link(key_of, *, certain: bool) -> None:
        first: dict[str, int] = {}
        for i in range(len(infos)):
            key = key_of(i)
            if not key:
                continue
            if key in first:
                union(first[key], i, certain=certain)
            else:
                first[key] = i

    by_size: dict[int, list[int]] = {}
    for i, info in enumerate(infos):
        if info.size:
            by_size.setdefault(info.size, []).append(i)
    hashes: dict[int, str] = {}
    for same in by_size.values():
        if len(same) > 1:
            for i in same:
                try:
                    hashes[i] = _content_hash(infos[i].path)
                except OSError as exc:
                    debuglog.exception("dedupe.hash", exc, path=str(infos[i].path))
    link(lambda i: f"{infos[i].size}:{hashes[i]}" if i in hashes else "", certain=True)
    link(lambda i: infos[i].isbn, certain=True)
    link(lambda i: infos[i].title_key, certain=False)
    link(lambda i: infos[i].fname_key, certain=False)

    rank_of = {ext: n for n, ext in enumerate(prefer)}

    def rank(i: int) -> tuple:
        info = infos[i]
        hinted = os.path.normcase(os.path.abspath(str(info.path))) in keep_hint
        return (info.ext in UNREADABLE, rank_of.get(info.ext, len(prefer)), not hinted, len(str(info.path)), str(info.path))

    dupes: dict[Path, Path] = {}
    for root in {find(i) for i in range(len(infos))}:
        members = groups[root].members
        if len(members) < 2:
            continue
        keep = min(members, key=rank)
        for i in members:
            if i != keep:
                dupes[infos[i].path] = infos[keep].path
    debuglog.event("dedupe.done", files=len(paths), duplicates=len(dupes))
    return dupes
