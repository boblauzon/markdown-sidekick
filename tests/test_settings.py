"""Tests for settings persistence: legacy migration and value validation."""

from __future__ import annotations

import json

from markdown_sidekick.settings import Settings


def _write_settings(tmp_path, data: dict) -> None:
    cfg_dir = tmp_path / "MarkdownSidekick"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "settings.json").write_text(json.dumps(data), encoding="utf-8")


class TestMigration:
    def test_legacy_split_chapters_true_becomes_chapters_style(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        _write_settings(tmp_path, {"split_chapters": True})
        assert Settings.load().export_style == "chapters"

    def test_legacy_split_chapters_false_stays_single(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        _write_settings(tmp_path, {"split_chapters": False})
        assert Settings.load().export_style == "single"

    def test_explicit_export_style_wins_over_legacy_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        _write_settings(tmp_path, {"split_chapters": True, "export_style": "ai"})
        assert Settings.load().export_style == "ai"


class TestValidation:
    def test_junk_ai_target_resets_to_claude(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        _write_settings(tmp_path, {"ai_target": "GPT-9"})
        assert Settings.load().ai_target == "Claude"

    def test_valid_ai_target_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        _write_settings(tmp_path, {"ai_target": "Local LLM"})
        assert Settings.load().ai_target == "Local LLM"

    def test_junk_export_style_resets_to_single(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        _write_settings(tmp_path, {"export_style": "everything"})
        assert Settings.load().export_style == "single"


class TestLocalAIModels:
    def test_summary_model_round_trips_and_tolerates_junk(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        _write_settings(tmp_path, {"summary_model": "  llama3.2 "})
        assert Settings.load().summary_model == "llama3.2"
        _write_settings(tmp_path, {"summary_model": None})
        assert Settings.load().summary_model == ""
        _write_settings(tmp_path, {"summary_model": 42})
        assert Settings.load().summary_model == "42"


class TestPdfReading:
    def test_layout_and_figures_default_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        s = Settings.load()  # no file yet: pure defaults
        assert s.pdf_layout is True and s.extract_images is True

    def test_saved_opt_out_is_kept(self, tmp_path, monkeypatch):
        # Existing users who never enabled figure extraction keep their choice.
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        _write_settings(tmp_path, {"extract_images": False, "pdf_layout": False})
        s = Settings.load()
        assert s.extract_images is False and s.pdf_layout is False

    def test_junk_pdf_layout_coerced_to_bool(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        _write_settings(tmp_path, {"pdf_layout": 0})
        assert Settings.load().pdf_layout is False
