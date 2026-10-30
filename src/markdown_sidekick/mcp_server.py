"""MCP server exposing Markdown Sidekick's conversion pipeline.

Lets agent tools (Claude Desktop, Cursor, VS Code) convert local files to
Markdown mid-conversation using the *full* pipeline — markitdown for digital
docs, RapidOCR for images / scanned & vector PDFs, faster-whisper for audio,
plus the cleanup pass — not just plain markitdown.

Run it over stdio:
    python -m markdown_sidekick.mcp_server

The stdio transport speaks JSON-RPC on **stdout**, so anything else printed there
would corrupt the framing. All logging goes to stderr, and stdout is redirected
to stderr while a conversion runs so stray library prints can't break the channel.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
from pathlib import Path

# Keep startup quiet and offline-safe: no banner, no pypi update check. Must be
# set before importing fastmcp, which reads these at import time.
os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")  # Literal: stable|prerelease|off

from fastmcp import FastMCP

from . import debuglog, errors, export
from .cleanup import clean_markdown
from .converter import ConversionEngine, ConversionResult
from .quality import assess_markdown
from .settings import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("MarkdownSidekickMCP")

mcp = FastMCP(
    name="Markdown Sidekick",
    instructions=(
        "Converts local documents, images, scanned/vector PDFs and audio files to "
        "Markdown using a local offline pipeline (markitdown + RapidOCR + "
        "faster-whisper) with optional cleanup. Use convert_local_file with an "
        "absolute path for one file; for a folder or a library use convert_batch, "
        "then poll batch_status."
    ),
)

_engine: ConversionEngine | None = None
# FastMCP runs sync tools in worker threads and may handle requests
# concurrently. Conversions share a single engine (and non-thread-safe RapidOCR /
# WhisperModel singletons) and use a process-global stdout redirect, so they must
# run one at a time — this lock guards both the engine build and the conversion.
_lock = threading.Lock()


def _get_engine() -> ConversionEngine:
    """Build the engine once, honouring the user's saved Settings. Call under _lock."""
    global _engine
    if _engine is None:
        s = Settings.load()
        _engine = ConversionEngine(
            enable_ocr=s.enable_ocr,
            enable_audio=s.enable_audio,
            whisper_model=s.whisper_model,
            mineru_endpoint=s.mineru_endpoint,
            page_anchors=s.page_anchors,
            pdf_layout=s.pdf_layout,
            ocr_device=s.ocr_device,
        )
    return _engine


def _coded(incident: errors.Incident, subject: str = "") -> str:
    """An error as the AI client should relay it: the code, what happened,
    the next step, and the reference of its error-log entry."""
    lead = f" {subject}:" if subject else ":"
    detail = f" Technical details: {incident.detail}" if incident.detail else ""
    return (
        f"Error {incident.code}{lead} {incident.title} Next step: {incident.next_step} "
        f"(ref {incident.ref}).{detail}"
    )


def _conversion_error(result: ConversionResult) -> str:
    code = result.error_code or errors.classify_conversion(result.error or "")
    incident = errors.Incident(
        errors.CATALOG.get(code, errors.CATALOG["MS-199"]), result.error_ref or "—", result.error or ""
    )
    return _coded(incident, f"converting '{result.source.name}'")


# Warnings from the conversion behind each cached result (a better route
# failed and a lesser one produced the text) — relayed with the Markdown.
_convert_notes: dict[str, list[str]] = {}
# The source's own details from the last conversion of each path
# (ConversionResult.details: engine, title, author, title_source, meta) for save_to.
_convert_meta: dict[str, dict] = {}


# Conversions are expensive (OCR can take minutes); outline + section reads of
# the same document must not re-convert. Tiny keyed cache, newest-4.
_CACHE_MAX = 4
_convert_cache: "dict[tuple[str, float, bool], str]" = {}


def _convert_cached(resolved: str, clean: bool) -> tuple[str | None, str | None]:
    """Return (markdown, error). Caches by (path, mtime, clean)."""
    try:
        mtime = os.path.getmtime(resolved)
    except OSError:
        mtime = 0.0
    key = (resolved, mtime, clean)
    if key in _convert_cache:
        return _convert_cache[key], None
    with _lock:
        with contextlib.redirect_stdout(sys.stderr):
            result = _get_engine().convert_file(resolved)
            if not result.ok:
                return None, _conversion_error(result)
            markdown = result.markdown
            if clean:
                with debuglog.file_context(Path(resolved)):
                    markdown, _stats = clean_markdown(markdown, engine=result.engine)
    _convert_notes[resolved] = [
        f"{w.code}: {w.title} Next step: {w.next_step} (ref {w.ref})" for w in result.warnings
    ]
    _convert_meta[resolved] = result.details
    while len(_convert_cache) >= _CACHE_MAX:
        _convert_cache.pop(next(iter(_convert_cache)))
    _convert_cache[key] = markdown
    return markdown, None


def _resolve(file_path: str) -> tuple[str | None, str | None]:
    resolved = os.path.abspath(os.path.expanduser(file_path))
    if not os.path.exists(resolved):
        incident = errors.report("MS-103", where="mcp", detail=f"no file at '{resolved}'")
        return None, _coded(incident)
    if os.path.isdir(resolved):
        incident = errors.report("MS-104", where="mcp", detail=f"'{resolved}' is a directory")
        return None, _coded(incident)
    return resolved, None


@mcp.tool
def convert_local_file(
    file_path: str,
    clean: bool = True,
    save_to: str = "",
    max_chars: int = 150_000,
) -> str:
    """Convert a local file to Markdown and return the Markdown text.

    Handles PDFs (incl. scanned/vector via OCR), Office docs, HTML, CSV/JSON,
    images (OCR), and audio/video (transcription) — all locally.

    For LARGE documents (books, long reports), prefer convert_outline +
    convert_section so the result fits your context, or pass save_to to write
    the Markdown to disk and receive only a short confirmation.

    Args:
        file_path: Absolute path to the local file to convert.
        clean: Tidy the output (strip page noise/TOC junk, restore chapter
            headings, fence code with guessed languages). Defaults to true.
        save_to: Optional absolute path to write the Markdown to instead of
            returning it (a quality summary is returned in its place).
        max_chars: Truncate the returned text beyond this size (a note is
            appended). Ignored when save_to is set.
    """
    resolved, err = _resolve(file_path)
    if err:
        return err
    logger.info("Converting %s", resolved)
    markdown, conv_err = _convert_cached(resolved, clean)
    if conv_err is not None:
        logger.warning("Conversion failed: %s", conv_err)
        return conv_err
    assert markdown is not None
    logger.info("Converted (%d chars)", len(markdown))

    report = assess_markdown(markdown)
    noise_note = "".join(f"> ⚠ Note — {note}\n\n" for note in _convert_notes.get(resolved, []))
    if report.binary_noise:
        noise_note += (
            "> ⚠ Warning: this output looks like binary noise (mostly unreadable "
            "characters). The source file may be corrupt or in a format the "
            "converter does not actually understand — treat the content below "
            "with suspicion.\n\n"
        )

    if save_to:
        out_path = os.path.abspath(os.path.expanduser(save_to))
        try:
            export.save(
                markdown,
                Path(out_path),
                source=os.path.basename(resolved),
                details=_convert_meta.get(resolved),
                front_matter=Settings.load().export_front_matter,
            )
        except OSError as exc:
            return _coded(
                errors.report(errors.classify_os_error(exc), exc=exc, where="mcp save_to", target=out_path),
                f"saving to '{out_path}'",
            )
        # export.save read the file back, so this is a confirmed save.
        title = _convert_meta.get(resolved, {}).get("title", "")
        titled = f" Title: {title}." if title else ""
        return f"Saved Markdown to {out_path} (verified on disk).{titled} {report.summary()}"

    if len(markdown) > max_chars:
        return (
            noise_note
            + markdown[:max_chars]
            + f"\n\n---\n\n_(Truncated at {max_chars:,} of {len(markdown):,} characters. "
            "Use convert_outline + convert_section to read the rest, or save_to "
            "to write the full file to disk.)_"
        )
    return noise_note + markdown


@mcp.tool
def convert_outline(
    file_path: str, clean: bool = True, max_tokens: int = export.DEFAULT_MAX_TOKENS
) -> dict:
    """Convert a file and return its structure WITHOUT the content — title,
    quality report, and a numbered list of sections with token estimates.
    Sections follow chapter structure when present, and every section is
    guaranteed to fit max_tokens (heading-less documents are hard-split at
    safe boundaries; only a single indivisible fenced code block can exceed
    the budget). Follow up with convert_section — pass the SAME max_tokens
    there or the indices won't line up. The conversion is cached, so
    outline + section reads don't re-convert.
    """
    resolved, err = _resolve(file_path)
    if err:
        return {"error": err}
    markdown, conv_err = _convert_cached(resolved, clean)
    if conv_err is not None:
        return {"error": conv_err}
    assert markdown is not None
    sections = export.split_for_ai(markdown, max_tokens=max_tokens, pack=False)
    return {
        "warnings": _convert_notes.get(resolved, []),
        "title": export.document_title(markdown, os.path.basename(resolved)),
        "est_tokens": export.estimate_tokens(markdown),
        "quality": assess_markdown(markdown).as_dict(),
        "sections": [
            {"index": i, "title": s.title or "(document)", "est_tokens": s.est_tokens}
            for i, s in enumerate(sections)
        ],
    }


@mcp.tool
def convert_section(
    file_path: str,
    section_index: int,
    clean: bool = True,
    max_tokens: int = export.DEFAULT_MAX_TOKENS,
) -> str:
    """Return one section of a converted document (see convert_outline for
    the section list — pass the same max_tokens as the outline call so the
    indices match). Uses the cached conversion when available.
    """
    resolved, err = _resolve(file_path)
    if err:
        return err
    markdown, conv_err = _convert_cached(resolved, clean)
    if conv_err is not None:
        return conv_err
    assert markdown is not None
    sections = export.split_for_ai(markdown, max_tokens=max_tokens, pack=False)
    if not 0 <= section_index < len(sections):
        return _coded(
            errors.report(
                "MS-132",
                where="mcp convert_section",
                detail=f"section_index {section_index} is outside 0..{len(sections) - 1}",
            )
        )
    return sections[section_index].markdown


_URL_MAX_BYTES = 50 * 1024 * 1024


@mcp.tool
def convert_url(url: str, clean: bool = True, max_chars: int = 150_000) -> str:
    """Download a document (web page, PDF, image, …) and convert it to
    Markdown with the same local pipeline. http/https only, 50 MB cap.
    """
    import tempfile
    import urllib.parse
    import urllib.request

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return _coded(errors.report("MS-131", where="mcp convert_url", detail=f"scheme '{parsed.scheme}'"))
    suffix = Path(parsed.path).suffix.lower() or ".html"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MarkdownSidekick/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(_URL_MAX_BYTES + 1)
        if len(data) > _URL_MAX_BYTES:
            return _coded(errors.report("MS-131", where="mcp convert_url", detail="over 50 MB", url=url))
    except Exception as exc:
        return _coded(errors.report("MS-130", exc=exc, where="mcp convert_url", url=url))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / f"download{suffix}"
        tmp.write_bytes(data)
        with _lock:
            with contextlib.redirect_stdout(sys.stderr):
                result = _get_engine().convert_file(tmp)
                markdown = result.markdown
                if result.ok and clean:
                    markdown, _stats = clean_markdown(markdown, engine=result.engine)
    if not result.ok:
        return _conversion_error(result)
    if len(markdown) > max_chars:
        markdown = markdown[:max_chars] + f"\n\n_(Truncated at {max_chars:,} characters.)_"
    return markdown


@mcp.tool
def list_capabilities() -> dict:
    """Report which local conversion engines are available in this install."""
    from . import audio, mineru, ocr, pdflayout

    s = Settings.load()
    return {
        "image_ocr": ocr.ocr_available(),
        "pdf_ocr": ocr.pdf_ocr_available(),
        "pdf_layout": pdflayout.layout_available() and s.pdf_layout,
        "audio_transcription": audio.audio_available(),
        "mineru_endpoint": mineru.mineru_configured(s.mineru_endpoint),
        "whisper_model": s.whisper_model,
        "ocr_gpu": ocr.gpu_status(),  # "shadowed": see ocr.GPU_FIX
    }


# ---------------------------------------------------------------------------
# Library batches: start, then poll. A 500-book batch runs for many minutes,
# longer than MCP clients wait for one tool call, so convert_batch returns at
# once and batch_status reports progress and, at the end, the compact summary.
# ---------------------------------------------------------------------------
class _Batch:
    def __init__(self, batch_id: str, output_root: str) -> None:
        self.id = batch_id
        self.output_root = output_root
        self.cancel = threading.Event()
        self.state = "running"  # running | done | cancelled | error
        self.done = 0
        self.total = 0
        self.counts = {"converted": 0, "skipped": 0, "failed": 0}
        self.last = ""
        self.started = time.monotonic()
        self.summary: dict | None = None
        self.error = ""


_batches: dict[str, _Batch] = {}
_batches_lock = threading.Lock()


def _batch_view(job: _Batch) -> dict:
    view = {
        "batch_id": job.id,
        "state": job.state,
        "done": job.done,
        "total": job.total,
        **job.counts,
        "elapsed_s": round(time.monotonic() - job.started, 1),
        "output_root": job.output_root,
    }
    if job.last:
        view["last"] = job.last
    if job.summary is not None:
        view["summary"] = job.summary
    if job.error:
        view["error"] = job.error
    if job.state == "running":
        view["next"] = "Call batch_status again in 30-60 seconds."
    return view


@mcp.tool
def convert_batch(
    inputs: list[str],
    output_root: str,
    naming: str = "source",
    style: str = "single",
    ai_target: str = "Claude",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    skip_existing: bool = True,
    overwrite: bool = False,
    workers: int = 0,
    images: bool = False,
    clean: bool = True,
    dedupe: bool = True,
    prefer: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Start converting a whole library (folders and/or files) into
    output_root; returns a batch_id at once. Poll batch_status(batch_id)
    until state is "done" — it returns totals, failures with error codes,
    low-quality outputs to review, and the manifest path. One call replaces
    one convert_local_file per book.

    Folders are walked for documents and ebooks (PDF, EPUB, DOCX, PPTX,
    XLSX, HTML), skipping tool environments (.venv, node_modules,
    site-packages, .git), hidden folders and output_root itself; the folder
    structure is mirrored. Every input ends in one manifest row
    (output_root/_manifest.json and .csv): converted, skipped (unchanged
    since the last run, unsupported_format such as .mobi, or cancelled) or
    failed with its MS-… code. A saved file is read back before it counts
    as converted. Re-running converts only new or changed sources.

    Args:
        inputs: Absolute paths of folders to walk and/or files.
        output_root: Absolute path of the output folder (created if missing).
        naming: "source" (keep file names), "title" (the book's own title),
            or "title_author". A name another file already holds is never
            overwritten: edition, then author, then " (2)" is appended.
        style: "single" (one .md per source), "chapters" (a folder per book),
            or "ai" (AI-sized parts for ai_target).
        ai_target: Claude, ChatGPT, Gemini, "Gemini Notebook" or "Local LLM".
        include: Filename globs to convert instead of the default document
            types, e.g. ["*.png", "*.mp3"].
        exclude: Globs (name or path relative to an input folder) to skip.
        skip_existing: Skip sources unchanged since the last run (default).
        overwrite: Allow replacing files this batch didn't write.
        workers: Parallel conversions; 0 = automatic (cores minus one, max 4).
        images: Extract PDF figures to images/ folders.
        clean: Run the cleanup pass (default).
        dedupe: Convert one copy per book when the same book is there as
            epub and pdf, or copied into several folders (default). The
            others become "duplicate" rows naming the kept copy; different
            editions or volumes are never merged.
        prefer: Formats to keep, best first (default ["epub", "azw3",
            "mobi", "pdf"]).
        dry_run: Return what would be converted, skipped and de-duplicated
            right away, converting and writing nothing.
    """
    from . import batch

    out = os.path.abspath(os.path.expanduser(output_root))
    paths = [os.path.abspath(os.path.expanduser(p)) for p in inputs]
    missing = [p for p in paths if not os.path.exists(p)]
    if not paths or len(missing) == len(paths):
        incident = errors.report("MS-103", where="mcp convert_batch", detail=f"no such inputs: {missing[:3]}")
        return {"state": "error", "error": _coded(incident)}
    with _batches_lock:
        running = next((b for b in _batches.values() if b.state == "running"), None)
        if running is not None:
            return {**_batch_view(running), "note": "A batch is already running; poll it or cancel_batch it first."}
        try:
            options = batch.BatchOptions(
                output_root=Path(out),
                naming=naming,
                skip_existing=skip_existing,
                overwrite=overwrite,
                clean=clean,
                style=style,
                ai_target=ai_target,
                images=images,
                workers=workers or batch.default_workers(),
                include=tuple(include or ()),
                exclude=tuple(exclude or ()),
                engine=batch.engine_config(),
                isolate=True,  # never share this server's model singletons
                dedupe=dedupe,
                prefer=tuple(prefer or ()),
            )
        except ValueError as exc:
            return {"state": "error", "error": str(exc)}
        if dry_run:
            return {"state": "plan", **batch.plan_batch(paths, options), "skipped_inputs": missing}
        job = _Batch(f"b{int(time.time())}-{len(_batches) + 1}", out)
        _batches[job.id] = job

    def progress(done: int, total: int, row) -> None:
        job.done, job.total = done, total
        job.counts[row.status] = job.counts.get(row.status, 0) + 1
        job.last = f"{row.status}: {Path(row.output or row.source).name}"

    def run() -> None:
        try:
            summary = batch.run_batch(paths, options, on_progress=progress, cancel=job.cancel)
            job.summary = summary.as_dict()
            job.total = summary.total
            job.state = "cancelled" if summary.cancelled else "done"
        except Exception as exc:
            incident = errors.report(
                errors.classify_os_error(exc) if isinstance(exc, OSError) else "MS-900",
                exc=exc, where="mcp convert_batch",
            )
            job.error, job.state = _coded(incident), "error"
        logger.info("Batch %s %s: %s", job.id, job.state, job.counts)

    threading.Thread(target=run, name=f"batch-{job.id}", daemon=True).start()
    logger.info("Batch %s started: %d input(s) -> %s", job.id, len(paths), out)
    return {
        "batch_id": job.id,
        "state": "running",
        "output_root": out,
        "skipped_inputs": missing,
        "next": "Call batch_status(batch_id) every 30-60 seconds until state is done.",
    }


@mcp.tool
def batch_status(batch_id: str = "") -> dict:
    """Progress of a convert_batch run (the latest when batch_id is blank).
    When state is "done" (or "cancelled"), "summary" holds the totals, the
    failures with their MS-… codes, the outputs worth reviewing, and the
    manifest path (one row per input)."""
    with _batches_lock:
        job = _batches.get(batch_id) if batch_id else (list(_batches.values())[-1] if _batches else None)
    if job is None:
        return {"state": "unknown", "error": f"No batch {batch_id!r} in this session."}
    return _batch_view(job)


@mcp.tool
def cancel_batch(batch_id: str = "") -> dict:
    """Stop a running convert_batch (the latest when batch_id is blank).
    Files already converted are kept and listed in the manifest; the rest
    are recorded as skipped (cancelled). Poll batch_status until it says
    "cancelled"."""
    with _batches_lock:
        job = _batches.get(batch_id) if batch_id else (list(_batches.values())[-1] if _batches else None)
    if job is None:
        return {"state": "unknown", "error": f"No batch {batch_id!r} in this session."}
    job.cancel.set()
    return _batch_view(job)


def main() -> None:
    errors.install_crash_hooks()
    settings = Settings.load()
    if settings.debug_mode or debuglog.debug_requested():
        # Files only — stdout stays reserved for JSON-RPC.
        session = debuglog.enable("mcp", snapshots=settings.debug_snapshots)
        logger.info("Debug trace: %s", session)
    mcp.run(show_banner=False)  # stdio transport by default


if __name__ == "__main__":
    main()
