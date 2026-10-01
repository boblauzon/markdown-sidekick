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

from . import errors
from .export import AI_TARGETS

WHISPER_MODELS = ("tiny", "base", "small", "medium")

# The incident from the last load of an unreadable settings file (MS-501),
# for the GUI to surface once at startup; None when the file was fine.
LOAD_INCIDENT: "errors.Incident | None" = None


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
    # Column-aware PDF reading (pdflayout): TrimBox clipping, reading order by
    # column, bookmark headings. Off = the legacy markitdown PDF path.
    pdf_layout: bool = True
    # OCR compute device: "auto" (DirectML GPU when present, else CPU),
    # "cpu", or "gpu". Kept as a plain tuple check in normalize() — settings
    # must not import ocr.py (it pulls numpy/pypdfium2 at module level).
    ocr_device: str = "auto"
    enable_audio: bool = True
    whisper_model: str = "base"
    mineru_endpoint: str = ""  # blank = disabled
    default_output_dir: str = ""  # blank = ask each time
    clean_output: bool = True
    rendered_preview: bool = True
    # -- AI-friendly export --------------------------------------------------
    export_front_matter: bool = True  # YAML front matter on saved files
    # How "Save Markdown" writes output — chosen in the main window's export
    # bar and remembered here: "single" (one .md per source), "chapters"
    # (book folder split on chapter headings), "ai" (sections sized for an AI
    # platform's context window).
    export_style: str = "single"
    ai_target: str = "Claude"  # which platform "ai" sections are sized for
    page_anchors: bool = False  # <!-- p.N --> comments on PDF conversions
    # Extract PDF figures (>=120 px) to an images/ folder at save time and
    # link each one where it sits in the text.
    extract_images: bool = True
    # -- optional local-LLM extras (blank endpoint = disabled) ----------------
    ollama_endpoint: str = ""  # e.g. http://localhost:11434
    polish_model: str = ""  # e.g. llama3.2 — repairs residual artifacts
    caption_model: str = ""  # e.g. llava — alt-text for extracted figures
    summary_model: str = ""  # e.g. llama3.2 — 2-3 sentence summary in front matter
    # -- diagnostics (the error log itself is always on) ---------------------
    debug_mode: bool = False  # detailed session trace under logs/sessions
    # In debug mode, also keep each conversion's raw + cleaned Markdown.
    debug_snapshots: bool = True

    # -- persistence ---------------------------------------------------------
    @classmethod
    def config_path(cls) -> Path:
        return _config_path()

    @classmethod
    def load(cls) -> "Settings":
        """Load settings, tolerating a missing, corrupt, or wrong-typed file.

        An unreadable file is copied to settings.json.bad before the defaults
        take over (the next save would otherwise destroy the user's choices
        for good) and reported as MS-501.
        """
        global LOAD_INCIDENT
        path = _config_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Migrate the pre-export-bar key: split_chapters=true meant
            # "write book folders", now expressed as export_style="chapters".
            if data.get("split_chapters") and "export_style" not in data:
                data["export_style"] = "chapters"
            known = {f.name for f in fields(cls)}
            settings = cls(**{k: v for k, v in data.items() if k in known})
            settings.normalize()
            return settings
        except Exception as exc:
            backup = path.with_name(path.name + ".bad")
            try:
                backup.write_bytes(path.read_bytes())
            except OSError:
                backup = None
            LOAD_INCIDENT = errors.report(
                "MS-501", exc=exc, where="settings load", file=str(path), backup=str(backup)
            )
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
        self.pdf_layout = bool(self.pdf_layout)
        if self.ocr_device not in ("auto", "cpu", "gpu"):
            self.ocr_device = "auto"
        self.enable_audio = bool(self.enable_audio)
        self.clean_output = bool(self.clean_output)
        self.rendered_preview = bool(self.rendered_preview)
        if self.whisper_model not in WHISPER_MODELS:
            self.whisper_model = "base"
        self.mineru_endpoint = str(self.mineru_endpoint or "").strip()
        self.default_output_dir = str(self.default_output_dir or "").strip()
        self.export_front_matter = bool(self.export_front_matter)
        if self.export_style not in ("single", "chapters", "ai"):
            self.export_style = "single"
        if self.ai_target not in AI_TARGETS:
            self.ai_target = "Claude"
        self.page_anchors = bool(self.page_anchors)
        self.extract_images = bool(self.extract_images)
        self.ollama_endpoint = str(self.ollama_endpoint or "").strip().rstrip("/")
        self.polish_model = str(self.polish_model or "").strip()
        self.caption_model = str(self.caption_model or "").strip()
        self.summary_model = str(self.summary_model or "").strip()
        self.debug_mode = bool(self.debug_mode)
        self.debug_snapshots = bool(self.debug_snapshots)
