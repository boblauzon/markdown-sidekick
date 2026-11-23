"""Shared test setup."""

from __future__ import annotations

import gc
import os
import weakref

import pytest


@pytest.fixture(scope="session")
def tk_root():
    """One hidden Tk window for the tests that draw real widgets."""
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display (a headless runner)
        pytest.skip(f"Tk can't start here: {exc}")
    _tk_alive.discard(root)  # lives all session, destroyed on the main thread
    root.withdraw()
    yield root
    root.destroy()


_tk_alive: "weakref.WeakSet" = weakref.WeakSet()  # every Tk but the session root


@pytest.fixture(autouse=True, scope="session")
def _note_tk_windows():
    """Track every Tk interpreter a test or fixture creates (the classic
    window's TkinterDnD.Tk subclasses tkinter.Tk, so this sees it too)."""
    import tkinter as tk

    original = tk.Tk.__init__

    def init(self, *args, **kwargs):
        _tk_alive.add(self)
        original(self, *args, **kwargs)

    tk.Tk.__init__ = init
    yield
    tk.Tk.__init__ = original


@pytest.fixture(autouse=True)
def _collect_tk_on_the_main_thread():
    """A test that built a Tk window leaves it in reference cycles. Collect
    them here, on the main thread, until every such Tk is gone (one kept by
    a module fixture is freed later, so this keeps checking): left for
    later, the collection can run on a worker thread of the new window's
    tests, and Tcl kills the process (exit 0x80000003, no message) when an
    interpreter is freed by a thread other than its own. When that happened
    depended only on how many tests ran first."""
    yield
    if len(_tk_alive):
        gc.collect()


@pytest.fixture(autouse=True, scope="session")
def _isolated_logs(tmp_path_factory):
    """Keep the always-on error log (and any debug session) out of the real
    %LOCALAPPDATA% — tests convert deliberately broken files."""
    from markdown_sidekick import debuglog

    previous = os.environ.get(debuglog.ENV_LOG_DIR)
    os.environ[debuglog.ENV_LOG_DIR] = str(tmp_path_factory.mktemp("logs"))
    yield
    debuglog.disable()
    if previous is None:
        os.environ.pop(debuglog.ENV_LOG_DIR, None)
    else:
        os.environ[debuglog.ENV_LOG_DIR] = previous
