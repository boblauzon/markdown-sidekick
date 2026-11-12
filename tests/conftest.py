"""Shared test setup."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def tk_root():
    """One hidden Tk window for the tests that draw real widgets."""
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display (a headless runner)
        pytest.skip(f"Tk can't start here: {exc}")
    root.withdraw()
    yield root
    root.destroy()


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
