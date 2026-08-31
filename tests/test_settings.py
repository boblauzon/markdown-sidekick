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
