"""The web UI's Python bridge (webui/bridge.py), driven without a window."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from markdown_sidekick.settings import Settings
from markdown_sidekick.webui import bridge


class FakeWindow:
    def __init__(self):
        self.scripts: list[str] = []
        self.picked = None  # what the next file dialog returns (a tuple of paths, or None)
        self.dialogs: list[tuple] = []
        self.confirm = True
        self.prompts: list[str] = []
        self.on_dialog = lambda: None

    def run_js(self, script: str) -> None:
        self.scripts.append(script)

    def create_confirmation_dialog(self, title: str, message: str) -> bool:
        self.prompts.append(message)
        return self.confirm

    def set_title(self, title: str) -> None:
        self.title = title

    def create_file_dialog(self, dialog_type, **kwargs):
        self.dialogs.append((dialog_type, kwargs))
        self.on_dialog()
        return self.picked


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


@pytest.fixture
def saving(api, monkeypatch):
    """The api with Open-the-folder declined and startfile stubbed: a test
    must never open Explorer."""
    import os

    opened = []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    api._window.confirm = False
    api.opened = opened
    return api


def _convert_all(api, paths, timeout=60.0):
    api._add([str(p) for p in paths])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with api._lock:
            if not api._busy and all(i.result is not None and not i.preparing for i in api._items.values()):
                return
        time.sleep(0.05)
    raise AssertionError("conversion did not finish")


def test_save_one_file_through_a_save_dialog(saving, tmp_path):
    import webview

    src = tmp_path / "notes.md"
    src.write_text("# Field Notes\n\nThe notes have a sentence of their own.\n", encoding="utf-8")
    _convert_all(saving, [src])
    dest = tmp_path / "out" / "Saved.md"
    saving._window.picked = (str(dest),)
    saving.save()
    kind, kwargs = saving._window.dialogs[0]
    assert kind == webview.FileDialog.SAVE and kwargs["save_filename"] == "notes.md"
    text = dest.read_text(encoding="utf-8")
    assert text.startswith("---\ntitle: Field Notes\n") and "source: notes.md" in text
    assert saving._snapshot()["status"] == "Saved Saved.md." and not saving._window.prompts


def test_saving_several_files_to_a_folder_never_collides(saving, tmp_path):
    import webview

    sources = []
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        src = tmp_path / folder / "notes.md"
        src.write_text(f"# Notes {folder}\n\nFolder {folder} has its own sentence.\n", encoding="utf-8")
        sources.append(src)
    _convert_all(saving, sources)
    out = tmp_path / "out"
    saving._window.picked = (str(out),)
    saving.save()
    assert saving._window.dialogs[0][0] == webview.FileDialog.FOLDER
    assert sorted(p.name for p in out.iterdir()) == ["notes-2.md", "notes.md"]
    assert "title: Notes b" in (out / "notes-2.md").read_text(encoding="utf-8")
    assert "Saved 2 file(s)" in saving._snapshot()["status"]
    assert saving._window.prompts and not saving.opened  # asked, declined, nothing opened


def test_chapter_style_saves_a_book_folder(saving, tmp_path):
    src = tmp_path / "book.md"
    src.write_text("".join(f"# Chapter {n}\n\nChapter {n} says something new.\n\n" for n in range(1, 4)),
                   encoding="utf-8")
    _convert_all(saving, [src])
    saving.set_option("exportStyle", "chapters")
    saving._window.picked = (str(tmp_path / "out"),)
    saving.save()
    book = tmp_path / "out" / "book"
    assert (book / "index.md").exists() and (book / "manifest.json").exists()
    assert len(list(book.glob("0*.md"))) == 3


def test_save_greys_out_while_its_dialog_is_open_and_cancel_writes_nothing(saving, tmp_path):
    src = tmp_path / "a.md"
    src.write_text("# A\n\ntext\n", encoding="utf-8")
    _convert_all(saving, [src])
    seen = []
    saving._window.on_dialog = lambda: seen.append(saving._snapshot()["canSave"])
    saving.save()  # picked is None: the dialog was cancelled
    assert seen == [False]
    assert saving._snapshot()["canSave"] is True
    assert [p.name for p in tmp_path.rglob("*.md")] == ["a.md"]


def test_a_failed_save_names_the_code_and_next_step(saving, tmp_path, monkeypatch):
    src = tmp_path / "a.md"
    src.write_text("# A\n\ntext\n", encoding="utf-8")
    _convert_all(saving, [src])

    def denied(*args, **kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(bridge.export, "save", denied)
    saving._window.picked = (str(tmp_path / "out" / "a.md"),)
    saving.save()
    status = saving._snapshot()["status"]
    assert "couldn't be saved (a.md)" in status and "MS-201" in status


def test_nothing_to_save_says_so(saving):
    saving.save()
    assert not saving._window.dialogs and "Nothing to save yet" in saving._snapshot()["status"]


def test_settings_dialog_gets_every_field_it_edits(api):
    got = api.settings_get()
    assert set(got["values"]) == set(bridge._SETTING_KEYS)
    assert got["whisperModels"] and got["logDir"] and got["runtimes"]


def test_settings_wait_for_a_running_conversion(api):
    api._busy = True
    assert api.settings_get() is None
    assert "Finish the current conversion" in api._snapshot()["status"]
    res = api.settings_save({"theme": "dark"})
    assert res["ok"] is False and "your changes are still here" in res["message"]
    assert api._settings.theme == "system"


def test_settings_save_validates_persists_and_reaches_the_engine(api):
    class Engine:
        pass

    api._engine_obj = Engine()
    res = api.settings_save({"whisper_model": "huge", "ocr_device": "tpu", "theme": "dark", "pdf_layout": False,
                             "extract_images": False, "export_style": "ai", "not_a_setting": 1})
    assert res == {"ok": True, "clean": True, "rendered": True, "theme": "dark"}
    saved = Settings.load()
    assert saved.whisper_model == "base" and saved.ocr_device == "auto"  # junk became the default
    assert saved.theme == "dark" and saved.pdf_layout is False
    assert saved.export_style == "single"  # the export bar's, not the dialog's
    assert api._engine_obj.pdf_layout is False and api._engine_obj.figure_markers is False


def test_a_failed_settings_save_keeps_nothing(api, monkeypatch):
    def denied(self):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Settings, "save", denied)
    res = api.settings_save({"theme": "dark"})
    assert res["ok"] is False and "MS-502" in res["message"]
    assert api._settings.theme == "system"


def test_reconvert_is_offered_only_when_results_would_change(api, tmp_path, monkeypatch):
    src = tmp_path / "a.md"
    src.write_text("# A\n\ntext\n", encoding="utf-8")
    _convert_all(api, [src])
    api._window.confirm = False
    api.settings_save({"theme": "dark", "summary_model": "llama3.2", "clean_output": False})
    assert api._window.prompts == []  # nothing a conversion produces changed
    api.settings_save({"pdf_layout": False})
    assert api._window.prompts == ["Re-convert the 1 loaded file(s) with the new settings?"]
    monkeypatch.setattr(api, "_convert_pending", lambda: None)
    api._window.confirm = True
    api.settings_save({"pdf_layout": True})
    assert next(iter(api._items.values())).result is None  # queued to convert again


def test_debug_mode_follows_the_setting(api):
    from markdown_sidekick import debuglog

    api.settings_save({"debug_mode": True})
    try:
        assert debuglog.enabled()
    finally:
        api.settings_save({"debug_mode": False})
    assert not debuglog.enabled()


def test_detect_ai_says_what_it_found(api, monkeypatch):
    from markdown_sidekick import polish

    monkeypatch.setattr(polish, "detect_local_ai",
                        lambda ep: ("ok", ["llama3.2", "llava"], "ollama", "http://localhost:11434"))
    found = api.detect_ai("")
    assert found["status"] == "ok" and found["models"] == ["llama3.2", "llava"]
    assert found["endpoint"] == "http://localhost:11434" and "2 model(s)" in found["message"]
    monkeypatch.setattr(polish, "detect_local_ai", lambda ep: ("unreachable", [], "", ""))
    assert api.detect_ai("http://127.0.0.1:9")["message"] == "Nothing answered at that endpoint."
    assert api.detect_ai("")["message"].startswith("No local AI found")


def test_theme_is_remembered(api):
    assert api.ready()["theme"] == "system"
    assert api.set_option("theme", "dark") == {"ok": True}
    assert api.set_option("theme", "purple") == {"ok": False}
    assert Settings.load().theme == "dark"
    junk = Settings(theme="purple")
    junk.normalize()
    assert junk.theme == "system"


def test_copy_ai_prompt(api, monkeypatch):
    copied = []
    monkeypatch.setattr(bridge, "_set_clipboard", lambda text: copied.append(text) or True)
    assert api.copy_ai_prompt() is True
    assert "mcp" in copied[0].lower() and "AI setup prompt copied" in api._snapshot()["status"]


def test_diagnostic_report(saving, tmp_path, monkeypatch):
    from markdown_sidekick import diagnostics

    made = []
    monkeypatch.setattr(diagnostics, "build_report", lambda dest, include_snapshots=True: made.append(dest) or dest)
    saving._window.picked = (str(tmp_path / "report.zip"),)
    saving.create_report()
    assert made == [tmp_path / "report.zip"] and "Diagnostic report saved" in saving._snapshot()["status"]
    assert saving._window.prompts and not saving.opened  # asked, declined


def test_dialogs_open_in_a_real_folder(saving, tmp_path):
    src = tmp_path / "books" / "a.md"
    src.parent.mkdir()
    src.write_text("# A\n\ntext\n", encoding="utf-8")
    _convert_all(saving, [src])
    saving.save()  # cancelled: nothing picked
    assert saving._window.dialogs[-1][1]["directory"] == str(src.parent)  # beside the source
    saving.browse_folder()
    assert saving._window.dialogs[-1][1]["directory"] == str(Path.home())


def test_the_same_problem_is_one_note_not_one_per_file():
    from markdown_sidekick import errors

    no_ai = errors.report("MS-301", where="test")
    figs = errors.report("MS-401", where="test")
    lines = bridge._note_lines([("No summary for ", "a.pdf", no_ai), ("", "a.pdf", figs),
                                ("No summary for ", "b.pdf", no_ai), ("No summary for ", "c.pdf", no_ai)])
    assert lines == [f"No summary for 3 files: {no_ai.title} (MS-301)", f"a.pdf: {figs.title} (MS-401)"]


def test_a_partial_failure_keeps_the_other_notes(saving, tmp_path, monkeypatch):
    from markdown_sidekick import errors, polish

    sources = []
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text(f"# {name}\n\nIts own sentence about {name}.\n", encoding="utf-8")
        sources.append(tmp_path / name)
    _convert_all(saving, sources)
    saving._settings.ollama_endpoint, saving._settings.summary_model = "http://127.0.0.1:9", "m"
    no_ai = errors.report("MS-301", where="test")
    monkeypatch.setattr(polish, "summarize_markdown", lambda *a, **k: None)
    monkeypatch.setattr(polish, "take_last_failure", lambda: no_ai)
    real_save, calls = bridge.export.save, []

    def first_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError(13, "Access is denied")
        return real_save(*args, **kwargs)

    monkeypatch.setattr(bridge.export, "save", first_fails)
    saving._window.picked = (str(tmp_path / "out"),)
    saving.save()
    status = saving._snapshot()["status"]
    assert "1 couldn't be saved (a.md)" in status and "MS-201" in status
    assert "No summary for 2 files" in status


def test_a_local_ai_that_didnt_answer_is_asked_once_per_save(saving, tmp_path, monkeypatch):
    from markdown_sidekick import errors, polish

    sources = []
    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / name).write_text(f"# {name}\n\nIts own sentence about {name}.\n", encoding="utf-8")
        sources.append(tmp_path / name)
    _convert_all(saving, sources)
    saving._settings.ollama_endpoint, saving._settings.summary_model = "http://127.0.0.1:9", "m"
    asked, no_ai = [], errors.report("MS-301", where="test")
    monkeypatch.setattr(polish, "summarize_markdown", lambda *a, **k: asked.append(1))
    monkeypatch.setattr(polish, "take_last_failure", lambda: no_ai)
    saving._window.picked = (str(tmp_path / "out"),)
    saving.save()
    assert asked == [1]  # one try, not three
    status = saving._snapshot()["status"]
    assert "No summary for 3 files" in status and "Saving" not in status


def test_files_removed_while_waiting_are_not_converted(api, tmp_path, monkeypatch):
    from markdown_sidekick.converter import ConversionResult

    class Engine:
        enable_ocr = False

        def __init__(self):
            self.calls = []

        def convert_file(self, path, on_subprogress=None):
            self.calls.append(Path(path).name)
            return ConversionResult(source=Path(path), markdown=f"# {Path(path).stem}\n\ntext\n")

    engine = api._engine_obj = Engine()
    monkeypatch.setattr(api, "_convert_pending", lambda: None)
    paths = []
    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
        paths.append(tmp_path / name)
    api._add([str(p) for p in paths])
    targets = list(api._items.values())
    api.remove(targets[1].id)  # removed while it waited
    api._busy = True
    api._worker(targets, False)
    assert engine.calls == ["a.md", "c.md"]
    assert targets[1].result is None and not api._busy


def test_the_title_says_when_debug_mode_is_on():
    from markdown_sidekick import debuglog

    assert "DEBUG" not in bridge.window_title()
    debuglog.enable("webui", snapshots=False)
    try:
        assert bridge.window_title().endswith("·  DEBUG MODE")
    finally:
        debuglog.disable()


def test_help_has_the_guide_sections(api):
    titles = [s["title"] for s in api.help()]
    assert any("Error codes" in t for t in titles)
    assert all(s["html"].strip() for s in api.help())
