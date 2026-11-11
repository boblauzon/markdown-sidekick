"""Which window opens (start.py): a flag for one run, else Settings → window,
the new one by default; the new window falls back to the classic one when
this PC can't run it."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from markdown_sidekick import errors, start, ui, webui
from markdown_sidekick.diagnostics import load_errors

LACK = ("Microsoft Edge WebView2 Runtime (pywebview would use mshtml)", None)


@pytest.fixture
def opened(tmp_path, monkeypatch):
    """Records which window opened, with this PC able to run the new window
    unless a test says otherwise (``opened.lack``)."""

    class Opened(list):
        lack = None
        asked = 0
        fails = None  # what the new window's run() returns: an incident = it didn't start

    seen = Opened()
    seen.fallbacks = []

    def missing():
        seen.asked += 1
        return seen.lack

    def run(fallback=False):
        seen.append(("new", None))
        seen.fallbacks.append(fallback)
        return seen.fails

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(webui, "missing", missing)
    monkeypatch.setattr(webui, "run", run)
    monkeypatch.setattr(ui, "run", lambda notice=None: seen.append(("classic", notice)))
    return seen


def _choose(tmp_path, window: str) -> None:
    folder = tmp_path / "MarkdownSidekick"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.json").write_text(json.dumps({"window": window}), encoding="utf-8")


def test_the_new_window_opens_by_default(opened):
    start.open_window()
    assert opened == [("new", None)]


def test_without_webview2_the_classic_window_opens_and_says_why(opened):
    opened.lack = LACK
    start.open_window()
    [(window, notice)] = opened
    assert window == "classic" and notice.code == "MS-606"
    assert notice.action == "webview2" and "WebView2" in notice.detail
    assert any(r.get("ref") == notice.ref and r.get("code") == "MS-606" for r in load_errors(None))


def test_a_chosen_classic_window_opens_without_asking_about_webview2(opened, tmp_path):
    _choose(tmp_path, "classic")
    start.open_window()
    assert opened == [("classic", None)] and opened.asked == 0


def test_a_flag_picks_the_window_for_one_run(opened, tmp_path):
    start.open_window("--classic")
    assert opened == [("classic", None)]
    _choose(tmp_path, "classic")
    opened.lack = LACK  # asked for by name, the new window reports MS-605 itself
    start.open_window("--new-ui")
    assert opened[-1] == ("new", None) and opened.asked == 0
    assert opened.fallbacks == [False]  # by name: MS-605 and exit, no classic window


def test_the_pip_command_reads_its_flags(opened, monkeypatch):
    monkeypatch.setattr(start.sys, "argv", ["markdown-sidekick", "--classic"])
    start.main()
    monkeypatch.setattr(start.sys, "argv", ["markdown-sidekick"])
    start.main()
    assert opened == [("classic", None), ("new", None)]


def test_a_webview2_that_does_not_start_falls_back_to_classic(opened, monkeypatch):
    """In a new process: after a WebView2 window ran, this one's main thread
    is a multithreaded COM apartment and the classic window's drag and drop
    can't start (found by running it)."""
    import subprocess

    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, env=None, **kw: started.append((cmd, env)))
    monkeypatch.delenv(start.NOTICE_ENV, raising=False)
    opened.fails = errors.Incident(errors.CATALOG["MS-607"], ref="abc123", detail="no runtime")
    start.open_window()
    assert opened == [("new", None)] and opened.fallbacks == [True]  # no classic window in this process
    [(cmd, env)] = started
    assert cmd[-1] == "--classic" and cmd[:3] == [sys.executable, "-m", "markdown_sidekick"]
    assert str(Path(start.__file__).resolve().parents[1]) in env["PYTHONPATH"]

    # The new process shows the notice it was handed, and passes it on to nothing.
    monkeypatch.setenv(start.NOTICE_ENV, env[start.NOTICE_ENV])
    start.open_window("--classic")
    window, notice = opened[-1]
    assert window == "classic" and (notice.code, notice.ref, notice.detail) == ("MS-607", "abc123", "no runtime")
    assert start.NOTICE_ENV not in os.environ


def test_a_garbled_notice_is_ignored(opened, monkeypatch):
    monkeypatch.setenv(start.NOTICE_ENV, '{"code": "MS-999"}')
    start.open_window("--classic")
    assert opened == [("classic", None)]


class _Window:
    def __init__(self):
        self.destroyed = threading.Event()

    def destroy(self):
        self.destroyed.set()


def test_the_start_watch_closes_a_window_whose_webview2_failed():
    window, ready = _Window(), threading.Event()
    watch = webui._StartWatch(window, ready)
    log = logging.getLogger("pywebview")
    log.addHandler(watch)
    try:
        log.error("WebView2 initialization failed with exception:\n System.Exception: 0x80070005 Access denied")
    finally:
        log.removeHandler(watch)
    assert window.destroyed.wait(5)
    assert watch.failure == "System.Exception: 0x80070005 Access denied"
    watch.on_closing()  # the destroy's own close event
    assert watch.closed is False


def test_the_start_watch_gives_a_page_time_and_respects_a_user_close(monkeypatch):
    monkeypatch.setattr(webui, "START_TIMEOUT_S", 0.2)
    slow, ready = _Window(), threading.Event()
    watch = webui._StartWatch(slow, ready)
    watch.on_shown()
    assert slow.destroyed.wait(5) and "didn't load within" in watch.failure

    fine, ready = _Window(), threading.Event()
    watch = webui._StartWatch(fine, ready)
    watch.on_shown()
    ready.set()  # the page came up
    time.sleep(0.4)
    assert not fine.destroyed.is_set() and watch.failure == ""

    closed = _Window()
    watch = webui._StartWatch(closed, threading.Event())
    watch.on_closing()  # the user closed it before the page came up
    watch.fail("late")
    assert not closed.destroyed.is_set() and watch.failure == ""


def test_ms_607_offers_the_webview2_page_too():
    spec = errors.CATALOG["MS-607"]
    assert spec.severity == "warning" and spec.action == "webview2"


def test_ms_606_offers_the_webview2_page():
    spec = errors.CATALOG["MS-606"]
    assert spec.severity == "warning" and errors.ACTIONS[spec.action] == "Get WebView2…"
    assert errors.WEBVIEW2_URL.startswith("https://developer.microsoft.com/")
