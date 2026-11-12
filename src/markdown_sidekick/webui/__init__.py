"""Web-view front-end: the Designer prototype in a pywebview (WebView2)
window, driving the same engine as the Tk GUI (ADR-003 spike).

``MARKDOWN_SIDEKICK_WEBUI_PROBE=<file.json>`` runs the spike's measurements
instead of waiting for the user: it adds the files listed in
``MARKDOWN_SIDEKICK_WEBUI_PROBE_FILES`` (``os.pathsep``-separated), times the
preview and a scripted scroll, writes the numbers to the JSON file and
closes the window.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from pathlib import Path

from .. import __app_name__, debuglog, errors

_STATIC = Path(__file__).with_name("static")


def _windows_dark() -> bool:
    """Whether Windows apps use the dark theme (first paint matches it)."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def _message_box(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, __app_name__, 0x10)  # MB_ICONERROR


def _cant_start(missing: str, exc: BaseException | None = None) -> None:
    """MS-605 in the log, then a message box (the windowed exe has no
    console) and exit 1."""
    incident = errors.report("MS-605", exc=exc, detail=missing, where="new window")
    text = f"{incident.title}\n\n{incident.next_step}\n\nMissing: {missing}\n\nError {incident.code} · Ref {incident.ref}"
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    _message_box(text)
    raise SystemExit(1)


def missing() -> tuple[str, BaseException | None] | None:
    """What this PC lacks to show the new window (and the exception that
    said so), or None when it can show it."""
    import importlib.util

    try:
        # The preview renderer is imported at the first preview, not before
        # the window opens: here it's enough that it is installed.
        if importlib.util.find_spec("markdown_it") is None:
            raise ImportError("No module named 'markdown_it'", name="markdown_it")
        import webview  # noqa: F401
        from webview.dom import DOMEventHandler  # noqa: F401
    except ImportError as exc:
        return "the pywebview / markdown-it-py packages (pip install pywebview markdown-it-py)", exc
    try:
        # Importing the Windows backend is what picks the engine: without the
        # WebView2 runtime pywebview silently falls back to MSHTML (IE11),
        # which can't run this page — a blank window and no error.
        from webview.platforms import winforms
    except Exception as exc:
        return "the .NET Framework 4.6.2+ that pywebview runs on", exc
    if winforms.renderer != "edgechromium":
        return f"Microsoft Edge WebView2 Runtime (pywebview would use {winforms.renderer})", None
    return None


# How long a page may take to come up before the window counts as not
# started (a WebView2 that failed without saying so). Generous: a slow PC's
# first start, antivirus scanning the runtime, must not be mistaken for it.
START_TIMEOUT_S = 60.0


class _StartWatch(logging.Handler):
    """WebView2 failing to start (a damaged runtime, a policy, a data folder
    it can't create) is only logged by pywebview, which then shows a blank
    window forever. This notices that, and a page that never comes up, and
    closes the window so the caller can say so and fall back."""

    def __init__(self, window, ready: threading.Event) -> None:
        super().__init__(logging.ERROR)
        self.window, self.ready = window, ready
        self.failure = ""
        self.closed = False  # by the user, before anything went wrong

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if "WebView2 initialization failed" in message:
            self.fail(message.split("exception:", 1)[-1].strip() or message)

    def fail(self, reason: str) -> None:
        if self.failure or self.closed:
            return
        self.failure = reason
        threading.Thread(target=self.window.destroy, daemon=True, name="start-watch").start()

    def on_shown(self) -> None:
        def wait() -> None:
            if not self.ready.wait(START_TIMEOUT_S):
                self.fail(f"the page didn't load within {START_TIMEOUT_S:.0f} s")

        threading.Thread(target=wait, daemon=True, name="start-watch").start()

    def on_closing(self) -> None:
        if not self.failure:
            self.closed = True


def run(fallback: bool = False) -> errors.Incident | None:
    """Show the new window until it closes. If WebView2 doesn't start, the
    window closes: with ``fallback`` (the saved choice) the MS-607 incident
    is returned for the classic window to show; asked for by name, MS-605."""
    errors.install_crash_hooks()
    lack = missing()
    if lack is not None:
        _cant_start(*lack)
    import webview
    from webview.dom import DOMEventHandler

    from .bridge import Api, window_title

    api = Api()
    if api._settings.debug_mode or debuglog.debug_requested():
        debuglog.enable("webui", snapshots=api._settings.debug_snapshots)
    probe = os.environ.get("MARKDOWN_SIDEKICK_WEBUI_PROBE")
    api.probe_path = probe or None
    window = webview.create_window(
        window_title(),
        url=str(_STATIC / "index.html"),
        js_api=api,
        width=1180,
        height=760,
        min_size=(920, 600),
        background_color="#1e1f29" if _windows_dark() else "#f3f2f2",
        text_select=True,
        focus=not probe,
    )
    api._attach(window)

    def on_loaded() -> None:
        # Registering a Python drop handler is what makes pywebview attach
        # each dropped file's full path (the page itself only gets names).
        window.dom.document.events.drop += DOMEventHandler(api._on_drop, True, True)
        if probe:
            files = [f for f in os.environ.get("MARKDOWN_SIDEKICK_WEBUI_PROBE_FILES", "").split(os.pathsep) if f]
            threading.Timer(0.3, api._probe_add, args=(files,)).start()

    window.events.loaded += on_loaded
    watch = _StartWatch(window, api._page_ready)
    window.events.shown += watch.on_shown
    window.events.closing += watch.on_closing
    window.events.closing += api._on_closing  # a downloaded update installs as the window closes
    # The frozen build copies the icon beside the page; a source checkout
    # has it in assets/.
    icons = [_STATIC / "MarkdownSidekick.ico", Path(__file__).resolve().parents[3] / "assets" / "MarkdownSidekick.ico"]
    icon = next((str(p) for p in icons if p.exists()), None)
    pywebview_log = logging.getLogger("pywebview")
    pywebview_log.addHandler(watch)
    try:
        webview.start(private_mode=True, icon=icon, debug=bool(os.environ.get("MARKDOWN_SIDEKICK_WEBUI_DEVTOOLS")))
    finally:
        pywebview_log.removeHandler(watch)
    if not watch.failure:
        return None
    debuglog.event("webui.start_failed", reason=watch.failure, fallback=fallback)
    if not fallback:
        _cant_start(f"a Microsoft Edge WebView2 Runtime that starts ({watch.failure})")
    return errors.report("MS-607", detail=watch.failure, where="new window")
