"""The classic window, built for real (hidden) and driven through its own
event queue. Conversion is a stand-in engine so each test decides what a
file turns into and when; one test at the end converts a real file.

Native dialogs and message boxes are replaced by recorders: a test says
what the user would answer, and can see what they were asked.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from markdown_sidekick import errors, ui
from markdown_sidekick import settings as settings_store
from markdown_sidekick.converter import ConversionResult
from markdown_sidekick.settings import Settings


class FakeEngine:
    """Stands in for ConversionEngine: instant, scripted, and pausable."""

    def __init__(self) -> None:
        self.enable_ocr = True
        self.enable_audio = True
        self.ocr_device = "auto"
        self.results: dict[str, object] = {}  # file name -> ConversionResult factory
        self.gate = threading.Event()
        self.gate.set()
        self.calls: list[list[str]] = []
        self.crash_after: int | None = None

    def convert_many(self, targets, on_progress=None, on_subprogress=None):
        self.calls.append([p.name for p in targets])
        assert self.gate.wait(20), "the test never released the conversion"
        for index, path in enumerate(targets, start=1):
            if self.crash_after is not None and index > self.crash_after:
                raise RuntimeError("the worker died")
            make = self.results.get(path.name)
            result = make(path) if make else ConversionResult(source=path, markdown=f"# {path.stem}\n\nBody of {path.name}.\n")
            on_progress(index, len(targets), result)


class Boxes:
    """tkinter.messagebox, recorded. Unanswered questions get No."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []
        self.answers: dict[str, bool] = {}

    def __getattr__(self, kind: str):
        def box(title="", message="", **kwargs):
            self.shown.append((kind, message))
            return self.answers.get(kind, False)

        return box


class Dialogs:
    """tkinter.filedialog, recorded. Empty answers are a cancelled dialog."""

    def __init__(self) -> None:
        self.open: tuple[str, ...] = ()
        self.save = ""
        self.folder = ""
        self.asked: list[str] = []

    def askopenfilenames(self, **kwargs):
        self.asked.append("open")
        return self.open

    def asksaveasfilename(self, **kwargs):
        self.asked.append("save")
        return self.save

    def askdirectory(self, **kwargs):
        self.asked.append("folder")
        return self.folder


@pytest.fixture(scope="module")
def _window(tmp_path_factory):
    """One real window for the whole file. Starting Tcl dozens of times in a
    process fails now and then on Windows (it can't read its own script
    files), so the tests share a window and ``app`` resets it."""
    patch = pytest.MonkeyPatch()
    patch.setenv("LOCALAPPDATA", str(tmp_path_factory.mktemp("appdata")))
    patch.delenv("MARKDOWN_SIDEKICK_DEBUG", raising=False)
    patch.setattr(settings_store, "LOAD_INCIDENT", None)
    patch.setattr(errors, "_listeners", [])  # the window adds one; don't keep it
    patch.setattr(errors, "install_crash_hooks", lambda: None)  # process-wide hooks
    patch.setattr(ui.MarkdownSidekickApp, "_start_updates", lambda self: None)  # no first-run question
    window = None
    for attempt in range(3):
        try:
            window = ui.MarkdownSidekickApp()
            break
        except Exception as exc:  # TclError, or tkinterdnd2's RuntimeError around one
            problem = exc
            time.sleep(0.5)
    if window is None:
        patch.undo()
        pytest.skip(f"Tk can't start here: {problem}")
    window.withdraw()
    yield window
    for timer in window.tk.splitlist(window.tk.call("after", "info")):
        window.after_cancel(timer)  # the event pump re-arms itself for ever
    window.destroy()
    patch.undo()


@pytest.fixture
def app(_window, monkeypatch):
    """The shared window, as a fresh start: no files, default settings, a
    stand-in engine, and this test's own dialog answers."""
    import dataclasses

    window = _window
    window.boxes, window.dialogs = Boxes(), Dialogs()
    monkeypatch.setattr(ui, "messagebox", window.boxes)
    monkeypatch.setattr(ui, "filedialog", window.dialogs)
    window.engine = FakeEngine()
    yield window
    if isinstance(window.engine, FakeEngine):
        window.engine.gate.set()
    settle(window)
    if window._dialog is not None and window._dialog.winfo_exists():
        window._dialog.destroy()
    window._dialog = None
    window._last_dialog.clear()
    window.files.clear()
    window._evict_all_caches()
    window.tree.delete(*window.tree.get_children())
    window._clear_preview()
    window.notice.hide()
    settings_store.app_data_dir("settings.json").unlink(missing_ok=True)
    for field in dataclasses.fields(Settings):
        setattr(window.settings, field.name, getattr(Settings(), field.name))
    window._apply_settings()
    window.style_var.set(dict(window._STYLE_LABELS)[window.settings.export_style])
    window.target_var.set(window.settings.ai_target)
    window._sync_target_state()
    window.status_var.set("Ready.")


def pump(window, until, timeout: float = 20.0) -> None:
    """Run the window's event loop until ``until()`` is true."""
    deadline = time.monotonic() + timeout
    while True:
        window.update()
        if until():
            return
        assert time.monotonic() < deadline, f"timed out; status: {window.status_var.get()!r}"
        time.sleep(0.01)


def settle(window) -> None:
    pump(window, lambda: not window._busy and window._events.empty())
    window.update()


def sources(tmp_path: Path, *names: str) -> list[Path]:
    made = []
    for name in names:
        path = tmp_path / "in" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source", encoding="utf-8")
        made.append(path)
    return made


def add(window, *paths: Path) -> None:
    window._add_paths([str(p) for p in paths])


def select(window, path: Path) -> None:
    window.tree.selection_set(str(path))
    window.update()
    window._on_select_file()


def row(window, path: Path) -> str:
    return window.tree.item(str(path), "values")[0]


def preview(window) -> str:
    return window.preview.get("1.0", "end-1c")


def shown(widget) -> bool:
    return widget.winfo_manager() != ""


def failed(code: str, error: str = "It broke."):
    return lambda path: ConversionResult(source=path, error=error, error_code=code, error_ref="7F3A2C")


class TestAddingFiles:
    def test_files_convert_by_themselves_and_the_first_is_shown(self, app, tmp_path):
        a, b = sources(tmp_path, "a.docx", "b.pdf")
        add(app, a, b)
        assert app._busy and row(app, a) == "pending"
        settle(app)
        assert app.engine.calls == [["a.docx", "b.pdf"]]
        assert row(app, a) == "markitdown" and row(app, b) == "markitdown"
        assert app.status_var.get() == "Finished — 2 converted."
        assert app.tree.selection() == (str(a),)
        pump(app, lambda: "Body of a.docx." in preview(app))
        assert "Quality" in app.quality_label.cget("text")

    def test_folders_missing_files_and_repeats_are_not_added(self, app, tmp_path):
        (a,) = sources(tmp_path, "a.docx")
        add(app, a, a, tmp_path / "in", tmp_path / "gone.docx")
        settle(app)
        assert list(app.files) == [a] and app.engine.calls == [["a.docx"]]
        assert app.notice.incident.code == "MS-104" and "Skipped 1 folder(s)." in app.notice._text.cget("text")
        add(app, a)
        settle(app)
        assert app.engine.calls == [["a.docx"]]  # already there: not converted again

    def test_the_open_dialog_adds_what_was_picked(self, app, tmp_path):
        (a,) = sources(tmp_path, "a.docx")
        app.add_files()  # cancelled
        assert not app.files
        app.dialogs.open = (str(a),)
        app.add_files()
        settle(app)
        assert list(app.files) == [a] and app.files[a].ok

    def test_a_drop_of_names_with_spaces(self, app, tmp_path):
        a, b = sources(tmp_path, "annual report.docx", "b.pdf")

        class Drop:
            data = f"{{{a}}} {b}".replace("\\", "/")

        app._on_drop(Drop())
        settle(app)
        assert [p.name for p in app.files] == ["annual report.docx", "b.pdf"]


class TestWhileConverting:
    def test_remove_clear_and_save_are_locked_buttons_and_keys_alike(self, app, tmp_path):
        a, b = sources(tmp_path, "a.docx", "b.pdf")
        app.engine.gate.clear()
        add(app, a, b)
        app.tree.selection_set(str(a))
        for button in (app.save_btn, app.remove_btn, app.clear_btn):
            assert str(button.cget("state")) == "disabled"
        app.remove_selected()  # the Delete key
        app.clear_files()
        app.save_markdown()  # Ctrl+S
        assert list(app.files) == [a, b] and app.dialogs.asked == [] and app.boxes.shown == []
        app.engine.gate.set()
        settle(app)
        for button in (app.save_btn, app.remove_btn, app.clear_btn):
            assert str(button.cget("state")) == "normal"

    def test_files_added_meanwhile_wait_then_convert(self, app, tmp_path):
        a, b = sources(tmp_path, "a.docx", "b.pdf")
        app.engine.gate.clear()
        add(app, a)
        add(app, b)
        assert app.engine.calls == [["a.docx"]] or app.engine.calls == []  # the worker may not have started yet
        app.engine.gate.set()
        settle(app)
        assert app.engine.calls == [["a.docx"], ["b.pdf"]]
        assert app.files[a].ok and app.files[b].ok

    def test_a_result_for_a_file_no_longer_listed_is_dropped(self, app, tmp_path):
        (a,) = sources(tmp_path, "a.docx")
        ghost = tmp_path / "in" / "ghost.docx"
        app._handle_event(("progress", 1, 1, ConversionResult(source=ghost, markdown="x")))
        app._handle_event(("pack", ghost, True, ("x", None, None)))
        assert ghost not in app.files and ghost not in app._clean_cache and not app.tree.exists(str(ghost))

    def test_a_handler_that_raises_does_not_leave_the_window_stuck(self, app, tmp_path, capsys):
        (a,) = sources(tmp_path, "a.docx")
        app.engine.gate.clear()
        add(app, a)
        app._events.put(("progress",))  # malformed: its handler raises
        app.engine.gate.set()
        settle(app)
        assert not app._busy and app.files[a].ok
        assert "MS-901" in app._dialog.title()  # and the user is told, with a code
        assert "ValueError" in capsys.readouterr().err


class TestFailures:
    def test_a_failed_file_says_what_to_do_and_offers_retry(self, app, tmp_path):
        (a,) = sources(tmp_path, "a.docx")
        app.engine.results["a.docx"] = failed("MS-199", "Parser exploded at byte 7.")
        add(app, a)
        settle(app)
        assert row(app, a) == "error MS-199" and app.status_var.get() == "Finished — 0 converted, 1 failed."
        select(app, a)
        text = preview(app)
        spec = errors.CATALOG["MS-199"]
        assert spec.title in text and f"What to do: {spec.next_step}" in text
        assert "Error MS-199 · Ref 7F3A2C" in text and "Parser exploded at byte 7." in text
        assert shown(app.retry_btn) and shown(app.details_btn)
        app.copy_preview()  # an error page is not a document
        assert app._current_export_text == ""

    def test_retry_converts_that_file_again(self, app, tmp_path):
        a, b = sources(tmp_path, "a.docx", "b.pdf")
        app.engine.results["a.docx"] = failed("MS-199")
        add(app, a, b)
        settle(app)
        del app.engine.results["a.docx"]
        select(app, a)
        app.retry_selected()
        settle(app)
        assert app.engine.calls[-1] == ["a.docx"] and app.files[a].ok and row(app, a) == "markitdown"
        assert not shown(app.retry_btn)

    def test_a_one_click_fix_reruns_every_file_it_helps(self, app, tmp_path):
        a, b, c = sources(tmp_path, "scan1.pdf", "scan2.pdf", "fine.docx")
        action = errors.CATALOG["MS-110"].action
        app.engine.results["scan1.pdf"] = app.engine.results["scan2.pdf"] = failed("MS-110")
        app.settings.enable_ocr = False
        add(app, a, b, c)
        settle(app)
        select(app, a)
        assert shown(app.fix_btn) and app.fix_btn.cget("text") == errors.ACTIONS[action]
        app.engine.results.clear()
        app.fix_btn.invoke()
        settle(app)
        assert sorted(app.engine.calls[-1]) == ["scan1.pdf", "scan2.pdf"]  # not the file that was fine
        assert app.files[a].ok and app.files[b].ok
        assert Settings.load().enable_ocr is True  # the fix is remembered

    def test_a_crashed_batch_fails_the_files_it_never_reached(self, app, tmp_path):
        a, b, c = sources(tmp_path, "a.docx", "b.pdf", "c.txt")
        app.engine.crash_after = 1
        add(app, a, b, c)
        settle(app)
        assert app.files[a].ok
        assert [app.files[p].error_code for p in (b, c)] == ["MS-902", "MS-902"]
        assert row(app, b) == "error MS-902"
        assert len(app.engine.calls) == 1  # never handed straight back to the same crash
        assert not app._busy and "MS-902" in app._dialog.title()

    def test_a_warning_marks_the_row_and_follows_the_selection(self, app, tmp_path):
        a, b = sources(tmp_path, "a.pdf", "b.docx")
        warning = errors.Incident(errors.CATALOG["MS-120"], "AB12CD")
        app.engine.results["a.pdf"] = lambda path: ConversionResult(
            source=path, markdown="# A\n\nText.\n", engine="markitdown", warnings=[warning]
        )
        add(app, a, b)
        settle(app)
        assert row(app, a) == "markitdown ⚠"
        select(app, a)
        assert app.notice.incident is warning and app.notice.owner == a
        select(app, b)
        assert app.notice.incident is None  # the other file's warning doesn't linger

    def test_the_same_error_twice_is_one_dialog(self, app):
        first = errors.Incident(errors.CATALOG["MS-299"], "AAAAAA")
        app._show_error(first)
        dialog = app._dialog
        dialog.destroy()
        app._show_error(errors.Incident(errors.CATALOG["MS-299"], "BBBBBB"))
        assert app._dialog is dialog  # no second box within 15 s
        assert "MS-299" in app.status_var.get() and "BBBBBB" in app.status_var.get()

    def test_every_one_click_fix_in_the_catalog_has_a_handler(self, app):
        handlers = app._error_handlers()
        assert set(handlers) == set(errors.ACTIONS)
        used = {spec.action for spec in errors.CATALOG.values() if spec.action}
        assert used <= set(handlers)

    def test_the_webview2_notice_opens_microsofts_page(self, app, monkeypatch):
        opened = []
        monkeypatch.setattr(ui.webbrowser, "open", opened.append)
        app._show_notice(errors.report("MS-606", detail="Microsoft Edge WebView2 Runtime", where="test"))
        bar = app.notice
        assert bar.incident.code == "MS-606" and bar._action.cget("text") == "Get WebView2…"
        bar._action.invoke()
        assert opened == [errors.WEBVIEW2_URL]

    def test_the_fallback_notice_leaves_an_unreadable_settings_notice_in_place(self, app, monkeypatch):
        fallback = errors.report("MS-606", detail="Microsoft Edge WebView2 Runtime", where="test")
        app._show_startup_notice(fallback)
        assert app.notice.incident.code == "MS-606"  # the usual case: the bar
        app.notice.hide()
        unreadable = errors.report("MS-501", detail="JSONDecodeError", where="test")
        monkeypatch.setattr(settings_store, "LOAD_INCIDENT", unreadable)
        app._show_notice(unreadable)
        app._show_startup_notice(fallback)
        assert app.notice.incident.code == "MS-501"  # their settings were reset: that stays
        assert "MS-606" in app.status_var.get()


class TestSettingsDialog:
    def _dialog(self, app, monkeypatch):
        from markdown_sidekick import polish

        monkeypatch.setattr(polish, "detect_local_ai", lambda ep: ("unreachable", [], "", ""))
        before = set(app.winfo_children())
        app.open_settings("Output")
        app.update()
        [dlg] = [w for w in app.winfo_children() if w not in before]
        return dlg

    @staticmethod
    def _widgets(widget):
        for child in widget.winfo_children():
            yield child
            yield from TestSettingsDialog._widgets(child)

    def _find(self, dlg, text):
        return next(w for w in self._widgets(dlg) if "text" in w.keys() and w.cget("text") == text)

    def test_the_window_choice_is_saved_for_the_next_start(self, app, monkeypatch):
        from markdown_sidekick.settings import preferred_window

        dlg = self._dialog(app, monkeypatch)
        box = self._find(dlg, "Open the new window when Markdown Sidekick starts")
        assert app.tk.getvar(box.cget("variable")) in (1, True, "1")  # new by default
        box.invoke()
        self._find(dlg, "Save").invoke()
        app.update()
        assert not dlg.winfo_exists()
        assert app.settings.window == "classic" and Settings.load().window == "classic"
        assert preferred_window() == "classic"


class TestRemovingAndClearing:
    def test_remove_takes_the_file_and_its_preview(self, app, tmp_path):
        a, b = sources(tmp_path, "a.docx", "b.pdf")
        add(app, a, b)
        settle(app)
        select(app, a)
        app.remove_selected()
        assert list(app.files) == [b] and not app.tree.exists(str(a))
        assert preview(app) == "" and app._current_export_text == ""

    def test_clear_asks_before_discarding_converted_results(self, app, tmp_path):
        a, b = sources(tmp_path, "a.docx", "b.pdf")
        add(app, a, b)
        settle(app)
        app.clear_files()  # answered No
        assert len(app.files) == 2 and "Discard 2 converted result(s)?" in app.boxes.shown[-1][1]
        app.boxes.answers["askyesno"] = True
        app.clear_files()
        assert not app.files and app.tree.get_children() == () and app.status_var.get() == "Ready."

    def test_clear_does_not_ask_about_failures(self, app, tmp_path):
        (a,) = sources(tmp_path, "a.docx")
        app.engine.results["a.docx"] = failed("MS-199")
        add(app, a)
        settle(app)
        app.clear_files()
        assert not app.files and app.boxes.shown == []


class TestPreviewAndCopy:
    def test_copy_takes_the_whole_document_without_figure_markers(self, app, tmp_path, monkeypatch):
        (a,) = sources(tmp_path, "a.pdf")
        body = "# Title\n\nBefore.\n\n<!-- figure 1.1 -->\n\nAfter.\n"
        app.engine.results["a.pdf"] = lambda path: ConversionResult(source=path, markdown=body, engine="pdflayout")
        copied = []
        monkeypatch.setattr(app, "_to_clipboard", lambda text: copied.append(text) or True)
        add(app, a)
        settle(app)
        select(app, a)
        app.copy_preview()
        assert "Before." in copied[0] and "After." in copied[0] and "figure 1.1" not in copied[0]
        assert app.status_var.get() == "Markdown copied to clipboard."

    def test_a_huge_document_is_cut_in_the_preview_only(self, app, tmp_path, monkeypatch):
        (a,) = sources(tmp_path, "big.txt")
        monkeypatch.setattr(ui, "PREVIEW_MAX_CHARS", 2000)
        body = "".join(f"Sentence number {n} of the long document.\n\n" for n in range(400))
        app.engine.results["big.txt"] = lambda path: ConversionResult(source=path, markdown=body)
        app.clean_var.set(False)
        add(app, a)
        settle(app)
        select(app, a)
        assert len(preview(app)) < 2300 and "Copy and Save always use the full document" in preview(app)
        assert app._current_export_text == body

    def test_the_clean_switch_chooses_what_is_shown_and_saved(self, app, tmp_path):
        (a,) = sources(tmp_path, "a.docx")
        body = "# Title\n\nA line with a ligature: ﬁne.\n"
        app.engine.results["a.docx"] = lambda path: ConversionResult(source=path, markdown=body)
        add(app, a)
        settle(app)
        select(app, a)
        assert "fine." in app._current_export_text  # cleaned
        app.clean_var.set(False)
        app._on_clean_toggle()
        assert app._current_export_text == body

    def test_raw_view_shows_the_markdown_itself(self, app, tmp_path):
        (a,) = sources(tmp_path, "a.docx")
        add(app, a)
        settle(app)
        select(app, a)
        assert "# a" not in preview(app)  # rendered: the hash is gone
        app._fix_raw_preview()
        assert "# a" in preview(app) and app.rendered_var.get() is False


class TestSaving:
    def test_nothing_to_save_says_so(self, app):
        app.save_markdown()
        assert app.boxes.shown[-1][0] == "showinfo" and app.dialogs.asked == []

    def test_one_file_gets_a_save_dialog_and_front_matter(self, app, tmp_path):
        (a,) = sources(tmp_path, "a.docx")
        add(app, a)
        settle(app)
        dest = tmp_path / "out" / "chosen name.md"
        dest.parent.mkdir()
        app.dialogs.save = str(dest)
        app.save_markdown()
        saved = dest.read_text(encoding="utf-8")
        assert app.dialogs.asked == ["save"]
        assert saved.startswith("---\n") and "source: a.docx" in saved and "Body of a.docx." in saved
        assert app.status_var.get() == "Saved chosen name.md."

    def test_a_cancelled_dialog_writes_nothing(self, app, tmp_path):
        (a,) = sources(tmp_path, "a.docx")
        add(app, a)
        settle(app)
        app.save_markdown()
        assert app.dialogs.asked == ["save"] and "Saved" not in app.status_var.get()
        assert not list(tmp_path.rglob("*.md"))

    def test_several_files_go_to_a_folder_under_their_own_names(self, app, tmp_path):
        a, b = sources(tmp_path, "a.docx", "b.pdf")
        other = tmp_path / "elsewhere" / "a.docx"  # the same name from another folder
        other.parent.mkdir()
        other.write_text("source", encoding="utf-8")
        add(app, a, b, other)
        settle(app)
        out = tmp_path / "out"
        out.mkdir()
        app.dialogs.folder = str(out)
        app.save_markdown()
        assert app.dialogs.asked == ["folder"]
        assert sorted(p.name for p in out.iterdir()) == ["a-2.md", "a.md", "b.md"]  # neither a.docx overwrote the other
        assert "Saved 3 file(s)" in app.boxes.shown[-1][1] and "Open the folder?" in app.boxes.shown[-1][1]

    def test_a_book_style_makes_a_folder_even_for_one_file(self, app, tmp_path):
        (a,) = sources(tmp_path, "a.docx")
        app.engine.results["a.docx"] = lambda path: ConversionResult(
            source=path, markdown="# Chapter 1: One\n\nFirst.\n\n# Chapter 2: Two\n\nSecond.\n\n# Chapter 3: Three\n\nThird.\n"
        )
        add(app, a)
        settle(app)
        app.style_var.set(dict(app._STYLE_LABELS)["chapters"])
        out = tmp_path / "out"
        out.mkdir()
        app.dialogs.folder = str(out)
        app.save_markdown()
        assert app.dialogs.asked == ["folder"]
        assert (out / "a" / "index.md").is_file() and len(list((out / "a").glob("0*.md"))) == 3

    def test_a_save_that_fails_is_an_error_never_saved(self, app, tmp_path, monkeypatch):
        (a,) = sources(tmp_path, "a.docx")
        add(app, a)
        settle(app)

        def refuse(*args, **kwargs):
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(ui.md_export, "save", refuse)
        app.dialogs.save = str(tmp_path / "locked.md")
        app.save_markdown()
        assert "Saved" not in app.status_var.get()
        assert app._dialog is not None and "MS-2" in app._dialog.title()

    def test_one_bad_file_does_not_lose_the_rest_of_a_folder_save(self, app, tmp_path, monkeypatch):
        a, b = sources(tmp_path, "a.docx", "b.pdf")
        add(app, a, b)
        settle(app)
        real = ui.md_export.save

        def picky(text, dest, **kwargs):
            if Path(dest).stem == "a":
                raise PermissionError(13, "Access is denied")
            return real(text, dest, **kwargs)

        monkeypatch.setattr(ui.md_export, "save", picky)
        out = tmp_path / "out"
        out.mkdir()
        app.dialogs.folder = str(out)
        app.save_markdown()
        assert [p.name for p in out.iterdir()] == ["b.md"]
        assert "MS-2" in app._dialog.title() and "Saved" not in app.status_var.get()


class TestExportBar:
    def test_the_choice_is_remembered_and_the_target_follows_the_style(self, app):
        assert str(app.target_box.cget("state")) == "disabled"  # only AI-sized sections have a target
        app.style_var.set(dict(app._STYLE_LABELS)["ai"])
        app.target_var.set("ChatGPT")
        app._on_export_style_change()
        assert str(app.target_box.cget("state")) == "readonly"
        stored = json.loads(settings_store.app_data_dir("settings.json").read_text(encoding="utf-8"))
        assert stored["export_style"] == "ai" and stored["ai_target"] == "ChatGPT"

    def test_a_settings_file_that_cannot_be_written_is_a_notice(self, app, monkeypatch):
        def refuse(self):
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(Settings, "save", refuse)
        app._on_export_style_change()
        assert app.notice.incident.code == "MS-502"


def test_the_title_says_when_debug_mode_is_on(app, monkeypatch):
    assert "DEBUG MODE" not in app.title()
    monkeypatch.setattr(ui.debuglog, "enabled", lambda: True)
    app._update_title()
    assert app.title().endswith("DEBUG MODE")


def test_a_real_file_end_to_end(app, tmp_path):
    """The real engine, the worker thread, cleanup and the preview together."""
    from markdown_sidekick.converter import ConversionEngine

    source = tmp_path / "notes.md"
    source.write_text("# Field notes\n\nThe heron stood in the shallows for an hour.\n", encoding="utf-8")
    app.engine = ConversionEngine(enable_ocr=False, enable_audio=False)
    add(app, source)
    pump(app, lambda: not app._busy and app._events.empty(), timeout=120)
    assert app.files[source].ok, app.files[source].error
    pump(app, lambda: "heron" in preview(app))
    assert "Field notes" in preview(app) and "Quality" in app.quality_label.cget("text")
