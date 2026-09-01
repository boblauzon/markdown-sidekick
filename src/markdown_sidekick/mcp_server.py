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
from pathlib import Path

# Keep startup quiet and offline-safe: no banner, no pypi update check. Must be
# set before importing fastmcp, which reads these at import time.
os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")  # Literal: stable|prerelease|off

from fastmcp import FastMCP

from . import export
from .cleanup import clean_markdown
from .converter import ConversionEngine
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
        "absolute path."
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
        )
    return _engine


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
                return None, result.error
            markdown = result.markdown
            if clean:
                markdown, _stats = clean_markdown(markdown)
    while len(_convert_cache) >= _CACHE_MAX:
        _convert_cache.pop(next(iter(_convert_cache)))
    _convert_cache[key] = markdown
    return markdown, None


def _resolve(file_path: str) -> tuple[str | None, str | None]:
    resolved = os.path.abspath(os.path.expanduser(file_path))
    if not os.path.exists(resolved):
        return None, f"Error: local file not found at '{resolved}'"
    if os.path.isdir(resolved):
        return None, f"Error: '{resolved}' is a directory, not a file"
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
        return f"Error converting '{os.path.basename(resolved)}': {conv_err}"
    assert markdown is not None
    logger.info("Converted (%d chars)", len(markdown))

    report = assess_markdown(markdown)
    noise_note = ""
    if report.binary_noise:
        noise_note = (
            "> ⚠ Warning: this output looks like binary noise (mostly unreadable "
            "characters). The source file may be corrupt or in a format the "
            "converter does not actually understand — treat the content below "
            "with suspicion.\n\n"
        )

    if save_to:
        out_path = os.path.abspath(os.path.expanduser(save_to))
        export.export_single(
            markdown,
            Path(out_path),
            source=os.path.basename(resolved),
            front_matter=Settings.load().export_front_matter,
        )
        return f"Saved Markdown to {out_path}. {report.summary()}"

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
def convert_outline(file_path: str, clean: bool = True) -> dict:
    """Convert a file and return its structure WITHOUT the content — title,
    quality report, and a numbered list of sections (split on # headings) with
    token estimates. Follow up with convert_section to read parts that fit
    your context. The conversion is cached, so outline + section reads don't
    re-convert.
    """
    resolved, err = _resolve(file_path)
    if err:
        return {"error": err}
    markdown, conv_err = _convert_cached(resolved, clean)
    if conv_err is not None:
        return {"error": conv_err}
    assert markdown is not None
    sections = export.split_chapters(markdown)
    return {
        "title": export.document_title(markdown, os.path.basename(resolved)),
        "est_tokens": export.estimate_tokens(markdown),
        "quality": assess_markdown(markdown).as_dict(),
        "sections": [
            {"index": i, "title": s.title or "(document)", "est_tokens": s.est_tokens}
            for i, s in enumerate(sections)
        ],
    }


@mcp.tool
def convert_section(file_path: str, section_index: int, clean: bool = True) -> str:
    """Return one section of a converted document (see convert_outline for
    the section list). Uses the cached conversion when available.
    """
    resolved, err = _resolve(file_path)
    if err:
        return err
    markdown, conv_err = _convert_cached(resolved, clean)
    if conv_err is not None:
        return f"Error converting '{os.path.basename(resolved)}': {conv_err}"
    assert markdown is not None
    sections = export.split_chapters(markdown)
    if not 0 <= section_index < len(sections):
        return f"Error: section_index must be 0..{len(sections) - 1}"
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
        return "Error: only http/https URLs are supported."
    suffix = Path(parsed.path).suffix.lower() or ".html"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MarkdownSidekick/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(_URL_MAX_BYTES + 1)
        if len(data) > _URL_MAX_BYTES:
            return "Error: download exceeds the 50 MB limit."
    except Exception as exc:
        return f"Error downloading '{url}': {type(exc).__name__}: {exc}"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / f"download{suffix}"
        tmp.write_bytes(data)
        with _lock:
            with contextlib.redirect_stdout(sys.stderr):
                result = _get_engine().convert_file(tmp)
                markdown = result.markdown
                if result.ok and clean:
                    markdown, _stats = clean_markdown(markdown)
    if not result.ok:
        return f"Error converting '{url}': {result.error}"
    if len(markdown) > max_chars:
        markdown = markdown[:max_chars] + f"\n\n_(Truncated at {max_chars:,} characters.)_"
    return markdown


@mcp.tool
def list_capabilities() -> dict:
    """Report which local conversion engines are available in this install."""
    from . import audio, mineru, ocr

    s = Settings.load()
    return {
        "image_ocr": ocr.ocr_available(),
        "pdf_ocr": ocr.pdf_ocr_available(),
        "audio_transcription": audio.audio_available(),
        "mineru_endpoint": mineru.mineru_configured(s.mineru_endpoint),
        "whisper_model": s.whisper_model,
    }


def main() -> None:
    mcp.run(show_banner=False)  # stdio transport by default


if __name__ == "__main__":
    main()
