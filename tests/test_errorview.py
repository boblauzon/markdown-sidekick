"""How the classic window shows an error: the dialog and the notice bar,
built as real Tk widgets. Every error must reach the user with what happened,
what to do, the code to quote, and its one-click fix when the window has one.
"""

from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk

import pytest

from markdown_sidekick import errorview
from markdown_sidekick.errors import ACTIONS, CATALOG, Incident
from markdown_sidekick.errorview import NoticeBar, show_error_dialog


def incident_with(action) -> Incident:
    """A catalogued error whose one-click fix satisfies ``action`` (a predicate)."""
    spec = next(s for s in CATALOG.values() if action(s.action))
    return Incident(spec, "7F3A2C", detail="Boom: it broke", where="unit test")


def widgets(root: tk.Misc):
    for child in root.winfo_children():
        yield child
        yield from widgets(child)


def labels(root: tk.Misc) -> list[str]:
    return [str(w.cget("text")) for w in widgets(root) if isinstance(w, ttk.Label)]


def buttons(root: tk.Misc) -> dict[str, ttk.Button]:
    return {str(w.cget("text")): w for w in widgets(root) if isinstance(w, ttk.Button)}


def press(window: tk.Misc, key: str) -> None:
    """Run ``window``'s binding for ``key``. The test window is hidden, and a
    hidden window never has the keyboard focus a generated key press needs, so
    this calls what the key is bound to (taking focus would also steal it
    from whoever is running the tests)."""
    command = re.search(r"\[(\S+)", window.bind(key)).group(1)
    fields = ["0"] + ["??"] * 18  # %# %b %f %h %k %s %t %w %x %y %A %E %K %N %W …
    fields[14] = str(window)
    window.tk.call(command, *fields)


def packed(widget: tk.Misc) -> bool:
    return widget.winfo_manager() == "pack"


@pytest.fixture
def dialog(tk_root):
    made = []

    def show(incident, handlers, **kwargs) -> tk.Toplevel:
        made.append(show_error_dialog(tk_root, incident, handlers, bg="#ffffff", **kwargs))
        return made[-1]

    yield show
    for dlg in made:
        if dlg.winfo_exists():
            dlg.destroy()


class TestDialog:
    def test_it_says_what_happened_what_to_do_and_what_to_quote(self, dialog):
        incident = incident_with(lambda a: a == "")
        dlg = dialog(incident, {}, extra="report.pdf")
        text = labels(dlg)
        assert incident.code in dlg.title()
        assert incident.title in text and "report.pdf" in text
        assert f"What to do: {incident.next_step}" in text
        assert any(incident.reference() in line for line in text)

    def test_the_fix_is_the_default_button_and_closes_the_dialog(self, dialog):
        incident = incident_with(lambda a: a not in ("", "open_logs"))
        ran = []
        dlg = dialog(incident, {incident.action: lambda: ran.append("fix"), "open_logs": lambda: ran.append("logs")})
        found = buttons(dlg)
        assert ACTIONS["open_logs"] not in found  # one next step, not two
        press(dlg, "<Return>")
        assert ran == ["fix"] and not dlg.winfo_exists()

    def test_without_a_handler_the_fix_button_is_not_offered(self, dialog):
        """The window can't do it, so it mustn't show a button that does nothing."""
        incident = incident_with(lambda a: a not in ("", "open_logs"))
        ran = []
        dlg = dialog(incident, {"open_logs": lambda: ran.append("logs")})
        found = buttons(dlg)
        assert ACTIONS[incident.action] not in found
        found[ACTIONS["open_logs"]].invoke()
        assert ran == ["logs"] and not dlg.winfo_exists()

    def test_enter_closes_when_there_is_no_fix(self, dialog):
        dlg = dialog(incident_with(lambda a: a == ""), {})
        assert set(buttons(dlg)) == {"Close", "Copy details"}
        press(dlg, "<Return>")
        assert not dlg.winfo_exists()

    def test_escape_closes_without_running_the_fix(self, dialog):
        incident = incident_with(lambda a: a not in ("", "open_logs"))
        ran = []
        dlg = dialog(incident, {incident.action: lambda: ran.append("fix")})
        press(dlg, "<Escape>")
        assert ran == [] and not dlg.winfo_exists()

    def test_copy_details_puts_the_support_text_on_the_clipboard(self, dialog, monkeypatch):
        copied = []
        monkeypatch.setattr(errorview, "_copy", lambda widget, text: copied.append(text) or True)
        incident = incident_with(lambda a: a == "")
        dlg = dialog(incident, {})
        button = buttons(dlg)["Copy details"]
        button.invoke()
        assert copied == [incident.details_text()] and "Boom: it broke" in copied[0]
        assert button.cget("text") == "Copied ✓" and dlg.winfo_exists()

    def test_a_busy_clipboard_is_said_not_hidden(self, dialog, monkeypatch):
        monkeypatch.setattr(errorview, "_copy", lambda widget, text: False)
        dlg = dialog(incident_with(lambda a: a == ""), {})
        button = buttons(dlg)["Copy details"]
        button.invoke()
        assert button.cget("text") == "Clipboard busy"


def test_copy_reports_a_clipboard_it_cannot_use():
    class Busy:
        def clipboard_clear(self):
            raise tk.TclError("clipboard held by another program")

    class Free:
        def __init__(self):
            self.text = None

        def clipboard_clear(self):
            self.text = ""

        def clipboard_append(self, text):
            self.text += text

    free = Free()
    assert errorview._copy(Busy(), "x") is False
    assert errorview._copy(free, "details") is True and free.text == "details"


@pytest.fixture
def bar(tk_root):
    handlers: dict = {}
    notice = NoticeBar(tk_root, lambda: handlers)
    notice.place_in(fill="x")
    notice.handlers = handlers
    yield notice
    notice.destroy()


class TestNoticeBar:
    def test_it_stays_hidden_until_there_is_something_to_say(self, bar):
        assert not packed(bar) and bar.incident is None

    def test_one_line_with_the_next_step_and_the_code(self, bar):
        incident = incident_with(lambda a: a == "")
        bar.show(incident, extra="report.pdf:")
        assert packed(bar)
        assert bar._text.cget("text") == f"report.pdf: {incident.title} {incident.next_step}  ({incident.code})"
        assert not packed(bar._action)

    def test_the_fix_button_runs_and_hides_the_bar(self, bar):
        incident = incident_with(lambda a: a not in ("", "open_logs"))
        ran = []
        bar.handlers[incident.action] = lambda: ran.append("fix")
        bar.show(incident)
        assert packed(bar._action) and bar._action.cget("text") == ACTIONS[incident.action]
        bar._action.invoke()
        assert ran == ["fix"] and not packed(bar) and bar.incident is None

    def test_the_fix_button_goes_when_the_next_notice_has_none(self, bar):
        with_fix = incident_with(lambda a: a not in ("", "open_logs"))
        bar.handlers[with_fix.action] = lambda: None
        bar.show(with_fix)
        bar.show(incident_with(lambda a: a == ""))
        assert packed(bar) and not packed(bar._action)

    def test_a_notice_about_one_file_is_not_hidden_for_another(self, bar):
        bar.show(incident_with(lambda a: a == ""), owner="a.pdf")
        bar.hide(owner="b.pdf")
        assert packed(bar) and bar.owner == "a.pdf"
        bar.hide(owner="a.pdf")
        assert not packed(bar) and bar.owner is None

    def test_the_close_button_hides_whatever_it_is_about(self, bar):
        bar.show(incident_with(lambda a: a == ""), owner="a.pdf")
        buttons(bar)["✕"].invoke()
        assert not packed(bar)

    def test_copy_details_resets_for_the_next_notice(self, bar, monkeypatch):
        copied = []
        monkeypatch.setattr(errorview, "_copy", lambda widget, text: copied.append(text) or True)
        first = incident_with(lambda a: a == "")
        bar.show(first)
        bar._copy.invoke()
        assert copied == [first.details_text()] and bar._copy.cget("text") == "Copied ✓"
        bar.show(first)
        assert bar._copy.cget("text") == "Copy details"

    def test_copy_details_with_nothing_shown_does_nothing(self, bar, monkeypatch):
        copied = []
        monkeypatch.setattr(errorview, "_copy", lambda widget, text: copied.append(text) or True)
        bar._copy.invoke()
        assert copied == []
