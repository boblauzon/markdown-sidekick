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

# Keep startup quiet and offline-safe: no banner, no pypi update check. Must be
# set before importing fastmcp, which reads these at import time.
os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")  # Literal: stable|prerelease|off

from fastmcp import FastMCP

from .converter import ConversionEngine
from .cleanup import clean_markdown
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
        )
    return _engine


@mcp.tool
def convert_local_file(file_path: str, clean: bool = True) -> str:
    """Convert a local file to Markdown and return the Markdown text.

    Handles PDFs (incl. scanned/vector via OCR), Office docs, HTML, CSV/JSON,
    images (OCR), and audio (transcription) — all locally.

    Args:
        file_path: Absolute path to the local file to convert.
        clean: Tidy the output (strip page-number/header noise, fence code,
            remove scrambled table-of-contents tables). Defaults to true.
    """
    resolved = os.path.abspath(os.path.expanduser(file_path))
    if not os.path.exists(resolved):
        return f"Error: local file not found at '{resolved}'"
    if os.path.isdir(resolved):
        return f"Error: '{resolved}' is a directory, not a file"

    logger.info("Converting %s", resolved)
    # Serialise the whole conversion: only one thread may hold the global stdout
    # redirect and touch the shared model singletons at a time.
    with _lock:
        with contextlib.redirect_stdout(sys.stderr):
            result = _get_engine().convert_file(resolved)
            if result.ok and clean:
                result_md, _stats = clean_markdown(result.markdown)
            else:
                result_md = result.markdown

    if not result.ok:
        logger.warning("Conversion failed: %s", result.error)
        return f"Error converting '{os.path.basename(resolved)}': {result.error}"
    logger.info("Converted via %s (%d chars)", result.engine, len(result_md))
    return result_md


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
