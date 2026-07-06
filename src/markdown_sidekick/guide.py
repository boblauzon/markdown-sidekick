"""Loads the bundled user guide.

The guide ships as package data (``USERGUIDE.md`` next to this module), which
works identically for a source checkout, a pip install, and a PyInstaller
build — ``importlib.resources`` resolves all three.
"""

from __future__ import annotations

from importlib import resources

KOFI_URL = "https://ko-fi.com/roblauzon"


def load_user_guide() -> str:
    """Return the user guide's Markdown text (a short apology if missing)."""
    try:
        return (
            resources.files("markdown_sidekick")
            .joinpath("USERGUIDE.md")
            .read_text(encoding="utf-8")
        )
    except Exception:
        return (
            "# User Guide\n\nThe bundled guide could not be loaded.\n\n"
            f"Documentation and support: {KOFI_URL}\n"
        )
