"""Which window opens (start.py): a flag for one run, else Settings → window,
the new one by default; the new window falls back to the classic one when
this PC can't run it."""

from __future__ import annotations

import json

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

    seen = Opened()

    def missing():
        seen.asked += 1
        return seen.lack

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(webui, "missing", missing)
    monkeypatch.setattr(webui, "run", lambda: seen.append(("new", None)))
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


def test_the_pip_command_reads_its_flags(opened, monkeypatch):
    monkeypatch.setattr(start.sys, "argv", ["markdown-sidekick", "--classic"])
    start.main()
    monkeypatch.setattr(start.sys, "argv", ["markdown-sidekick"])
    start.main()
    assert opened == [("classic", None), ("new", None)]


def test_ms_606_offers_the_webview2_page():
    spec = errors.CATALOG["MS-606"]
    assert spec.severity == "warning" and errors.ACTIONS[spec.action] == "Get WebView2…"
    assert errors.WEBVIEW2_URL.startswith("https://developer.microsoft.com/")
