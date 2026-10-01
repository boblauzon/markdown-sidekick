"""Error codes: the catalog, the always-on error log, and how failures in
each front-end get a code, a reference and a next step."""

from __future__ import annotations

import json
import re
import urllib.error
from importlib import resources

import pytest

from markdown_sidekick import audio, cli, debuglog, errors, pdflayout
from markdown_sidekick.converter import ConversionEngine
from markdown_sidekick.diagnostics import load_errors

from pdfgen import make_pdf


def _logged(ref: str) -> dict:
    """The error-log record with this reference."""
    matches = [r for r in load_errors(None) if r.get("ref") == ref]
    assert len(matches) == 1, f"ref {ref} logged {len(matches)} times"
    return matches[0]


class TestCatalog:
    def test_every_code_is_well_formed(self):
        assert len(errors.CATALOG) == len(errors._SPECS)  # no duplicate codes
        for code, spec in errors.CATALOG.items():
            assert re.fullmatch(r"MS-\d{3}", code)
            assert spec.title.endswith(".") and spec.next_step.endswith(".")
            assert spec.severity in ("error", "warning", "info")
            assert spec.action == "" or spec.action in errors.ACTIONS

    def test_next_steps_read_the_same_in_every_front_end(self):
        # The CLI and MCP server print these too: no "click", no GUI-only
        # button names in the text (the GUI adds the button itself).
        for spec in errors.CATALOG.values():
            assert "click" not in spec.next_step.lower(), spec.code
            assert "Copy details" not in spec.next_step, spec.code

    def test_every_code_is_documented_in_the_user_guide(self):
        # The guide's tables are generated from the catalog; a code added or
        # reworded without regenerating them fails here. To fix:
        #   python -m markdown_sidekick.errors   -> paste over the tables
        guide = resources.files("markdown_sidekick").joinpath("USERGUIDE.md").read_text("utf-8")
        stale = [row for row in errors.guide_tables().splitlines() if row and row not in guide]
        assert not stale, f"USERGUIDE.md → Error codes is out of date: {stale[:3]}"


class TestReport:
    def test_incident_lands_in_the_error_log_with_traceback_and_context(self):
        try:
            raise ValueError("kaboom")
        except ValueError as exc:
            incident = errors.report("MS-299", exc=exc, where="unit save", target="C:/x.md")
        assert re.fullmatch(r"[0-9A-F]{6}", incident.ref)
        record = _logged(incident.ref)
        assert record["code"] == "MS-299" and record["severity"] == "error"
        assert record["detail"] == "ValueError: kaboom"
        assert "raise ValueError" in record["tb"]
        assert record["context"] == {"target": "C:/x.md"}
        assert record["app_version"] and record["os"]

    def test_same_error_again_is_the_same_incident(self):
        # A callback failing on every Tk tick must not write a line per tick.
        first = errors.report("MS-901", detail="loop failure", where="unit dedupe")
        second = errors.report("MS-901", detail="loop failure", where="unit dedupe")
        assert second is first
        other = errors.report("MS-901", detail="a different failure", where="unit dedupe")
        assert other.ref != first.ref

    def test_details_text_is_a_complete_support_request(self):
        incident = errors.report("MS-201", detail="PermissionError: denied", where="unit details")
        text = incident.details_text()
        assert f"Error MS-201 · Ref {incident.ref}" in text
        assert "What happened:" in text and "Next step:" in text
        assert "PermissionError: denied" in text and "errors.jsonl" in text

    def test_unknown_code_degrades_to_the_generic_one(self):
        assert errors.report("MS-000", detail="?", where="unit unknown").code == "MS-901"


class TestClassification:
    def test_saving_errors(self):
        locked = PermissionError(13, "in use")
        locked.winerror = 32
        assert errors.classify_os_error(locked) == "MS-204"  # open elsewhere, not "no permission"
        assert errors.classify_os_error(PermissionError(13, "Permission denied")) == "MS-201"
        assert errors.classify_os_error(OSError(28, "No space left on device")) == "MS-202"
        long_path = OSError(2, "too long")
        long_path.winerror = 206
        assert errors.classify_os_error(long_path) == "MS-203"
        assert errors.classify_os_error(OSError("something else")) == "MS-299"

    def test_local_ai_errors(self):
        not_found = urllib.error.HTTPError("http://x", 404, "Not Found", None, None)
        assert errors.classify_http(not_found) == "MS-302"
        assert errors.classify_http(urllib.error.URLError(TimeoutError("timed out"))) == "MS-303"
        assert errors.classify_http(urllib.error.URLError(ConnectionRefusedError())) == "MS-301"
        assert errors.classify_http(ValueError("bad json")) == "MS-399"


class TestConversionCodes:
    def test_failure_carries_code_and_log_reference(self, tmp_path):
        res = ConversionEngine().convert_file(tmp_path / "nope.pdf")
        assert not res.ok and res.error_code == "MS-103"
        assert _logged(res.error_ref)["context"]["file"].endswith("nope.pdf")

    def test_image_with_ocr_off_points_at_the_ocr_switch(self, tmp_path):
        from PIL import Image

        png = tmp_path / "scan.png"
        Image.new("RGB", (200, 60), "white").save(png)
        res = ConversionEngine(enable_ocr=False).convert_file(png)
        assert not res.ok and res.error_code == "MS-110"
        assert errors.CATALOG["MS-110"].action == "enable_ocr"

    def test_crashed_layout_reader_is_a_logged_warning_not_a_silent_fallback(
        self, tmp_path, monkeypatch
    ):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(make_pdf(["A page with plenty of ordinary text on it for the reader."] * 2))

        def boom(*args, **kwargs):
            raise RuntimeError("layout exploded")

        monkeypatch.setattr(pdflayout, "extract_markdown", boom)
        res = ConversionEngine(enable_ocr=False).convert_file(pdf)
        # The basic reader still produced the document…
        assert res.ok and res.engine == "markitdown" and "ordinary text" in res.markdown
        # …but the user is told, and the traceback is on disk.
        assert [w.code for w in res.warnings] == ["MS-120"]
        record = _logged(res.warnings[0].ref)
        assert record["severity"] == "warning" and "layout exploded" in record["tb"]

    def test_unreadable_pdf_gets_one_accurate_warning(self, tmp_path):
        # pdfium can't open it, so every PDF route fails the same way: one
        # "couldn't be opened as a PDF", not an OCR and a layout warning.
        pdf = tmp_path / "fake.pdf"
        pdf.write_text("plain text wearing a .pdf extension", encoding="utf-8")
        res = ConversionEngine().convert_file(pdf)
        assert res.ok
        assert [w.code for w in res.warnings] == ["MS-125"]

    def test_speech_model_download_failure_is_the_reported_cause(self, tmp_path, monkeypatch):
        # The mp3 reaches markitdown and "produces no text", but the message
        # the user needs is about the model download.
        mp3 = tmp_path / "talk.mp3"
        mp3.write_bytes(b"\x00" * 20_000)

        class Offline:
            model_size = "base"

            def transcribe_to_markdown(self, *args, **kwargs):
                raise ConnectionError("HTTPSConnectionPool(host='huggingface.co'): Max retries")

        monkeypatch.setattr(audio, "audio_available", lambda: True)
        engine = ConversionEngine()
        monkeypatch.setattr(engine, "_audio_engine", lambda: Offline())
        res = engine.convert_file(mp3)
        assert not res.ok and res.error_code == "MS-111"
        assert "HTTPSConnectionPool" in _logged(res.error_ref)["tb"]

    def test_route_failure_blames_the_file_when_the_file_is_the_problem(self):
        code = ConversionEngine._route_failure_code
        assert code("whisper", RuntimeError("InvalidDataError: moov atom not found")) == "MS-109"
        assert code("whisper", RuntimeError("CUDA out of luck")) == "MS-112"

        class UnidentifiedImageError(Exception):
            pass

        assert code("ocr_image", UnidentifiedImageError("cannot identify image file")) == "MS-107"
        assert code("ocr_image", RuntimeError("DirectML device lost")) == "MS-113"


class TestCliErrors:
    def test_error_output_has_code_next_step_and_log_location(self, tmp_path, capsys):
        rc = cli.main(["convert", str(tmp_path / "nope.pdf")])
        out = capsys.readouterr().out
        assert rc == 1
        assert "ERROR  [MS-103]" in out and "Next step:" in out and "errors.jsonl" in out

    def test_json_record_carries_the_code(self, tmp_path, capsys):
        cli.main(["convert", str(tmp_path / "nope.pdf"), "--json"])
        record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert record["ok"] is False and record["error_code"] == "MS-103"
        assert record["next_step"] and _logged(record["error_ref"])

    def test_save_failure_is_coded_and_does_not_stop_the_batch(self, tmp_path, capsys):
        good = tmp_path / "a.html"
        good.write_text("<h1>A</h1><p>text</p>", encoding="utf-8")
        blocker = tmp_path / "out"
        blocker.write_text("a file where the output folder should go", encoding="utf-8")
        rc = cli.main(["convert", str(good), "--out", str(blocker), "--json"])
        record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rc == 1 and record["ok"] is False
        assert record["error_code"].startswith("MS-2")  # a saving code, not a crash
        assert "Traceback" in _logged(record["error_ref"])["tb"]

    def test_fallback_warning_is_printed_with_its_code(self, tmp_path, capsys):
        pdf = tmp_path / "fake.pdf"
        pdf.write_text("plain text wearing a .pdf extension", encoding="utf-8")
        rc = cli.main(["convert", str(pdf), "--out", str(tmp_path / "o")])
        out = capsys.readouterr().out
        assert rc == 0 and "warn   [MS-125]" in out


class TestSettingsErrors:
    def test_corrupt_settings_file_is_backed_up_and_reported(self, tmp_path, monkeypatch):
        from markdown_sidekick import settings as settings_store

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        cfg = tmp_path / "MarkdownSidekick"
        cfg.mkdir()
        (cfg / "settings.json").write_text("{ not json", encoding="utf-8")
        monkeypatch.setattr(settings_store, "LOAD_INCIDENT", None)
        loaded = settings_store.Settings.load()
        assert loaded == settings_store.Settings()  # defaults, the app still starts
        # The user's file survives the next save, and they are told.
        assert (cfg / "settings.json.bad").read_text(encoding="utf-8") == "{ not json"
        assert settings_store.LOAD_INCIDENT.code == "MS-501"

    def test_debug_options_round_trip(self, tmp_path, monkeypatch):
        from markdown_sidekick.settings import Settings

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        settings = Settings(debug_mode=True, debug_snapshots=False)
        settings.save()
        loaded = Settings.load()
        assert loaded.debug_mode is True and loaded.debug_snapshots is False


class TestLocalAiErrors:
    def test_dead_server_is_reported_once_the_summary_is_lost(self):
        from markdown_sidekick import polish

        polish.take_last_failure()
        # Nothing listens on this port: the schema attempt fails fast, the
        # plain-text retry fails too, and only then is it an incident.
        summary = polish.summarize_markdown(
            "# Title\n\n" + "Real body text. " * 50, "http://127.0.0.1:9", "some-model"
        )
        failure = polish.take_last_failure()
        assert summary is None
        assert failure is not None and failure.code in ("MS-301", "MS-303")
        assert polish.take_last_failure() is None  # taking clears it


@pytest.fixture(autouse=True)
def _no_debug_session():
    yield
    debuglog.disable()
