"""Logs for troubleshooting: an always-on error log and an opt-in debug trace.

Both live under ``%LOCALAPPDATA%\\MarkdownSidekick\\logs`` (override with the
``MARKDOWN_SIDEKICK_LOG_DIR`` environment variable):

* ``errors.jsonl`` — ALWAYS written. One JSON line per error incident (see
  :func:`errors.report`): code, reference, traceback, context. Rotated at
  2 MB, three older generations kept.
* ``sessions/<stamp>-<pid>-<source>/`` — only in DEBUG MODE (Settings →
  Diagnostics, ``--debug``, or ``MARKDOWN_SIDEKICK_DEBUG=1``):
  ``events.jsonl``, a timeline of what the pipeline decided and how long each
  step took; ``snapshots/`` with every conversion's raw and cleaned Markdown
  (optional); ``faulthandler.log`` with stack dumps from native crashes and
  UI hangs.

Nothing here writes to stdout (the MCP server's JSON-RPC channel) or leaves
the machine. With debug mode off every trace call is one flag check. Stdlib
only — settings.py and export.py import this module at startup.
"""

from __future__ import annotations

import contextlib
import contextvars
import faulthandler
import itertools
import json
import locale
import logging
import os
import platform
import re
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

ENV_DEBUG = "MARKDOWN_SIDEKICK_DEBUG"
ENV_LOG_DIR = "MARKDOWN_SIDEKICK_LOG_DIR"
ERROR_LOG = "errors.jsonl"
SESSIONS_DIR = "sessions"

_ERROR_LOG_MAX_BYTES = 2_000_000
_ERROR_LOG_KEEP = 3  # errors.1.jsonl … errors.3.jsonl
_KEEP_SESSIONS = 15
_SESSION_MAX_BYTES = 200_000_000  # a runaway trace stops here, the app doesn't
_STALL_MS = 1_000  # a UI tick this late is a stall worth recording
_HANG_DUMP_S = 20  # UI thread silent this long -> dump every thread's stack
_SAMPLE_CHARS = 160

# Versions worth knowing when a conversion misbehaves. Read from package
# metadata — nothing is imported.
_PACKAGES = (
    "markitdown",
    "pypdfium2",
    "pdfminer.six",
    "rapidocr",
    "onnxruntime",
    "onnxruntime-directml",
    "faster-whisper",
    "ctranslate2",
    "huggingface-hub",
    "fastmcp",
    "pillow",
    "numpy",
    "requests",
    "tkinterdnd2",
)

_lock = threading.RLock()
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("ms_debug_ctx", default={})
_conv_counter = itertools.count(1)
_conv_ids: dict[str, int] = {}


def debug_requested() -> bool:
    """True when the environment asks for debug mode (``--debug`` sets it)."""
    return os.environ.get(ENV_DEBUG, "").strip().lower() in ("1", "true", "yes", "on")


def log_dir() -> Path:
    override = os.environ.get(ENV_LOG_DIR, "").strip()
    if override:
        return Path(override)
    from .settings import app_data_dir  # lazy: settings imports this module

    return app_data_dir("logs")


def error_log_path() -> Path:
    return log_dir() / ERROR_LOG


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _dumps(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, default=str) + "\n"


# ---------------------------------------------------------------------------
# Tier 1: the always-on error log
# ---------------------------------------------------------------------------
def _rotate(path: Path) -> None:
    if not path.exists() or path.stat().st_size < _ERROR_LOG_MAX_BYTES:
        return
    stem, suffix = path.stem, path.suffix
    for n in range(_ERROR_LOG_KEEP, 0, -1):
        older = path.with_name(f"{stem}.{n}{suffix}")
        newer = path if n == 1 else path.with_name(f"{stem}.{n - 1}{suffix}")
        if newer.exists():
            os.replace(newer, older)


def write_error(record: dict) -> Path | None:
    """Append one incident to the error log. Never raises: a logging failure
    must not turn one error into two."""
    try:
        path = error_log_path()
        line = _dumps(record)
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate(path)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return path
    except Exception:
        return None


def error_log_files() -> list[Path]:
    """The error log and its rotated generations, oldest first."""
    path = error_log_path()
    older = [
        path.with_name(f"{path.stem}.{n}{path.suffix}") for n in range(_ERROR_LOG_KEEP, 0, -1)
    ]
    return [p for p in [*older, path] if p.exists()]


# ---------------------------------------------------------------------------
# Tier 2: the debug session trace
# ---------------------------------------------------------------------------
class _Session:
    def __init__(self, directory: Path, source: str, snapshots: bool) -> None:
        self.dir = directory
        self.source = source
        self.snapshots = snapshots
        self.started = time.monotonic()
        self.bytes = 0
        self.truncated = False
        self.last_event = ""
        directory.mkdir(parents=True, exist_ok=True)
        self.events_path = directory / "events.jsonl"
        self._fh = open(self.events_path, "a", encoding="utf-8")
        self.fault_fh = open(directory / "faulthandler.log", "a", encoding="utf-8")

    def write(self, record: dict) -> None:
        line = _dumps(record)
        with _lock:
            if self._fh is None:
                return
            if self.bytes + len(line) > _SESSION_MAX_BYTES:
                if not self.truncated:
                    self.truncated = True
                    self._fh.write(_dumps({"t": _now(), "ev": "session.truncated", "lvl": "warn"}))
                    self._fh.flush()
                return
            self._fh.write(line)
            self._fh.flush()  # a crash must not eat the lines leading up to it
            self.bytes += len(line)
            if record.get("ev") != "ui.stall":
                self.last_event = record.get("ev", "")

    def close(self) -> None:
        with _lock:
            for fh in (self._fh, self.fault_fh):
                with contextlib.suppress(Exception):
                    fh.close()
            self._fh = None


class _EventHandler(logging.Handler):
    """Forwards library log records (WARNING and up) into the timeline."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields: dict[str, Any] = {"logger": record.name, "msg": record.getMessage()}
            if record.exc_info and record.exc_info[1] is not None:
                fields["tb"] = "".join(traceback.format_exception(*record.exc_info))
            event("log", level=record.levelname.lower(), **fields)
        except Exception:
            pass


_LOG_HANDLER = _EventHandler(level=logging.WARNING)
_session: _Session | None = None
_fault_prev_enabled = False
_last_tick: float | None = None
_last_arm = 0.0
_atexit_registered = False


def enabled() -> bool:
    return _session is not None


def session_dir() -> Path | None:
    s = _session
    return s.dir if s else None


def session_name() -> str:
    s = _session
    return s.dir.name if s else ""


def snapshots_enabled() -> bool:
    s = _session
    return bool(s and s.snapshots)


def _prune_sessions(root: Path, keep: int) -> None:
    try:
        dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return
    for old in dirs[: max(0, len(dirs) - keep)]:
        shutil.rmtree(old, ignore_errors=True)


def enable(source: str = "gui", *, snapshots: bool = True) -> Path | None:
    """Start (or retune) the debug session; returns its folder, or None when
    the log folder can't be created (debug mode then simply stays off)."""
    global _session, _fault_prev_enabled, _atexit_registered
    with _lock:
        if _session is not None:
            _session.snapshots = snapshots
            return _session.dir
        root = log_dir() / SESSIONS_DIR
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            root.mkdir(parents=True, exist_ok=True)
            _prune_sessions(root, _KEEP_SESSIONS - 1)
            session = _Session(root / f"{stamp}-{os.getpid()}-{source}", source, snapshots)
        except OSError:
            return None
        _session = session
        _fault_prev_enabled = faulthandler.is_enabled()
        with contextlib.suppress(Exception):
            faulthandler.enable(file=session.fault_fh, all_threads=True)
        # Library warnings (markitdown, pdfminer, pydub, warnings.warn) join
        # the timeline — they often explain an odd conversion.
        logging.captureWarnings(True)
        logging.getLogger().addHandler(_LOG_HANDLER)
        if not _atexit_registered:
            import atexit

            atexit.register(disable)
            _atexit_registered = True
    event("session.start", source=source, snapshots=snapshots, **environment())
    return session.dir


def disable() -> None:
    """End the debug session (flushes, closes, restores faulthandler)."""
    global _session, _last_tick
    s = _session
    if s is None:
        return
    event(
        "session.end",
        uptime_s=round(time.monotonic() - s.started, 1),
        **(memory() or {}),
    )
    with _lock:
        _session = None
        _last_tick = None
        logging.getLogger().removeHandler(_LOG_HANDLER)
        logging.captureWarnings(False)
        with contextlib.suppress(Exception):
            faulthandler.cancel_dump_traceback_later()
            faulthandler.disable()
            if _fault_prev_enabled and sys.stderr is not None:
                faulthandler.enable()
        s.close()


def event(name: str, level: str = "info", **fields: Any) -> None:
    """Record one timeline event (debug mode only)."""
    s = _session
    if s is None:
        return
    record: dict[str, Any] = {
        "t": _now(),
        "rel": round(time.monotonic() - s.started, 3),
        "lvl": level,
        "ev": name,
        "thread": threading.current_thread().name,
    }
    record.update(_ctx.get())
    for key, value in fields.items():
        record[f"f_{key}" if key in record else key] = value
    s.write(record)


def exception(name: str, exc: BaseException, **fields: Any) -> None:
    """Record a caught exception with its traceback (debug mode only)."""
    if _session is None:
        return
    event(
        name,
        level="error",
        error=f"{type(exc).__name__}: {exc}",
        tb="".join(traceback.format_exception(exc)),
        **fields,
    )


class _Span:
    """Times a block; ``span["key"] = value`` adds fields to its event."""

    __slots__ = ("name", "fields", "_t0")

    def __init__(self, name: str, fields: dict) -> None:
        self.name = name
        self.fields = fields
        self._t0 = 0.0

    def __setitem__(self, key: str, value: Any) -> None:
        self.fields[key] = value

    def __enter__(self) -> "_Span":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        ms = round((time.perf_counter() - self._t0) * 1000, 1)
        if exc is not None:
            event(
                self.name,
                level="error",
                ms=ms,
                ok=False,
                error=f"{exc_type.__name__}: {exc}",
                tb="".join(traceback.format_exception(exc_type, exc, tb)),
                **self.fields,
            )
        else:
            event(self.name, ms=ms, ok=True, **self.fields)
        return False  # never swallow


class _NullSpan:
    __slots__ = ()

    def __setitem__(self, key: str, value: Any) -> None:
        pass

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, *exc) -> bool:
        return False


_NULL_SPAN = _NullSpan()


def span(name: str, **fields: Any) -> "_Span | _NullSpan":
    """``with span("route", route="pdflayout") as sp:`` — logs duration,
    outcome and (on an exception, which still propagates) the traceback."""
    return _Span(name, fields) if _session is not None else _NULL_SPAN


@contextlib.contextmanager
def context(**fields: Any) -> Iterator[None]:
    """Fields merged into every event logged inside the block (this thread)."""
    token = _ctx.set({**_ctx.get(), **fields})
    try:
        yield
    finally:
        _ctx.reset(token)


def current_context() -> dict:
    """This thread's event context — to carry onto a worker thread."""
    return dict(_ctx.get())


def new_conversion(path: Path) -> int:
    """A sequence number tying one file's events and snapshots together."""
    conv = next(_conv_counter)
    _conv_ids[str(path)] = conv
    return conv


def file_context(path: Path) -> contextlib.AbstractContextManager:
    """Event context for work on an already-converted file (cleanup, save)."""
    if _session is None:
        return contextlib.nullcontext()
    return context(file=path.name, conv=_conv_ids.get(str(path), 0))


_UNSAFE_NAME_RE = re.compile(r"[^\w.-]+")


def snapshot(label: str, text: str) -> Path | None:
    """Keep a copy of ``text`` (e.g. raw / cleaned Markdown) for quality
    analysis; named after the file in the current context."""
    s = _session
    if s is None or not s.snapshots or not text:
        return None
    ctx = _ctx.get()
    if "file" not in ctx:  # not attributable to a document: not worth keeping
        return None
    stem = _UNSAFE_NAME_RE.sub("_", Path(str(ctx["file"])).stem)[:60]
    path = s.dir / "snapshots" / f"{int(ctx.get('conv', 0)):03d}-{stem}.{label}.md"
    try:
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        return None
    event("snapshot", label=label, path=path.name, chars=len(text))
    return path


def sample(text: str) -> str:
    """A log-sized excerpt of one line of document text."""
    text = text.strip()
    return text if len(text) <= _SAMPLE_CHARS else text[: _SAMPLE_CHARS - 1] + "…"


class PageTimer:
    """Wraps an ``on_page(current, total)`` callback to time each page."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.durations: list[tuple[int, float]] = []
        self._last = time.perf_counter()

    def wrap(self, callback: Callable[[int, int], None] | None):
        if _session is None:
            return callback

        def timed(current, total) -> None:
            now = time.perf_counter()
            self.durations.append((int(current), (now - self._last) * 1000))
            self._last = now
            if callback is not None:
                callback(current, total)

        return timed

    def emit(self, **fields: Any) -> None:
        if _session is None or not self.durations:
            return
        ms = sorted(d for _, d in self.durations)
        slowest = sorted(self.durations, key=lambda p: -p[1])[:5]
        event(
            "pages.timing",
            route=self.name,
            pages=len(ms),
            total_ms=round(sum(ms)),
            mean_ms=round(sum(ms) / len(ms), 1),
            p95_ms=round(ms[min(len(ms) - 1, int(len(ms) * 0.95))], 1),
            max_ms=round(ms[-1], 1),
            slowest=[[page, round(d)] for page, d in slowest],
            **fields,
        )


# ---------------------------------------------------------------------------
# UI health: stalls and hangs
# ---------------------------------------------------------------------------
def ui_tick() -> None:
    """Called from the Tk event pump. A late tick is logged as a stall; the
    faulthandler watchdog is re-armed, so a UI thread silent for
    _HANG_DUMP_S seconds dumps every thread's stack to faulthandler.log."""
    global _last_tick, _last_arm
    s = _session
    if s is None:
        return
    now = time.monotonic()
    if _last_tick is not None:
        gap = (now - _last_tick) * 1000
        if gap >= _STALL_MS:
            event("ui.stall", level="warn", gap_ms=round(gap), last_event=s.last_event)
    _last_tick = now
    if now - _last_arm >= 1.0:
        _last_arm = now
        with contextlib.suppress(Exception):
            faulthandler.dump_traceback_later(_HANG_DUMP_S, file=s.fault_fh)


@contextlib.contextmanager
def ui_blocking(what: str) -> Iterator[None]:
    """A deliberate wait on the UI thread (a native file dialog): not a
    stall, and no hang dump while the user browses."""
    global _last_tick
    if _session is None:
        yield
        return
    event("ui.dialog", what=what)
    with contextlib.suppress(Exception):
        faulthandler.cancel_dump_traceback_later()
    try:
        yield
    finally:
        _last_tick = None


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
def memory() -> dict | None:
    """Current and peak working set of this process, in MB."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.K32GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD
            ]
            pmc = _PMC()
            pmc.cb = ctypes.sizeof(pmc)
            if not kernel32.K32GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb
            ):
                return None
            mb = 1024 * 1024
            return {
                "rss_mb": round(pmc.WorkingSetSize / mb, 1),
                "peak_mb": round(pmc.PeakWorkingSetSize / mb, 1),
            }
        import resource  # POSIX

        return {"peak_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)}
    except Exception:
        return None


def _total_ram_mb() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _MS(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MS()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return round(status.ullTotalPhys / (1024 * 1024))
    except Exception:
        return None


def package_versions() -> dict[str, str]:
    from importlib import metadata

    versions = {}
    for name in _PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except Exception:
            continue
    return versions


def environment() -> dict:
    """Everything about this install that could explain a difference in
    behaviour between two machines."""
    from . import __version__

    info: dict[str, Any] = {
        "app_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "ram_total_mb": _total_ram_mb(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "argv": sys.argv,
        "fs_encoding": sys.getfilesystemencoding(),
        "pid": os.getpid(),
    }
    with contextlib.suppress(Exception):
        info["locale"] = locale.getlocale()
    info["packages"] = package_versions()
    return info
