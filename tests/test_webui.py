"""The web UI's Python bridge (webui/bridge.py), driven without a window."""

from __future__ import annotations

import sys
import time

import pytest

from markdown_sidekick.settings import Settings
from markdown_sidekick.webui import bridge


class FakeWindow:
    def __init__(self):
        self.scripts: list[str] = []

    def run_js(self, script: str) -> None:
        self.scripts.append(script)

    def create_confirmation_dialog(self, title: str, message: str) -> bool:
        return True


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))  # settings.save lands here
    a = bridge.Api(Settings(enable_ocr=False, enable_audio=False))
    a._window = FakeWindow()  # _attach without the engine warm-up thread
    return a


def _convert(api, path, timeout=60.0):
    api._add([str(path)])
    item = next(i for i in api._items.values() if i.path == path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with api._lock:
            if item.result is not None and not item.preparing and not api._busy:
                return item
        time.sleep(0.05)
    raise AssertionError("conversion did not finish")


def test_preview_renders_and_never_runs_document_html(api, tmp_path):
    src = tmp_path / "notes.md"
    src.write_text(
        "# Notes\n\nSome **bold** text.\n\n<script>alert(1)</script>\n\n"
        '<img src="x" onerror="alert(2)">\n\n[bad](javascript:alert(3))\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n'
        "[web](https://example.com) [settings](ms-settings:about)\n",
        encoding="utf-8",
    )
    item = _convert(api, src)
    page = api.preview(item.id, "rendered")
    assert page["state"] == "done"
    html = page["html"]
    assert "<h1>Notes</h1>" in html and "<strong>bold</strong>" in html and "<table>" in html
    # Raw HTML in a converted document is shown as text, never as markup.
    assert "<script" not in html and "&lt;script&gt;" in html
    assert "<img" not in html and 'href="javascript' not in html
    # Only web links stay links: another scheme could start a program.
    assert 'href="https://example.com"' in html and "ms-settings" not in html and ">settings</a>" in html
    raw = api.preview(item.id, "raw")
    assert "html" not in raw and raw["raw"].startswith("# Notes")
    assert [s["label"] for s in page["stats"]] == ["Quality", "Tokens", "Headings", "Cleanup"]


def test_preview_is_capped_but_says_so(api, tmp_path):
    src = tmp_path / "big.md"
    para = "word " * 60
    src.write_text("\n\n".join(f"{para} {n}" for n in range(1600)), encoding="utf-8")  # ~490k chars
    item = _convert(api, src)
    page = api.preview(item.id, "raw")
    assert page["truncated"] is True
    assert len(page["raw"]) <= bridge.PREVIEW_MAX_CHARS


def test_failed_file_gets_its_code_and_next_step(api, tmp_path):
    src = tmp_path / "broken.docx"
    src.write_bytes(b"PK\x03\x04 not really a zip " * 50)
    item = _convert(api, src)
    page = api.preview(item.id)
    assert page["state"] == "error"
    assert page["code"] == "MS-107" and page["title"] and page["next"]
    assert "Markdown Sidekick" in page["details"]
    row = api._snapshot()["files"][0]
    assert row["status"] == "error" and row["errCode"] == page["code"]


def test_options_persist_and_junk_is_refused(api):
    assert api.set_option("clean", False) == {"ok": True}
    assert api.set_option("aiTarget", "ChatGPT") == {"ok": True}
    assert api.set_option("aiTarget", "Nonsense") == {"ok": False}
    assert api.set_option("export_style", "ai") == {"ok": False}  # not a page option
    saved = Settings.load()
    assert saved.clean_output is False and saved.ai_target == "ChatGPT"


def test_only_web_links_leave_the_window(api, monkeypatch):
    opened = []
    monkeypatch.setattr(bridge.webbrowser, "open", opened.append)
    for url in ("https://example.com/a", "javascript:alert(1)", "file:///C:/Windows", "", None):
        api.open_url(url)
    assert opened == ["https://example.com/a"]


def test_folders_and_duplicates_are_not_added(api, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_convert_pending", lambda: None)
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    api._add([str(f), str(f), str(tmp_path), str(tmp_path / "missing.txt")])
    assert [r["name"] for r in api._snapshot()["files"]] == ["a.txt"]


def test_a_folder_note_is_not_hidden_by_the_added_note(api, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_convert_pending", lambda: None)
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    api._add([str(f), str(tmp_path / "sub")])
    status = api._snapshot()["status"]
    assert "Added 1 file(s)." in status and "Skipped 1 folder(s)" in status and "MS-104" in status


def test_a_fix_that_turns_ocr_on_redraws_the_switch(api):
    assert api._snapshot()["ocr"] is False
    api.fix("enable_ocr")
    assert api._snapshot()["ocr"] is True and Settings.load().enable_ocr is True
    assert '"ocr": true' in api._window.scripts[-1]  # the page was told
    api._window.scripts.clear()
    api.set_option("ocr", False)
    assert '"ocr": false' in api._window.scripts[-1]


def test_the_probe_hook_is_not_part_of_the_page_api(api):
    public = {n for n in dir(api) if not n.startswith("_") and callable(getattr(api, n))}
    assert "probe_add" not in public and "preview" in public


def test_snapshots_are_numbered_so_the_page_can_drop_stale_ones(api):
    first, second = api._snapshot()["seq"], api._snapshot()["seq"]
    assert second > first


def test_clear_all_mid_conversion_does_not_say_converting(api):
    api._busy, api._current = True, "f99"  # the batch's file was cleared from the list
    snap = api._snapshot()
    assert snap["status"].startswith("Ready") and snap["busy"] is False


def test_copy_survives_a_retry_mid_copy(api, tmp_path, monkeypatch):
    src = tmp_path / "a.md"
    src.write_text("# A\n\ntext\n", encoding="utf-8")
    item = _convert(api, src)
    result = item.result
    item.result = None  # a Retry lands between copy() reading the result and cleaning it
    item.evict()
    text, report, _ = api._text_and_report(item, result, True)
    assert text.startswith("# A") and report.score >= 0


def test_without_webview2_the_new_window_says_so(monkeypatch):
    import types

    import webview.platforms

    from markdown_sidekick import webui

    shown = []
    fake = types.SimpleNamespace(renderer="mshtml")  # what pywebview picks with no WebView2 runtime
    monkeypatch.setattr(webui, "_message_box", shown.append)
    monkeypatch.setattr(webui.errors, "install_crash_hooks", lambda: None)
    monkeypatch.setitem(sys.modules, "webview.platforms.winforms", fake)
    monkeypatch.setattr(webview.platforms, "winforms", fake, raising=False)
    with pytest.raises(SystemExit) as stop:
        webui.run()
    assert stop.value.code == 1
    assert "MS-605" in shown[0] and "WebView2" in shown[0]


def test_help_has_the_guide_sections(api):
    titles = [s["title"] for s in api.help()]
    assert any("Error codes" in t for t in titles)
    assert all(s["html"].strip() for s in api.help())
