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


def run() -> None:
    errors.install_crash_hooks()
    try:
        import markdown_it  # noqa: F401 - the preview renderer
        import webview
        from webview.dom import DOMEventHandler
    except ImportError as exc:
        _cant_start('the pywebview / markdown-it-py packages (pip install "markdown-sidekick[webui]")', exc)
    try:
        # Importing the Windows backend is what picks the engine: without the
        # WebView2 runtime pywebview silently falls back to MSHTML (IE11),
        # which can't run this page — a blank window and no error.
        from webview.platforms import winforms
    except Exception as exc:
        _cant_start("the .NET Framework 4.6.2+ that pywebview runs on", exc)
    if winforms.renderer != "edgechromium":
        _cant_start(f"Microsoft Edge WebView2 Runtime (pywebview would use {winforms.renderer})")

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
    # The frozen build copies the icon beside the page; a source checkout
    # has it in assets/.
    icons = [_STATIC / "MarkdownSidekick.ico", Path(__file__).resolve().parents[3] / "assets" / "MarkdownSidekick.ico"]
    icon = next((str(p) for p in icons if p.exists()), None)
    webview.start(private_mode=True, icon=icon, debug=bool(os.environ.get("MARKDOWN_SIDEKICK_WEBUI_DEVTOOLS")))
