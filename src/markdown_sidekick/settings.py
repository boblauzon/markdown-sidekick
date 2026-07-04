"""Persisted user settings for Markdown Sidekick.

Stored as JSON under %LOCALAPPDATA%\\MarkdownSidekick\\settings.json (or the
home directory as a fallback). Loading is tolerant: unknown keys are ignored and
a missing/corrupt file yields defaults, so the app always starts.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

WHISPER_MODELS = ("tiny", "base", "small", "medium")


def app_data_dir(*parts: str) -> Path:
    """Per-user app data dir (%LOCALAPPDATA%\\MarkdownSidekick), plus subpaths.

    The single source of truth for where the app stores settings and models.
    """
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base).joinpath("MarkdownSidekick", *parts)


def _config_path() -> Path:
    return app_data_dir("settings.json")


@dataclass
class Settings:
    """All user-configurable options, with sensible defaults."""

    enable_ocr: bool = True
    enable_audio: bool = True
    whisper_model: str = "base"
    mineru_endpoint: str = ""  # blank = disabled
    default_output_dir: str = ""  # blank = ask each time
    clean_output: bool = True
    rendered_preview: bool = True

    # -- persistence ---------------------------------------------------------
    @classmethod
    def load(cls) -> "Settings":
        """Load settings, tolerating a missing, corrupt, or wrong-typed file."""
        path = _config_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            settings = cls(**{k: v for k, v in data.items() if k in known})
            settings.normalize()
            return settings
        except Exception:
            return cls()

    def save(self) -> None:
        self.normalize()
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def normalize(self) -> None:
        """Coerce every field to its declared type so bad/hand-edited input
        (wrong types, nulls) can never crash load or break routing."""
        self.enable_ocr = bool(self.enable_ocr)
        self.enable_audio = bool(self.enable_audio)
        self.clean_output = bool(self.clean_output)
        self.rendered_preview = bool(self.rendered_preview)
        if self.whisper_model not in WHISPER_MODELS:
            self.whisper_model = "base"
        self.mineru_endpoint = str(self.mineru_endpoint or "").strip()
        self.default_output_dir = str(self.default_output_dir or "").strip()
