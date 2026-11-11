"""Which window opens: the one a flag names, for that run only, else the one
chosen in Settings (``Settings.window``: the new one unless the user picked
the classic one). Every way of starting the app goes through here: the exe
(app.py), ``python -m markdown_sidekick`` and the ``markdown-sidekick``
command pip installs.

Stdlib only at import time: both windows are imported when one opens.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Command-line flags that pick a window for one run.
WINDOW_FLAGS = {"--new-ui": "new", "--classic": "classic"}
# A notice handed to a classic window started in a new process (see
# _classic_in_new_process): {"code", "ref", "detail"} of an already-logged
# incident.
NOTICE_ENV = "MARKDOWN_SIDEKICK_NOTICE"


def open_window(flag: str | None = None) -> None:
    """Open a window and run it until it closes. Asked for by name, the new
    window says what this PC lacks and exits (MS-605); as the saved choice
    it falls back to the classic window, whose notice bar says why: MS-606
    when it can't run here, MS-607 when WebView2 is there but didn't start."""
    from .settings import preferred_window

    notice = _passed_notice()
    if (WINDOW_FLAGS.get(flag or "") or preferred_window()) == "new":
        from . import webui

        lack = None if flag else webui.missing()
        if lack is None:
            failed = webui.run(fallback=flag is None)
            if failed is not None:
                _classic_in_new_process(failed)
            return
        from . import errors

        notice = errors.report("MS-606", exc=lack[1], detail=lack[0], where="start")
    from .ui import run

    run(notice=notice)


def _classic_in_new_process(incident) -> None:
    """The classic window, with ``incident`` in its notice bar, in a process
    of its own: once a WebView2 window has run, this process's main thread
    is a multithreaded COM apartment, where the classic window's drag and
    drop (OLE) can't start."""
    import subprocess

    env = dict(os.environ)
    env[NOTICE_ENV] = json.dumps({"code": incident.code, "ref": incident.ref, "detail": incident.detail})
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--classic"]
    else:  # a source checkout, pip's command or python -m: run the package that is running now
        here = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        cmd = [sys.executable, "-m", "markdown_sidekick", "--classic"]
    subprocess.Popen(cmd, env=env, close_fds=True)


def _passed_notice():
    """The incident a failed new window handed this process, if any (taken
    out of the environment, so nothing this process starts inherits it)."""
    raw = os.environ.pop(NOTICE_ENV, None)
    if not raw:
        return None
    from . import errors

    try:
        data = json.loads(raw)
        return errors.Incident(errors.CATALOG[data["code"]], ref=str(data.get("ref", "")),
                               detail=str(data.get("detail", "")))
    except (ValueError, KeyError, TypeError):
        return None


def main() -> None:
    """``markdown-sidekick`` / ``python -m markdown_sidekick``: ``--new-ui``
    or ``--classic`` for this run, else the saved choice."""
    open_window(next((a for a in sys.argv[1:] if a in WINDOW_FLAGS), None))
