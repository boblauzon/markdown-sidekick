"""Library-scale batch conversion: one call converts a folder.

A user (or an agent) points this at a library of a few hundred books and
gets one Markdown file — or book folder — per source, mirrored under an
output root, plus a manifest of every input and what happened to it. The
reference run this was built from needed 504 separate tool calls, four
parallel agents and three post-processing scripts; this is all of it.

Guarantees:

- Every input in scope ends in exactly one row: ``converted``, ``skipped``
  (``unchanged`` since the last run, ``unsupported_format``, ``cancelled``)
  or ``failed`` (with its MS-… error code). converted + skipped + failed =
  total.
- "Converted" means the output was read back (export.write_text_verified).
- Re-runs are cheap: a source whose size, modification time and conversion
  settings match its manifest row, and whose outputs still exist, is skipped
  without being opened.
- Nothing is overwritten that this source didn't write: a name another file
  already holds gets the edition, then the first author, then " (2)" …
  appended (``overwrite=True`` lifts this).

Conversion runs in worker processes (the OCR/Whisper model singletons are
not thread-safe; processes also give real parallelism for epub/Office
parsing). Naming, figure extraction and every write happen in the calling
process, so name collisions are settled in one place.
"""

from __future__ import annotations

import concurrent.futures as cf
import csv
import fnmatch
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from . import debuglog, errors, export
from .dedupe import DEFAULT_PREFERENCE

MANIFEST_NAME = "_manifest.json"
MANIFEST_CSV = "_manifest.csv"
NAMING_MODES = ("source", "title", "title_author")
STYLES = ("single", "chapters", "ai")

# What a folder walk picks up by default: documents and ebooks. Images,
# audio, .md/.txt and data files in a library are usually screenshots,
# earlier conversions and sidecar files — add them with ``include`` globs.
LIBRARY_EXTENSIONS = frozenset({".pdf", ".epub", ".docx", ".pptx", ".xlsx", ".xls", ".html", ".htm"})
# Ebook formats a library holds that no engine here reads (their usual
# readers are GPL); listed in the manifest as skipped so they are not lost.
UNSUPPORTED_EBOOK_EXTENSIONS = frozenset({".mobi", ".azw", ".azw3", ".prc", ".djvu", ".kfx"})
# Folders never walked into: tool environments and caches hold thousands of
# files that are not the user's documents. Hidden (.x) and system ($x)
# folders are skipped too, as is the output root itself.
DEFAULT_EXCLUDE_DIRS = frozenset(
    {"site-packages", "venv", ".venv", "env", "node_modules", "__pycache__", ".git", "$recycle.bin"}
)
# Full output path budget (Windows MAX_PATH is 260; leave room for images/).
_MAX_PATH = 240
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
)
_EDITION_RE = re.compile(
    r"\b((?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"revised|updated|new)\s+edition)\b",
    re.IGNORECASE,
)
_LOW_QUALITY = 85
_MAX_LISTED = 20
_MANIFEST_EVERY = 10  # rewrite the manifest after this many finished files


# ---------------------------------------------------------------------------
# Options and results
# ---------------------------------------------------------------------------
@dataclass
class BatchOptions:
    output_root: Path
    naming: str = "source"  # NAMING_MODES
    skip_existing: bool = True
    overwrite: bool = False
    clean: bool = True
    style: str = "single"  # STYLES: one file, a chapter folder, or AI-sized parts
    ai_target: str = "Claude"
    front_matter: bool = True
    images: bool = False
    workers: int = 1
    include: tuple[str, ...] = ()  # filename globs; empty = LIBRARY_EXTENSIONS
    exclude: tuple[str, ...] = ()  # globs against the path relative to the root
    # Engine configuration (from Settings by default; see engine_config()).
    engine: dict = field(default_factory=dict)
    # Convert in worker processes even with one worker: a host that converts
    # in-process too (the MCP server) must not share its model singletons
    # with a batch running on another thread.
    isolate: bool = False
    # Convert one copy per book (dedupe.py); the others become "duplicate"
    # rows naming the kept copy. ``prefer`` orders the formats to keep.
    dedupe: bool = True
    prefer: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.naming not in NAMING_MODES:
            raise ValueError(f"naming must be one of {', '.join(NAMING_MODES)}")
        if self.style not in STYLES:
            raise ValueError(f"style must be one of {', '.join(STYLES)}")
        if self.style == "ai" and self.ai_target not in export.AI_TARGETS:
            raise ValueError(f"ai_target must be one of {', '.join(export.AI_TARGETS)}")
        self.output_root = Path(self.output_root)
        self.workers = max(1, int(self.workers))
        self.prefer = tuple(
            e.lower() if e.startswith(".") else "." + e.lower() for e in self.prefer if e
        ) or DEFAULT_PREFERENCE


@dataclass
class BatchRow:
    source: str
    status: str = ""  # converted | skipped | failed
    reason: str = ""  # unchanged | unsupported_format | cancelled | conversion_failed | save_failed
    code: str = ""  # MS-… for a failure (or a warning on a converted file)
    message: str = ""
    output: str = ""  # the .md file, or the book folder
    files: list[str] = field(default_factory=list)  # everything written
    title: str = ""
    title_source: str = ""
    authors: list[str] = field(default_factory=list)
    identifier: str = ""
    engine: str = ""
    quality: int | None = None
    issues: list[str] = field(default_factory=list)
    est_tokens: int = 0
    duration_s: float = 0.0
    size: int = 0
    mtime: float = 0.0
    settings: str = ""  # settings_hash() of the run that wrote the outputs
    error_ref: str = ""
    duplicate_of: str = ""  # the kept copy, for a "duplicate" row


@dataclass
class BatchSummary:
    total: int = 0
    converted: int = 0
    skipped: int = 0
    failed: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    manifest: str = ""
    manifest_csv: str = ""
    elapsed_s: float = 0.0
    cancelled: bool = False
    rows: list[BatchRow] = field(default_factory=list)

    def as_dict(self) -> dict:
        """A compact result: totals, where the manifest is, and the files
        that need attention — never the whole run (that is the manifest)."""
        failures = [r for r in self.rows if r.status == "failed"]
        review = sorted(
            (r for r in self.rows if r.status == "converted" and r.quality is not None and r.quality < _LOW_QUALITY),
            key=lambda r: r.quality or 0,
        )
        return {
            "total": self.total,
            "converted": self.converted,
            "skipped": self.skipped,
            "failed": self.failed,
            "by_reason": self.by_reason,
            "cancelled": self.cancelled,
            "elapsed_s": round(self.elapsed_s, 1),
            "manifest": self.manifest,
            "manifest_csv": self.manifest_csv,
            "failures": [
                {"source": r.source, "code": r.code, "reason": r.reason, "message": r.message}
                for r in failures[:_MAX_LISTED]
            ],
            "more_failures": max(0, len(failures) - _MAX_LISTED),
            "review": [
                {"output": r.output, "quality": r.quality, "issues": r.issues[:3]} for r in review[:_MAX_LISTED]
            ],
            "more_review": max(0, len(review) - _MAX_LISTED),
        }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Job:
    source: Path
    rel_dir: Path  # where the output goes, relative to output_root


def _matches(name: str, rel: str, globs: Iterable[str]) -> bool:
    name, rel = name.lower(), rel.lower()
    return any(fnmatch.fnmatch(name, g.lower()) or fnmatch.fnmatch(rel, g.lower()) for g in globs)


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(a.resolve()) == os.path.normcase(b.resolve())
    except OSError:
        return False


def discover(inputs: Iterable[str | os.PathLike[str]], options: BatchOptions) -> list[_Job]:
    """The files in scope: folders are walked (pruning tool environments,
    hidden folders and the output root), files are taken as given."""
    from .converter import SUPPORTED_EXTENSIONS

    out_root = options.output_root
    jobs: dict[str, _Job] = {}
    for raw in inputs:
        root = Path(raw)
        if root.is_file():
            ext = root.suffix.lower()
            if ext in SUPPORTED_EXTENSIONS or ext in UNSUPPORTED_EBOOK_EXTENSIONS:
                jobs.setdefault(os.path.normcase(str(root.resolve())), _Job(root, Path()))
            continue
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            dirnames[:] = sorted(
                d for d in dirnames
                if d.lower() not in DEFAULT_EXCLUDE_DIRS
                and not d.startswith((".", "$"))
                and not _same(here / d, out_root)
                and not _matches(d, (here / d).relative_to(root).as_posix(), options.exclude)
            )
            for name in sorted(filenames):
                path = here / name
                rel = path.relative_to(root).as_posix()
                if options.exclude and _matches(name, rel, options.exclude):
                    continue
                ext = path.suffix.lower()
                if options.include:
                    wanted = _matches(name, rel, options.include) and (
                        ext in SUPPORTED_EXTENSIONS or ext in UNSUPPORTED_EBOOK_EXTENSIONS
                    )
                else:
                    wanted = ext in LIBRARY_EXTENSIONS or ext in UNSUPPORTED_EBOOK_EXTENSIONS
                if wanted:
                    key = os.path.normcase(str(path.resolve()))
                    jobs.setdefault(key, _Job(path, here.relative_to(root)))
    return list(jobs.values())


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------
def safe_filename(name: str, max_len: int = 120) -> str:
    """``name`` as a valid Windows file name: colon -> " - ", reserved
    characters dropped, trailing dots/spaces trimmed, reserved device names
    avoided, and cut at a word boundary to ``max_len``."""
    text = name.replace(":", " - ")
    text = re.sub(r'[<>"/\\|?*\x00-\x1f]', "", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    if len(text) > max_len:
        cut = text[: max_len + 1]
        text = cut.rsplit(" ", 1)[0] if " " in cut.strip() else text[:max_len]
        text = text.rstrip(" .,-–—")
    if not text:
        text = "untitled"
    if text.split(".")[0].upper() in _RESERVED_NAMES:
        text += "_"
    return text


def _base_name(row: BatchRow, source: Path, naming: str) -> str:
    if naming == "source":
        return source.stem
    title = row.title or export.tidy_filename_title(source.stem)
    if naming == "title_author" and row.authors:
        title = f"{title} - {row.authors[0]}"
    return title


class _Namer:
    """Settles every output name of a run in one place.

    A candidate is free when no other source claimed it this run and it
    doesn't exist on disk — unless this source wrote it last time (its
    manifest row "owns" it) or overwrite is on.
    """

    def __init__(self, overwrite: bool) -> None:
        self.overwrite = overwrite
        self.claimed: dict[str, str] = {}

    def claim(self, out_dir: Path, base: str, *, suffix: str, source: str, owned: set[str],
              edition: str = "", author: str = "") -> Path:
        budget = _MAX_PATH - len(str(out_dir)) - 1 - len(suffix) - (70 if not suffix else 0)
        base = safe_filename(base, max(16, min(120, budget)))
        candidates = [base]
        if edition and edition.lower() not in base.lower():
            candidates.append(f"{base} ({edition})")
        if author and author.lower() not in base.lower():
            candidates.append(f"{base} - {safe_filename(author, 40)}")
        candidates += [f"{base} ({n})" for n in range(2, 1000)]
        for cand in candidates:
            path = out_dir / f"{cand}{suffix}"
            key = os.path.normcase(str(path))
            holder = self.claimed.get(key)
            if holder is not None and holder != source:
                continue
            if path.exists() and key not in owned and not self.overwrite:
                continue
            self.claimed[key] = source
            return path
        raise OSError(f"no free output name for {base} in {out_dir}")


# ---------------------------------------------------------------------------
# Settings fingerprint and manifest
# ---------------------------------------------------------------------------
def engine_config(settings=None, *, no_ocr: bool = False, no_audio: bool = False) -> dict:
    """The conversion-engine configuration from Settings (picklable)."""
    if settings is None:
        from .settings import Settings

        settings = Settings.load()
    return {
        "enable_ocr": settings.enable_ocr and not no_ocr,
        "enable_audio": settings.enable_audio and not no_audio,
        "whisper_model": settings.whisper_model,
        "mineru_endpoint": settings.mineru_endpoint,
        "page_anchors": settings.page_anchors,
        "pdf_layout": settings.pdf_layout,
        "ocr_device": settings.ocr_device,
    }


def settings_hash(options: BatchOptions) -> str:
    """Everything that changes what a conversion writes. A row written under
    another fingerprint is converted again rather than skipped."""
    from . import __version__

    material = {
        "version": __version__,
        "naming": options.naming,
        "clean": options.clean,
        "style": options.style,
        "ai_target": options.ai_target if options.style == "ai" else "",
        "front_matter": options.front_matter,
        "images": options.images,
        "engine": options.engine,
    }
    return hashlib.sha1(json.dumps(material, sort_keys=True).encode()).hexdigest()[:12]


def _key(source: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(source)))


def load_manifest(output_root: Path) -> dict[str, BatchRow]:
    """The previous run's rows by source (empty when there is none or it
    can't be read — a damaged manifest only costs a full re-run)."""
    path = Path(output_root) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        names = {f.name for f in BatchRow.__dataclass_fields__.values()}
        rows = [BatchRow(**{k: v for k, v in r.items() if k in names}) for r in data.get("rows", [])]
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        debuglog.exception("batch.manifest_load", exc, path=str(path))
        return {}
    return {_key(r.source): r for r in rows}


def _write_manifest(options: BatchOptions, rows: list[BatchRow], summary: BatchSummary) -> None:
    root = options.output_root
    data = {
        "tool": "Markdown Sidekick",
        "updated": datetime.now().isoformat(timespec="seconds"),
        "settings": settings_hash(options),
        "run": {k: v for k, v in summary.as_dict().items() if k in ("total", "converted", "skipped", "failed", "by_reason", "cancelled", "elapsed_s")},
        "rows": [asdict(r) for r in rows],
    }
    export.write_text_verified(root / MANIFEST_NAME, json.dumps(data, indent=2, ensure_ascii=False))
    buf = io.StringIO()
    cols = ["source", "output", "status", "reason", "code", "message", "title", "title_source",
            "authors", "identifier", "engine", "quality", "issues", "est_tokens", "duration_s", "duplicate_of"]
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(cols)
    for r in rows:
        d = asdict(r)
        d["authors"] = "; ".join(r.authors)
        d["issues"] = "; ".join(r.issues)
        writer.writerow(["" if d[c] is None else d[c] for c in cols])
    # utf-8-sig: Excel opens a BOM-less UTF-8 CSV as ANSI and mangles titles.
    export.write_text_verified(root / MANIFEST_CSV, "﻿" + buf.getvalue())


# ---------------------------------------------------------------------------
# The worker side: convert + clean + assess (runs in a worker process)
# ---------------------------------------------------------------------------
_engine = None


def _init_worker(config: dict, figure_markers: bool, quiet_stdout: bool = False) -> None:
    global _engine
    if quiet_stdout:
        # A worker of the MCP server inherits its stdout, which carries
        # JSON-RPC: anything a library prints there (even from native code)
        # would corrupt the channel. Point fd 1 at stderr for good.
        try:
            os.dup2(2, 1)
        except OSError:
            pass  # no usable stderr (windowed build): nothing will print either
        sys.stdout = sys.stderr
    from .converter import ConversionEngine

    _engine = ConversionEngine(**config)
    _engine.figure_markers = figure_markers


# Windows attributes of a cloud placeholder whose contents aren't on this
# disk: RECALL_ON_DATA_ACCESS (OneDrive Files On-Demand), RECALL_ON_OPEN,
# OFFLINE.
_PLACEHOLDER_ATTRS = 0x400000 | 0x40000 | 0x1000
_HYDRATE_DELAYS = (1.0, 3.0, 6.0)


def is_placeholder(path: Path) -> bool:
    try:
        attrs = getattr(os.stat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & _PLACEHOLDER_ATTRS)


def _hydrate(path: Path) -> OSError | None:
    """Read an online-only file once, so its sync provider downloads it
    before a converter opens it (a provider that's slow to start fails the
    first read; retried with backoff). Returns the last error, if any."""
    last: OSError | None = None
    for delay in (*_HYDRATE_DELAYS, None):
        try:
            with open(path, "rb") as fh:
                while fh.read(1 << 20):
                    pass
            return None
        except OSError as exc:
            last = exc
            if delay is None:
                break
            time.sleep(delay)
    return last


def _convert_job(source: str, clean: bool) -> dict:
    """Convert one file; returns plain data (it crosses a process boundary)."""
    from .cleanup import clean_markdown
    from .quality import assess_markdown

    started = time.perf_counter()
    if is_placeholder(Path(source)) and (exc := _hydrate(Path(source))) is not None:
        incident = errors.report("MS-116", exc=exc, where="batch download", file=source)
        return {"ok": False, "engine": "", "error": f"{type(exc).__name__}: {exc}", "error_code": "MS-116",
                "error_ref": incident.ref, "warnings": [], "seconds": time.perf_counter() - started}
    result = _engine.convert_file(source)
    out: dict = {
        "ok": result.ok,
        "engine": result.engine,
        "error": result.error or "",
        "error_code": result.error_code,
        "error_ref": result.error_ref,
        "warnings": [(w.code, w.title, w.ref) for w in result.warnings],
    }
    if result.ok:
        markdown = result.markdown
        if clean:
            with debuglog.file_context(Path(source)):
                markdown, _stats = clean_markdown(markdown, engine=result.engine)
        report = assess_markdown(markdown)
        out.update(
            markdown=markdown,
            title=result.doc_title,
            author=result.doc_author,
            title_source=result.title_source,
            meta=dict(result.doc_meta),
            quality=report.score,
            issues=list(report.issues),
            est_tokens=report.est_tokens,
        )
    out["seconds"] = time.perf_counter() - started
    return out


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
ProgressFn = Callable[[int, int, BatchRow], None]


def default_workers() -> int:
    """Parallel conversions: cores minus one, at most 4. OCR already spreads
    one page across every core, so more processes mostly add memory."""
    return max(1, min(4, (os.cpu_count() or 2) - 1))


def _plan(jobs: list[_Job], options: BatchOptions, previous: dict[str, BatchRow],
          fingerprint: str) -> tuple[list[BatchRow], list[_Job]]:
    """Rows decided without converting (duplicates, unsupported formats,
    unchanged sources) and the jobs left to convert."""
    dupes: dict = {}
    if options.dedupe and len(jobs) > 1:
        from .dedupe import find_duplicates

        converted_before = {k for k, r in previous.items() if r.status == "converted" or r.reason == "unchanged"}
        dupes = find_duplicates([j.source for j in jobs], options.prefer, keep_hint=converted_before)
    decided: list[BatchRow] = []
    to_convert: list[_Job] = []
    for job in jobs:
        row = _new_row(job.source)
        if job.source in dupes:
            row.status, row.reason = "skipped", "duplicate"
            row.duplicate_of = str(dupes[job.source].resolve())
            row.message = f"Same book as {dupes[job.source].name}, which is converted instead."
            decided.append(row)
            continue
        if job.source.suffix.lower() in UNSUPPORTED_EBOOK_EXTENSIONS:
            row.status, row.reason = "skipped", "unsupported_format"
            row.code = "MS-105"
            row.message = errors.CATALOG["MS-105"].title
            decided.append(row)
            continue
        prev = previous.get(_key(job.source))
        if options.skip_existing and _unchanged(prev, row, fingerprint):
            kept = BatchRow(**asdict(prev))
            kept.status, kept.reason, kept.code, kept.message, kept.duration_s = "skipped", "unchanged", "", "", 0.0
            decided.append(kept)
            continue
        to_convert.append(job)
    return decided, to_convert


def plan_batch(inputs: Iterable[str | os.PathLike[str]], options: BatchOptions) -> dict:
    """What run_batch would do, without converting or writing anything."""
    if not options.engine:
        options.engine = engine_config()
    jobs = discover(inputs, options)
    decided, to_convert = _plan(jobs, options, load_manifest(options.output_root), settings_hash(options))
    by_reason: dict[str, int] = {}
    for r in decided:
        by_reason[r.reason] = by_reason.get(r.reason, 0) + 1
    dupes = [r for r in decided if r.reason == "duplicate"]
    return {
        "total": len(jobs),
        "to_convert": len(to_convert),
        "skipped": by_reason,
        "convert_sample": [str(j.source) for j in to_convert[:_MAX_LISTED]],
        "duplicates": [{"source": r.source, "duplicate_of": r.duplicate_of} for r in dupes[:_MAX_LISTED]],
        "more_duplicates": max(0, len(dupes) - _MAX_LISTED),
    }


def run_batch(
    inputs: Iterable[str | os.PathLike[str]],
    options: BatchOptions,
    *,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> BatchSummary:
    """Convert every input in scope; see the module docstring."""
    started = time.perf_counter()
    if not options.engine:
        options.engine = engine_config()
    options.output_root.mkdir(parents=True, exist_ok=True)
    fingerprint = settings_hash(options)
    previous = load_manifest(options.output_root)
    jobs = discover(inputs, options)
    summary = BatchSummary(total=len(jobs))
    rows: list[BatchRow] = []
    pending_keys: set[str] = set()  # sources queued for conversion, not yet finished
    namer = _Namer(options.overwrite)
    debuglog.event("batch.start", inputs=len(jobs), workers=options.workers, settings=fingerprint,
                   output_root=str(options.output_root), naming=options.naming, style=options.style)

    def finish(row: BatchRow) -> None:
        rows.append(row)
        setattr(summary, row.status, getattr(summary, row.status) + 1)
        if row.reason:
            summary.by_reason[row.reason] = summary.by_reason.get(row.reason, 0) + 1
        if on_progress is not None:
            on_progress(len(rows), summary.total, row)
        if len(rows) % _MANIFEST_EVERY == 0:
            _save(partial=True)

    def _save(partial: bool = False) -> None:
        # Rows of earlier runs whose sources are out of this run's scope
        # stay in the manifest: it describes the whole output root.
        here = {_key(r.source) for r in rows}
        kept = [r for k, r in previous.items() if k not in here and k not in pending_keys]
        summary.elapsed_s = time.perf_counter() - started
        try:
            _write_manifest(options, kept + rows, summary)
        except OSError as exc:
            if not partial:
                raise
            debuglog.exception("batch.manifest_write", exc)

    decided, to_convert = _plan(jobs, options, previous, fingerprint)
    pending_keys.update(_key(j.source) for j in to_convert)
    for row in decided:
        finish(row)

    def handle(job: _Job, data: dict | None, exc: BaseException | None) -> None:
        pending_keys.discard(_key(job.source))
        prev = previous.get(_key(job.source))
        owned = {os.path.normcase(f) for f in (prev.files if prev else [])}
        if prev and prev.output:
            owned.add(os.path.normcase(prev.output))
        finish(_finish_job(job, data, exc, options, namer, owned, fingerprint))

    cancelled = False
    try:
        if not options.isolate and (options.workers <= 1 or len(to_convert) <= 1):
            _init_worker(options.engine, options.images)
            for job in to_convert:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                try:
                    data, err = _convert_job(str(job.source), options.clean), None
                except Exception as exc:  # convert_file never raises; this is a bug
                    data, err = None, exc
                handle(job, data, err)
        elif to_convert:
            cancelled = _run_pool(to_convert, options, handle, cancel)
    except KeyboardInterrupt:
        cancelled = True
    for job in to_convert:
        if _key(job.source) in pending_keys:  # never reached: say so
            row = _new_row(job.source)
            row.status, row.reason = "skipped", "cancelled"
            row.message = "The batch was stopped before this file was converted."
            finish(row)
    pending_keys.clear()
    summary.cancelled = cancelled
    summary.rows = rows
    _save()
    summary.manifest = str(options.output_root / MANIFEST_NAME)
    summary.manifest_csv = str(options.output_root / MANIFEST_CSV)
    debuglog.event("batch.done", **{k: v for k, v in summary.as_dict().items() if k in ("total", "converted", "skipped", "failed", "by_reason", "elapsed_s", "cancelled")})
    return summary


def _run_pool(to_convert: list[_Job], options: BatchOptions, handle, cancel) -> bool:
    """Convert in worker processes; returns True when cancelled."""
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")  # Windows' only start method; explicit elsewhere
    with cf.ProcessPoolExecutor(
        max_workers=min(options.workers, len(to_convert)),
        mp_context=ctx,
        initializer=_init_worker,
        initargs=(options.engine, options.images, True),  # stdout carries the CLI summary or MCP JSON-RPC
    ) as pool:
        futures = {pool.submit(_convert_job, str(j.source), options.clean): j for j in to_convert}
        try:
            for fut in cf.as_completed(futures):
                job = futures[fut]
                try:
                    handle(job, fut.result(), None)
                except cf.process.BrokenProcessPool as exc:
                    handle(job, None, exc)
                except Exception as exc:
                    handle(job, None, exc)
                if cancel is not None and cancel.is_set():
                    pool.shutdown(wait=True, cancel_futures=True)
                    return True
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
    return False


def _new_row(source: Path) -> BatchRow:
    row = BatchRow(source=str(source.resolve()) if source.exists() else str(source))
    try:
        st = source.stat()
        row.size, row.mtime = st.st_size, st.st_mtime
    except OSError:
        pass  # a vanished source fails at conversion with its own code
    return row


def _unchanged(prev: BatchRow | None, now: BatchRow, fingerprint: str) -> bool:
    if prev is None or prev.status not in ("converted", "skipped") or not prev.files:
        return False
    if prev.reason in ("unsupported_format", "cancelled"):
        return False
    if prev.size != now.size or abs(prev.mtime - now.mtime) > 1e-3 or prev.settings != fingerprint:
        return False
    return all(Path(f).exists() for f in prev.files)


def _finish_job(job: _Job, data: dict | None, exc: BaseException | None, options: BatchOptions,
                namer: _Namer, owned: set[str], fingerprint: str) -> BatchRow:
    """Name, export and verify one converted file (in the calling process)."""
    row = _new_row(job.source)
    row.settings = fingerprint
    if exc is not None or data is None:
        incident = errors.report("MS-901", exc=exc, where="batch worker", file=str(job.source))
        row.status, row.reason, row.code = "failed", "conversion_failed", incident.code
        row.message, row.error_ref = incident.title, incident.ref
        return row
    row.engine = data.get("engine", "")
    row.duration_s = round(data.get("seconds", 0.0), 2)
    if not data["ok"]:
        code = data.get("error_code") or errors.classify_conversion(data.get("error", ""))
        spec = errors.CATALOG.get(code, errors.CATALOG["MS-199"])
        reason = "input_not_local" if spec.code == "MS-116" else "conversion_failed"
        row.status, row.reason, row.code = "failed", reason, spec.code
        row.message, row.error_ref = f"{spec.title} {spec.next_step}", data.get("error_ref", "")
        return row

    meta = data.get("meta") or {}
    markdown = data["markdown"]
    row.title, row.title_source = export.resolve_title(markdown, job.source.name, data.get("title", ""),
                                                       data.get("title_source", ""))
    row.authors = list(meta.get("authors") or ([data["author"]] if data.get("author") else []))
    row.identifier = meta.get("identifier", "")
    row.quality, row.issues, row.est_tokens = data.get("quality"), data.get("issues", []), data.get("est_tokens", 0)
    if data.get("warnings"):
        code, title, ref = data["warnings"][0]
        row.code, row.message, row.error_ref = code, title, ref

    out_dir = options.output_root / job.rel_dir
    book = options.style in ("chapters", "ai")
    edition = ""
    if m := _EDITION_RE.search(" ".join([row.title, meta.get("subtitle", "")])):
        edition = m.group(1).title()
    t0 = time.perf_counter()
    try:
        target = namer.claim(
            out_dir, _base_name(row, job.source, options.naming), suffix="" if book else ".md",
            source=row.source, owned=owned, edition=edition, author=row.authors[0] if row.authors else "",
        )
        files = _export(job.source, markdown, target, book, data, options, row)
    except Exception as exc:
        code = errors.classify_os_error(exc) if isinstance(exc, OSError) else "MS-901"
        incident = errors.report(code, exc=exc, where="batch save", file=str(job.source))
        row.status, row.reason, row.code = "failed", "save_failed", incident.code
        row.message, row.error_ref = f"{incident.title} {incident.next_step}", incident.ref
        return row
    row.status = "converted"
    row.output = str(target)
    row.files = [str(f) for f in files]
    row.duration_s = round(row.duration_s + time.perf_counter() - t0, 2)
    return row


def _export(source: Path, markdown: str, target: Path, book: bool, data: dict,
            options: BatchOptions, row: BatchRow) -> list[Path]:
    from . import figures

    figs: list = []
    if options.images and source.suffix.lower() == ".pdf":
        images_dir = target / "images" if book else target.parent / "images" / target.stem
        rel_dir = "images" if book else f"images/{target.stem}"
        try:
            figs = figures.extract_pdf_figures(source, images_dir)
        except Exception as exc:  # the Markdown is still worth saving
            incident = errors.report("MS-401", exc=exc, where="batch figures", file=str(source))
            row.code, row.message, row.error_ref = incident.code, incident.title, incident.ref
        markdown = figures.insert_figure_links(markdown, figs, rel_dir)
    else:
        markdown = figures.strip_figure_markers(markdown)
    common = dict(
        source=source.name,
        engine=data.get("engine", ""),
        front_matter=options.front_matter,
        title=row.title,
        author=", ".join(row.authors),
        title_source=row.title_source,
        meta=data.get("meta") or {},
    )
    if book:
        notebook = options.style == "ai" and options.ai_target in export.NOTEBOOK_TARGETS
        res = export.export_book(
            markdown, target,
            max_tokens=export.AI_TARGETS[options.ai_target] if options.style == "ai" else export.DEFAULT_MAX_TOKENS,
            ai_sections=options.style == "ai",
            notebook=notebook,
            **common,
        )
        written = list(res.paths) + [p for p in (res.index_path, res.manifest_path) if p]
    else:
        written = export.export_single(markdown, target, **common).paths
    return written + [f.path for f in figs]
