"""Error codes: every failure the app reports, in plain words, with a next step.

Each code is stable (``MS-<area><nn>``, listed in USERGUIDE.md → *Error
codes*) and pairs WHAT happened with WHAT TO DO, plus an optional ``action``:
a one-click trigger the GUI renders as a button ("Turn on OCR & retry").
:func:`report` records every incident in the always-on error log
(debuglog.write_error) under a short reference the user can quote, so a
message on screen leads straight to the traceback on disk.

Areas: 1xx conversion · 2xx saving · 3xx local AI · 4xx figures ·
5xx settings · 6xx system (clipboard, folders, reports) · 9xx unexpected.

Stdlib only — settings.py imports this module.
"""

from __future__ import annotations

import errno
import platform
import re
import socket
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import debuglog


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    title: str  # what happened, in plain words
    next_step: str  # what the user can do about it
    action: str = ""  # one-click fix (a key of ACTIONS), "" = none
    severity: str = "error"  # "error" | "warning" | "info"


# One-click fixes: action key -> button label. The GUI wires each key to a
# handler; other front-ends print the next step instead.
ACTIONS: dict[str, str] = {
    "retry": "↻  Retry",
    "retry_failed": "↻  Retry failed files",
    "enable_ocr": "Turn on OCR & retry",
    "enable_audio": "Turn on transcription & retry",
    "ocr_cpu": "Use CPU for OCR & retry",
    "settings": "Open Settings",
    "settings_conversion": "Open Conversion settings",
    "settings_output": "Open Output settings",
    "settings_ai": "Open Local AI settings",
    "remove": "Remove from list",
    "add_files": "Add files…",
    "choose_folder": "Choose another folder…",
    "save_again": "Save again",
    "copy_again": "Copy again",
    "raw_preview": "Show raw Markdown",
    "open_logs": "Open log folder",
}

_SPECS: tuple[ErrorSpec, ...] = (
    # -- 1xx conversion -------------------------------------------------------
    ErrorSpec(
        "MS-101",
        "This document is password-protected or encrypted.",
        "Remove the protection (open it and print or export it to a new file), then retry.",
        "retry",
    ),
    ErrorSpec(
        "MS-102",
        "The file is locked or in use by another program.",
        "Close the program that has it open (often a PDF reader or Office), then retry.",
        "retry",
    ),
    ErrorSpec(
        "MS-103",
        "The file can't be found — it may have been moved, renamed, or deleted.",
        "Add the file again from its current location.",
        "remove",
    ),
    ErrorSpec(
        "MS-104",
        "That's a folder, not a file.",
        "Add the files inside it instead.",
        "add_files",
    ),
    ErrorSpec(
        "MS-105",
        "No conversion engine understands this file type.",
        "Export the content as PDF, DOCX, HTML, or another supported format, then add that file.",
    ),
    ErrorSpec(
        "MS-106",
        "The conversion finished but produced no text.",
        "The file may be corrupt, image-only, or a binary format with the wrong extension. "
        "Re-export it in a supported format, then add it again.",
    ),
    ErrorSpec(
        "MS-107",
        "The file appears to be corrupt or incomplete.",
        "Re-download or re-export the file, then retry.",
        "retry",
    ),
    ErrorSpec(
        "MS-108",
        "The file is too large to convert with the memory available.",
        "Close other programs (or split the document into parts), then retry.",
        "retry",
    ),
    ErrorSpec(
        "MS-109",
        "The audio/video stream couldn't be decoded.",
        "Convert it to a common format (MP3, WAV, or MP4), then add that file.",
    ),
    ErrorSpec(
        "MS-110",
        "This file has no text layer — it's a scan or an image — and OCR is turned off.",
        "Turn on OCR so the text can be read from the images.",
        "enable_ocr",
    ),
    ErrorSpec(
        "MS-111",
        "The speech-recognition model couldn't be downloaded.",
        "Connect to the internet once so the model can download (it's kept afterwards), "
        "then retry.",
        "retry",
    ),
    ErrorSpec(
        "MS-112",
        "The transcription engine failed on this file.",
        "Choose a smaller Whisper model in Settings → Conversion, then retry.",
        "settings_conversion",
    ),
    ErrorSpec(
        "MS-113",
        "The OCR engine failed.",
        "Switch OCR to the CPU — the graphics driver is the usual cause — and retry.",
        "ocr_cpu",
    ),
    ErrorSpec(
        "MS-114",
        "The MinerU server didn't return a result, so the PDF was converted locally.",
        "Check the server is running at the endpoint in Settings → Conversion, "
        "or clear the endpoint to stop using it.",
        "settings_conversion",
        "warning",
    ),
    ErrorSpec(
        "MS-115",
        "Audio/video transcription is turned off.",
        "Turn on transcription to convert this file.",
        "enable_audio",
    ),
    ErrorSpec(
        "MS-120",
        "The column-aware PDF reader failed, so the basic reader was used — "
        "columns and tables may be out of order.",
        "Retry. If it happens again, report it with this error's reference.",
        "retry",
        "warning",
    ),
    ErrorSpec(
        "MS-121",
        "OCR failed on this PDF's scanned pages, so only its text layer was kept.",
        "Switch OCR to the CPU — the graphics driver is the usual cause — and retry.",
        "ocr_cpu",
        "warning",
    ),
    ErrorSpec(
        "MS-122",
        "OCR failed on this image, so only basic image information was extracted.",
        "Switch OCR to the CPU — the graphics driver is the usual cause — and retry.",
        "ocr_cpu",
        "warning",
    ),
    ErrorSpec(
        "MS-123",
        "Transcription failed, so only basic file information was extracted.",
        "Choose a smaller Whisper model in Settings → Conversion, then retry.",
        "settings_conversion",
        "warning",
    ),
    ErrorSpec(
        "MS-124",
        "The page-by-page PDF reader failed, so this PDF has no page anchors.",
        "Retry. If it happens again, report it with this error's reference.",
        "retry",
        "warning",
    ),
    ErrorSpec(
        "MS-125",
        "This file couldn't be opened as a PDF (it may be damaged, or not a PDF at all), "
        "so only its raw text was converted.",
        "Re-download or re-export the PDF, then retry.",
        "retry",
        "warning",
    ),
    ErrorSpec(
        "MS-130",
        "The address couldn't be downloaded.",
        "Check the address and your internet connection, then retry.",
        "retry",
    ),
    ErrorSpec(
        "MS-131",
        "That link can't be converted: only http/https downloads up to 50 MB are supported.",
        "Download the file yourself and convert the local copy.",
    ),
    ErrorSpec(
        "MS-132",
        "There is no section with that number.",
        "Get the section list from convert_outline and use one of its indexes "
        "(with the same max_tokens).",
    ),
    ErrorSpec(
        "MS-199",
        "The conversion engine reported an unexpected error.",
        "Retry. If it keeps failing, report it — the technical details say why.",
        "retry",
    ),
    # -- 2xx saving -----------------------------------------------------------
    ErrorSpec(
        "MS-201",
        "Markdown Sidekick isn't allowed to write to that folder.",
        "Choose a different folder, such as Documents.",
        "choose_folder",
    ),
    ErrorSpec(
        "MS-202",
        "The disk is full.",
        "Free up space, or choose a folder on another drive.",
        "choose_folder",
    ),
    ErrorSpec(
        "MS-203",
        "The save location's path is too long for Windows.",
        "Choose a folder with a shorter path, such as C:\\Markdown.",
        "choose_folder",
    ),
    ErrorSpec(
        "MS-204",
        "A file being saved is open in another program.",
        "Close the program that has it open (often a Markdown editor), then save again.",
        "save_again",
    ),
    ErrorSpec(
        "MS-299",
        "The Markdown couldn't be saved.",
        "Try saving to a different folder. If it keeps failing, report it with this error's reference.",
        "choose_folder",
    ),
    # -- 3xx local AI (optional extras: the save itself still succeeds) --------
    ErrorSpec(
        "MS-301",
        "The local AI didn't answer, so its step (summary, captions or polish) was skipped.",
        "Start your local AI app (Ollama, LM Studio…), or clear the model in "
        "Settings → Local AI to stop using it.",
        "settings_ai",
        "warning",
    ),
    ErrorSpec(
        "MS-302",
        "The local AI doesn't have the chosen model, so its step was skipped.",
        "Download the model (e.g. ollama pull llama3.2) or pick an installed one in "
        "Settings → Local AI.",
        "settings_ai",
        "warning",
    ),
    ErrorSpec(
        "MS-303",
        "The local AI took too long, so its step was skipped.",
        "Pick a smaller or faster model in Settings → Local AI.",
        "settings_ai",
        "warning",
    ),
    ErrorSpec(
        "MS-304",
        "The AI summary didn't pass the quality checks, so it was left out.",
        "Nothing needs fixing. For better summaries, try another summary model in "
        "Settings → Local AI.",
        "settings_ai",
        "info",
    ),
    ErrorSpec(
        "MS-399",
        "The local AI step failed and was skipped.",
        "Check the endpoint and model in Settings → Local AI.",
        "settings_ai",
        "warning",
    ),
    # -- 4xx figures ------------------------------------------------------------
    ErrorSpec(
        "MS-401",
        "Figures couldn't be extracted, so the Markdown was saved without them.",
        "Save again, or turn off figure extraction in Settings → Output.",
        "settings_output",
        "warning",
    ),
    ErrorSpec(
        "MS-402",
        "Some figures couldn't be saved; the rest of the document is complete.",
        "If the missing figures matter, report the file with this error's reference.",
        "",
        "warning",
    ),
    # -- 5xx settings -----------------------------------------------------------
    ErrorSpec(
        "MS-501",
        "Your settings file couldn't be read, so the defaults were loaded.",
        "The old file was kept as settings.json.bad — check your choices in Settings.",
        "settings",
        "warning",
    ),
    ErrorSpec(
        "MS-502",
        "Your settings couldn't be saved.",
        "Make sure the folder %LOCALAPPDATA%\\MarkdownSidekick isn't read-only or full, "
        "then save again.",
    ),
    # -- 6xx system -------------------------------------------------------------
    ErrorSpec(
        "MS-601",
        "The clipboard is busy — another program is using it.",
        "Wait a moment, then copy again.",
        "copy_again",
        "warning",
    ),
    ErrorSpec(
        "MS-602",
        "Windows couldn't open that folder.",
        "Open it yourself — its path is in the details.",
        "",
        "warning",
    ),
    ErrorSpec(
        "MS-603",
        "The diagnostic report couldn't be created.",
        "Choose another folder for the report, or open the log folder and send the files "
        "from there.",
        "open_logs",
    ),
    ErrorSpec(
        "MS-604",
        "The built-in user guide couldn't be loaded — a file is missing from this installation.",
        "Reinstall Markdown Sidekick to restore it.",
    ),
    # -- 9xx unexpected ---------------------------------------------------------
    ErrorSpec(
        "MS-900",
        "Markdown Sidekick hit an unexpected error and had to stop.",
        "Restart the app. The details are in the error log — send them to support.",
        "open_logs",
    ),
    ErrorSpec(
        "MS-901",
        "Something went wrong inside Markdown Sidekick, but it recovered.",
        "Carry on. If anything looks wrong, restart the app and report this error's reference.",
        "open_logs",
    ),
    ErrorSpec(
        "MS-902",
        "Conversion stopped unexpectedly.",
        "The files that didn't finish are marked as failed — retry them, or restart the app.",
        "retry_failed",
    ),
    ErrorSpec(
        "MS-903",
        "The preview couldn't be drawn for this file.",
        "Show the raw Markdown instead — Copy and Save still work.",
        "raw_preview",
        "warning",
    ),
)

CATALOG: dict[str, ErrorSpec] = {spec.code: spec for spec in _SPECS}

_AREAS = (
    ("1", "Converting a file"),
    ("2", "Saving"),
    ("3", "Local AI (optional extras — the save itself still succeeds)"),
    ("4", "Figures"),
    ("5", "Settings"),
    ("6", "Clipboard, folders, reports, help"),
    ("9", "Unexpected errors"),
)


def guide_tables() -> str:
    """The *Error codes* tables of USERGUIDE.md, generated from the catalog
    (``python -m markdown_sidekick.errors`` prints them; a test keeps the
    guide in step)."""
    out: list[str] = []
    for digit, title in _AREAS:
        out += [
            f"**{title}**",
            "",
            "| Code | What happened | What to do | One-click fix |",
            "| ---- | ------------- | ---------- | ------------- |",
        ]
        for code, spec in CATALOG.items():
            if code[3] != digit:
                continue
            button = ACTIONS.get(spec.action, "—").replace("↻  ", "")
            mark = {"warning": " *(warning)*", "info": " *(note)*"}.get(spec.severity, "")
            out.append(f"| {code} | {spec.title}{mark} | {spec.next_step} | {button} |")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------
@dataclass
class Incident:
    """One reported occurrence of an error code."""

    spec: ErrorSpec
    ref: str
    detail: str = ""  # the technical message (exception text)
    where: str = ""
    context: dict = field(default_factory=dict)
    log_path: Path | None = None

    @property
    def code(self) -> str:
        return self.spec.code

    @property
    def title(self) -> str:
        return self.spec.title

    @property
    def next_step(self) -> str:
        return self.spec.next_step

    @property
    def action(self) -> str:
        return self.spec.action

    @property
    def severity(self) -> str:
        return self.spec.severity

    def reference(self) -> str:
        """The line a user can quote: ``Error MS-102 · Ref 7F3A2C``."""
        return f"Error {self.code} · Ref {self.ref}"

    def one_line(self) -> str:
        return f"{self.title} {self.next_step} ({self.code}, ref {self.ref})"

    def details_text(self) -> str:
        """Everything a support request needs, ready to paste."""
        from . import __version__

        lines = [
            f"Markdown Sidekick {__version__} — {self.reference()}",
            f"What happened: {self.title}",
            f"Next step: {self.next_step}",
        ]
        if self.where:
            lines.append(f"Where: {self.where}")
        for key, value in self.context.items():
            lines.append(f"{key}: {value}")
        if self.detail:
            lines.append(f"Technical details: {self.detail}")
        lines.append(f"System: {platform.platform()}, Python {sys.version.split()[0]}")
        if self.log_path:
            lines.append(f"Error log: {self.log_path}")
        return "\n".join(lines)


# The same error again within this window is the same incident (a Tk
# callback failing on every tick must not write a line every 60 ms).
_DEDUPE_S = 30.0
_recent: dict[tuple[str, str, str], tuple[float, Incident]] = {}
_recent_lock = threading.Lock()


def report(
    code: str,
    *,
    exc: BaseException | None = None,
    detail: str = "",
    where: str = "",
    **context: Any,
) -> Incident:
    """Record an incident in the error log (always) and the debug trace (when
    on); returns it for display. Never raises."""
    spec = CATALOG.get(code) or CATALOG["MS-901"]
    if exc is not None and not detail:
        detail = f"{type(exc).__name__}: {exc}"
    key = (spec.code, where, detail)
    now = time.monotonic()
    with _recent_lock:
        hit = _recent.get(key)
        if hit and now - hit[0] < _DEDUPE_S:
            return hit[1]
    incident = Incident(spec, uuid.uuid4().hex[:6].upper(), detail, where, dict(context))
    tb = "".join(traceback.format_exception(exc)) if exc is not None else ""
    from . import __version__

    record = {
        "t": datetime.now().isoformat(timespec="milliseconds"),
        "ref": incident.ref,
        "code": spec.code,
        "severity": spec.severity,
        "title": spec.title,
        "where": where,
        "detail": detail,
        "tb": tb,
        "context": context,
        "thread": threading.current_thread().name,
        "app_version": __version__,
        "python": sys.version.split()[0],
        "os": platform.platform(),
        "debug_session": debuglog.session_name(),
    }
    incident.log_path = debuglog.write_error(record)
    debuglog.event(
        "error",
        level="error" if spec.severity == "error" else "warn",
        code=spec.code,
        ref=incident.ref,
        where=where,
        detail=detail,
        tb=tb,
        **{f"ctx_{k}": v for k, v in context.items()},
    )
    with _recent_lock:
        for stale in [k for k, (t, _) in _recent.items() if now - t >= _DEDUPE_S]:
            del _recent[stale]
        _recent[key] = (now, incident)
    return incident


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
# Matched against the raw error (which includes the exception type name, e.g.
# "PDFPasswordIncorrect: ..."); first hit wins, so specific patterns come
# first. Compiled at import so a malformed addition fails loudly in tests.
_CONVERSION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), code)
    for pattern, code in (
        (r"password|encrypt|decrypt", "MS-101"),
        (
            r"WinError 32|being used by another process|Errno 13|Permission denied|"
            r"PermissionError",
            "MS-102",
        ),
        (r"File does not exist|FileNotFoundError|No such file|cannot find the (?:file|path)", "MS-103"),
        (r"Path is a directory", "MS-104"),
        (r"UnsupportedFormatException|[Uu]nsupported format|no converter", "MS-105"),
        (r"EmptyOutputError|produced no text", "MS-106"),
        # Includes the shapes pdfminer raises through markitdown's wrapper
        # ("PdfConverter threw PSEOF/PDFSyntaxError with message ...").
        (
            r"BadZipFile|not a zip file|corrupt|damaged|truncated|EOFError|"
            r"Unexpected EOF|PSEOF|PSSyntaxError|PDFSyntaxError|No /Root object|"
            r"invalid.{0,20}header",
            "MS-107",
        ),
        (r"MemoryError", "MS-108"),
        (r"codec|moov atom|InvalidDataError|DecoderNotFound", "MS-109"),
    )
)

# Exception messages embed the file path in quotes; its words must not steer
# the diagnosis (a locked "passwords-export.xlsx" is not an encryption error).
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def classify_conversion(error: str) -> str:
    """The code for a raw conversion error message (MS-199 when unknown)."""
    scrubbed = _QUOTED_RE.sub("", error)
    for pattern, code in _CONVERSION_PATTERNS:
        if pattern.search(scrubbed):
            return code
    return "MS-199"


_NETWORK_RE = re.compile(
    r"ConnectionError|URLError|getaddrinfo|Name or service|NameResolution|"
    r"HTTPSConnectionPool|LocalEntryNotFound|offline|timed out|Max retries|SSLError",
    re.IGNORECASE,
)


def is_network_error(exc: BaseException | str) -> bool:
    text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    return bool(_NETWORK_RE.search(text))


def classify_os_error(exc: BaseException) -> str:
    """The saving code for an exception raised while writing files."""
    winerror = getattr(exc, "winerror", None)
    err = getattr(exc, "errno", None)
    if winerror in (32, 33):  # sharing / lock violation: the file is open
        return "MS-204"
    if winerror in (39, 112) or err == errno.ENOSPC:
        return "MS-202"
    if winerror == 206 or err == errno.ENAMETOOLONG:
        return "MS-203"
    if isinstance(exc, PermissionError) or err in (errno.EACCES, errno.EPERM, errno.EROFS):
        return "MS-201"
    return "MS-299"


def classify_http(exc: BaseException) -> str:
    """The local-AI code for a failed request to the model server."""
    import urllib.error

    if isinstance(exc, urllib.error.HTTPError):
        return "MS-302" if exc.code == 404 else "MS-399"
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
        return "MS-303"
    if isinstance(exc, (urllib.error.URLError, ConnectionError, OSError)):
        return "MS-301"
    return "MS-399"


# ---------------------------------------------------------------------------
# Unexpected errors
# ---------------------------------------------------------------------------
_listeners: list[Callable[[Incident], None]] = []
_hooks_installed = False


def add_listener(callback: Callable[[Incident], None]) -> None:
    """Called with each incident from an unhandled thread exception (the GUI
    posts it to its event queue — only the Tk thread may show a dialog)."""
    _listeners.append(callback)


def install_crash_hooks() -> None:
    """Route uncaught exceptions (main thread → MS-900, other threads →
    MS-901) into the error log, then on to the previous hooks."""
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    previous_sys = sys.excepthook
    previous_thread = threading.excepthook

    def sys_hook(exc_type, exc, tb) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            report("MS-900", exc=exc, where="main thread")
        previous_sys(exc_type, exc, tb)

    def thread_hook(args) -> None:
        if args.exc_type is not SystemExit and args.exc_value is not None:
            name = args.thread.name if args.thread else "?"
            incident = report("MS-901", exc=args.exc_value, where=f"thread {name}")
            for listener in list(_listeners):
                try:
                    listener(incident)
                except Exception:
                    pass
        previous_thread(args)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(guide_tables())
