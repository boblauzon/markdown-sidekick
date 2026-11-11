"""Which window opens: the one a flag names, for that run only, else the one
chosen in Settings (``Settings.window``: the new one unless the user picked
the classic one). Every way of starting the app goes through here: the exe
(app.py), ``python -m markdown_sidekick`` and the ``markdown-sidekick``
command pip installs.

Stdlib only at import time: both windows are imported when one opens.
"""

from __future__ import annotations

import sys

# Command-line flags that pick a window for one run.
WINDOW_FLAGS = {"--new-ui": "new", "--classic": "classic"}


def open_window(flag: str | None = None) -> None:
    """Open a window and run it until it closes. Asked for by name, the new
    window says what this PC lacks and exits (MS-605); as the saved choice
    it falls back to the classic window, whose notice bar says why (MS-606)."""
    from .settings import preferred_window

    notice = None
    if (WINDOW_FLAGS.get(flag or "") or preferred_window()) == "new":
        from . import webui

        lack = None if flag else webui.missing()
        if lack is None:
            webui.run()
            return
        from . import errors

        notice = errors.report("MS-606", exc=lack[1], detail=lack[0], where="start")
    from .ui import run

    run(notice=notice)


def main() -> None:
    """``markdown-sidekick`` / ``python -m markdown_sidekick``: ``--new-ui``
    or ``--classic`` for this run, else the saved choice."""
    open_window(next((a for a in sys.argv[1:] if a in WINDOW_FLAGS), None))
