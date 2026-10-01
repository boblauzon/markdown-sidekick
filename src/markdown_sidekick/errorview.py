"""How the GUI shows an error: a dialog when it blocks what the user was
doing, a slim notice bar when it doesn't.

Both follow the same pattern (errors.py): what happened in plain words →
what to do next → a button that does it, when one exists → the code and
reference to quote, with *Copy details* for a support request. Widget
styles ("Panel.*", "Notice.*", "Accent.TButton") come from ui.py's theme.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from .errors import ACTIONS, Incident

Handlers = dict[str, Callable[[], None]]

_ICONS = {"error": "⚠", "warning": "⚠", "info": "ℹ"}


def _copy(widget: tk.Misc, text: str) -> bool:
    try:
        widget.clipboard_clear()
        widget.clipboard_append(text)
        return True
    except tk.TclError:  # clipboard held by another program
        return False


def show_error_dialog(
    parent: tk.Misc,
    incident: Incident,
    handlers: Handlers,
    *,
    bg: str,
    extra: str = "",
) -> tk.Toplevel:
    """A modal explanation with the next step as its default button."""
    dlg = tk.Toplevel(parent)
    dlg.title(f"Markdown Sidekick — {incident.code}")
    dlg.configure(bg=bg)
    dlg.transient(parent)
    dlg.resizable(False, False)

    body = ttk.Frame(dlg, style="Panel.TFrame", padding=(20, 18, 20, 14))
    body.pack(fill="both", expand=True)
    ttk.Label(
        body, text=_ICONS.get(incident.severity, "⚠"), style="Panel.TLabel", font=("Segoe UI", 22)
    ).grid(row=0, column=0, rowspan=4, sticky="n", padx=(0, 14))
    ttk.Label(
        body,
        text=incident.title,
        style="Panel.TLabel",
        font=("Segoe UI", 11, "bold"),
        wraplength=440,
        justify="left",
    ).grid(row=0, column=1, sticky="w")
    row = 1
    if extra:
        ttk.Label(body, text=extra, style="Panel.TLabel", wraplength=440, justify="left").grid(
            row=row, column=1, sticky="w", pady=(6, 0)
        )
        row += 1
    ttk.Label(
        body,
        text=f"What to do: {incident.next_step}",
        style="Panel.TLabel",
        wraplength=440,
        justify="left",
    ).grid(row=row, column=1, sticky="w", pady=(8, 0))
    ttk.Label(
        body,
        text=f"{incident.reference()} · details saved to the error log",
        style="PanelMuted.TLabel",
    ).grid(row=row + 1, column=1, sticky="w", pady=(12, 0))

    buttons = ttk.Frame(body, style="Panel.TFrame")
    buttons.grid(row=row + 2, column=0, columnspan=2, sticky="e", pady=(16, 0))

    def run(handler: Callable[[], None]) -> None:
        dlg.destroy()
        handler()

    close = ttk.Button(buttons, text="Close", command=dlg.destroy)
    close.pack(side="right", padx=(8, 0))
    copy_btn = ttk.Button(buttons, text="Copy details")

    def copy_details() -> None:
        copy_btn.configure(text="Copied ✓" if _copy(dlg, incident.details_text()) else "Clipboard busy")

    copy_btn.configure(command=copy_details)
    copy_btn.pack(side="right", padx=(8, 0))
    default = close
    if incident.action in handlers and incident.action in ACTIONS:
        primary = ttk.Button(
            buttons,
            text=ACTIONS[incident.action],
            style="Accent.TButton",
            command=lambda: run(handlers[incident.action]),
        )
        primary.pack(side="right")
        default = primary
    elif incident.action != "open_logs" and "open_logs" in handlers:
        ttk.Button(
            buttons, text=ACTIONS["open_logs"], command=lambda: run(handlers["open_logs"])
        ).pack(side="right")

    dlg.bind("<Escape>", lambda _e: dlg.destroy())
    dlg.bind("<Return>", lambda _e: default.invoke())
    dlg.update_idletasks()
    # Centre over the parent window.
    x = parent.winfo_rootx() + (parent.winfo_width() - dlg.winfo_width()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dlg.winfo_height()) // 3
    dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
    default.focus_set()
    dlg.grab_set()
    return dlg


class NoticeBar(ttk.Frame):
    """A dismissible one-line notice with its fix button — for problems the
    user should know about that don't stop them (a fallback reader was used,
    the AI summary was skipped, settings were reset)."""

    def __init__(self, parent: tk.Misc, handlers: Callable[[], Handlers]) -> None:
        super().__init__(parent, style="Notice.TFrame", padding=(10, 6))
        self._handlers = handlers
        self.incident: Incident | None = None
        self.owner: object = None  # what the notice is about (a file path), or None
        self._icon = ttk.Label(self, style="Notice.TLabel", font=("Segoe UI", 11))
        self._icon.pack(side="left", padx=(0, 8))
        ttk.Button(self, text="✕", width=3, style="Notice.TButton", command=self.hide).pack(
            side="right"
        )
        self._copy = ttk.Button(self, text="Copy details", style="Notice.TButton", command=self._copy_details)
        self._copy.pack(side="right", padx=(6, 0))
        self._action = ttk.Button(self, style="Notice.TButton")
        self._text = ttk.Label(self, style="Notice.TLabel", justify="left", wraplength=640)
        self._text.pack(side="left", fill="x", expand=True)
        self._pack_args: dict = {}

    def place_in(self, **pack_args) -> None:
        """Remember where the bar goes; it stays hidden until shown."""
        self._pack_args = pack_args

    def show(self, incident: Incident, *, owner: object = None, extra: str = "") -> None:
        self.incident, self.owner = incident, owner
        self._icon.configure(text=_ICONS.get(incident.severity, "⚠"))
        lead = f"{extra} " if extra else ""
        self._text.configure(text=f"{lead}{incident.title} {incident.next_step}  ({incident.code})")
        self._copy.configure(text="Copy details")
        handlers = self._handlers()
        if incident.action in handlers and incident.action in ACTIONS:
            self._action.configure(
                text=ACTIONS[incident.action],
                command=lambda: self._run(handlers[incident.action]),
            )
            self._action.pack(side="right", padx=(6, 0), after=self._copy)
        else:
            self._action.pack_forget()
        if not self.winfo_ismapped():
            self.pack(**self._pack_args)

    def hide(self, owner: object = None) -> None:
        """Hide the bar (only if it's about ``owner``, when one is given)."""
        if owner is not None and self.owner != owner:
            return
        self.incident, self.owner = None, None
        self.pack_forget()

    def _run(self, handler: Callable[[], None]) -> None:
        self.hide()
        handler()

    def _copy_details(self) -> None:
        if self.incident is not None:
            ok = _copy(self, self.incident.details_text())
            self._copy.configure(text="Copied ✓" if ok else "Clipboard busy")
