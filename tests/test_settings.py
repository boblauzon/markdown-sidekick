"""Tests for settings persistence: legacy migration and value validation."""

from __future__ import annotations

import json

from markdown_sidekick.settings import Settings, preferred_window


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


class TestByteOrderMark:
    def test_a_file_saved_with_a_bom_still_loads(self, tmp_path, monkeypatch):
        # Notepad's "UTF-8 with BOM" and PowerShell 5's Set-Content write one;
        # it used to read as an unreadable file: MS-501, every setting reset.
        from markdown_sidekick import settings as settings_store

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(settings_store, "LOAD_INCIDENT", None)
        cfg = tmp_path / "MarkdownSidekick" / "settings.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps({"window": "classic", "update_check": "off"}).encode())
        s = Settings.load()
        assert s.window == "classic" and s.update_check == "off"
        assert settings_store.LOAD_INCIDENT is None and not cfg.with_name("settings.json.bad").exists()
        assert preferred_window() == "classic"


class TestWindowChoice:
    def test_a_first_install_opens_the_new_window(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert Settings.load().window == "new" and preferred_window() == "new"

    def test_an_upgrade_opens_the_new_window(self, tmp_path, monkeypatch):
        # Settings written by 1.3 and earlier have no "window" key.
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        _write_settings(tmp_path, {"theme": "dark", "update_check": "weekly"})
        assert Settings.load().window == "new" and preferred_window() == "new"

    def test_a_chosen_classic_window_survives(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        s = Settings.load()
        s.window = "classic"
        s.save()
        assert Settings.load().window == "classic" and preferred_window() == "classic"

    def test_junk_reads_as_the_new_window(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        for junk in ("old", 3, None, ["classic"]):
            _write_settings(tmp_path, {"window": junk})
            assert Settings.load().window == "new" and preferred_window() == "new", junk

    def test_an_unreadable_file_is_left_to_the_window_to_report(self, tmp_path, monkeypatch):
        # preferred_window() runs before either window loads its settings:
        # the window's own load reports MS-501 and keeps the .bad copy, once.
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        cfg = tmp_path / "MarkdownSidekick" / "settings.json"
        cfg.parent.mkdir(parents=True)
        for text in ("{not json", "[1, 2]", ""):
            cfg.write_text(text, encoding="utf-8")
            assert preferred_window() == "new"
        cfg.write_bytes(b"\xff\xfe\x00junk")
        assert preferred_window() == "new"
        assert not (cfg.parent / "settings.json.bad").exists()
