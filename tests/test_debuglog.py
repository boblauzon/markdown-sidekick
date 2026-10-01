"""Debug mode: the session trace, what the pipeline records in it, and the
digest/report built from the logs."""

from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path

import pytest

from markdown_sidekick import cli, debuglog, diagnostics, errors
from markdown_sidekick.cleanup import clean_markdown
from markdown_sidekick.converter import ConversionEngine

from pdfgen import make_pdf


@pytest.fixture()
def session(tmp_path, monkeypatch):
    """A debug session writing under tmp_path; yields its folder."""
    monkeypatch.setenv(debuglog.ENV_LOG_DIR, str(tmp_path / "logs"))
    debuglog.disable()
    folder = debuglog.enable("test")
    assert folder is not None
    yield folder
    debuglog.disable()


def _events(folder, name: str | None = None) -> list[dict]:
    events = diagnostics.load_jsonl(folder / "events.jsonl")
    return [e for e in events if name is None or e.get("ev") == name]


class TestDisabled:
    def test_every_entry_point_is_inert(self, tmp_path, monkeypatch):
        monkeypatch.setenv(debuglog.ENV_LOG_DIR, str(tmp_path / "logs"))
        debuglog.disable()
        debuglog.event("anything", x=1)
        assert debuglog.snapshot("raw", "text") is None
        with debuglog.span("work") as sp:
            sp["k"] = "v"
        with pytest.raises(ValueError):
            with debuglog.span("failing"):
                raise ValueError("still propagates")
        debuglog.ui_tick()
        assert not (tmp_path / "logs").exists()  # nothing written at all

    def test_conversion_leaves_no_trace_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv(debuglog.ENV_LOG_DIR, str(tmp_path / "logs"))
        debuglog.disable()
        doc = tmp_path / "a.html"
        doc.write_text("<h1>A</h1><p>text</p>", encoding="utf-8")
        assert ConversionEngine().convert_file(doc).ok
        assert not (tmp_path / "logs" / debuglog.SESSIONS_DIR).exists()


class TestSession:
    def test_start_records_the_environment(self, session):
        start = _events(session, "session.start")[0]
        assert start["source"] == "test" and start["app_version"]
        assert start["python"] and start["platform"] and start["cpu_count"]
        assert "markitdown" in start["packages"]

    def test_events_carry_context_and_spans_time_and_trace(self, session):
        with debuglog.context(file="book.pdf", conv=7):
            debuglog.event("custom", pages=3)
            with debuglog.span("step", route="x") as sp:
                sp["chars"] = 42
            with pytest.raises(RuntimeError):
                with debuglog.span("step", route="y"):
                    raise RuntimeError("bad page")
        custom = _events(session, "custom")[0]
        assert (custom["file"], custom["conv"], custom["pages"]) == ("book.pdf", 7, 3)
        ok, failed = _events(session, "step")
        assert ok["ok"] is True and ok["chars"] == 42 and ok["ms"] >= 0
        assert failed["ok"] is False and failed["error"] == "RuntimeError: bad page"
        assert "raise RuntimeError" in failed["tb"]

    def test_end_is_written_and_files_released(self, session):
        debuglog.disable()
        assert _events(session, "session.end")
        (session / "events.jsonl").unlink()  # would fail on Windows if still open

    def test_concurrent_writers_never_tear_a_line(self, session):
        def work(n: int) -> None:
            for i in range(200):
                debuglog.event("burst", n=n, i=i, pad="x" * 200)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        raw = (session / "events.jsonl").read_text(encoding="utf-8").splitlines()
        parsed = [json.loads(line) for line in raw]  # every line is whole JSON
        assert sum(1 for e in parsed if e["ev"] == "burst") == 1600

    def test_snapshots_need_a_file_context_and_the_option(self, session):
        assert debuglog.snapshot("raw", "orphan text") is None
        with debuglog.context(file="My Book: Vol 1.pdf", conv=3):
            path = debuglog.snapshot("raw", "# Title\n")
            assert path.name == "003-My_Book_Vol_1.raw.md"
            debuglog.enable("test", snapshots=False)  # retune the live session
            assert debuglog.snapshot("cleaned", "# Title\n") is None

    def test_ui_stall_is_recorded(self, session, monkeypatch):
        debuglog.ui_tick()
        monkeypatch.setattr(debuglog, "_last_tick", time.monotonic() - 2.5)
        debuglog.event("ui.action", action="save")
        debuglog.ui_tick()
        stall = _events(session, "ui.stall")[0]
        assert stall["gap_ms"] >= 2500 and stall["last_event"] == "ui.action"

    def test_library_warnings_join_the_timeline(self, session):
        import logging

        logging.getLogger("pdfminer.fake").warning("Cannot locate objstm %s", 12)
        record = _events(session, "log")[0]
        assert record["logger"] == "pdfminer.fake" and "objstm 12" in record["msg"]

    def test_old_sessions_are_pruned(self, tmp_path, monkeypatch):
        monkeypatch.setenv(debuglog.ENV_LOG_DIR, str(tmp_path / "logs"))
        monkeypatch.setattr(debuglog, "_KEEP_SESSIONS", 3)
        root = tmp_path / "logs" / debuglog.SESSIONS_DIR
        for n in range(6):
            (root / f"20200101-00000{n}-1-gui").mkdir(parents=True)
        debuglog.disable()
        newest = debuglog.enable("test")
        debuglog.disable()
        kept = sorted(p.name for p in root.iterdir())
        assert len(kept) == 3 and newest.name in kept and "20200101-000005-1-gui" in kept


class TestErrorLog:
    def test_rotation_keeps_a_bounded_history(self, tmp_path, monkeypatch):
        monkeypatch.setenv(debuglog.ENV_LOG_DIR, str(tmp_path / "logs"))
        monkeypatch.setattr(debuglog, "_ERROR_LOG_MAX_BYTES", 2_000)
        for n in range(200):
            debuglog.write_error({"n": n, "pad": "x" * 200})
        files = debuglog.error_log_files()
        assert 2 <= len(files) <= 1 + debuglog._ERROR_LOG_KEEP
        assert files[-1].name == "errors.jsonl"
        last = diagnostics.load_jsonl(files[-1])[-1]
        assert last["n"] == 199  # newest entries are in the live file

    def test_errors_are_logged_without_debug_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv(debuglog.ENV_LOG_DIR, str(tmp_path / "logs"))
        debuglog.disable()
        incident = errors.report("MS-299", detail="disk trouble", where="unit always-on")
        assert incident.log_path == tmp_path / "logs" / "errors.jsonl"
        assert diagnostics.load_errors(None)[-1]["ref"] == incident.ref


class TestPipelineTrace:
    def test_conversion_is_traced_end_to_end(self, session, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(make_pdf(["A page with plenty of ordinary text on it for the reader."] * 3))
        result = ConversionEngine(enable_ocr=False).convert_file(pdf)
        with debuglog.file_context(pdf):
            clean_markdown(result.markdown, engine=result.engine)

        start = _events(session, "convert.start")[0]
        assert start["file"] == "doc.pdf" and start["bytes"] > 0 and start["config"]["pdf_layout"]
        route = _events(session, "route")[0]
        assert route["route"] == "pdflayout" and route["ok"] and route["outcome"] == "used"
        layout = _events(session, "pdflayout.done")[0]
        assert layout["stats"]["pages"] == 3 and set(layout["phases_ms"]) >= {"read_pages", "assemble"}
        assert [p[0] for p in layout["pages"]] == [1, 2, 3] and layout["pages"][0][1] == "text"
        assert _events(session, "pages.timing")[0]["pages"] == 3
        end = _events(session, "convert.end")[0]
        assert end["ok"] and end["engine"] == "pdflayout" and end["chars"] > 0 and end["ms"] >= 0
        # Raw and cleaned Markdown are kept side by side for quality review.
        names = sorted(p.name for p in (session / "snapshots").iterdir())
        assert [n.split(".", 1)[1] for n in names] == ["cleaned.md", "raw.md"]
        assert names[0].split(".")[0] == names[1].split(".")[0]  # same conversion id + file

    def test_cleanup_records_what_each_pass_removed(self, session):
        text = (
            "Intro paragraph here.\n\n• first point\n• second point\n• third point\n\n"
            "Closing paragraph.\n"
        )
        with debuglog.file_context(Path("draft.docx")):
            cleaned, stats = clean_markdown(text, engine="markitdown")
        done = _events(session, "cleanup.done")[0]
        assert done["chars_in"] == len(text) and done["chars_out"] == len(cleaned)
        assert done["lines_in"] == text.count("\n") + 1
        assert done["stats"]["bullets_normalized"] == stats.bullets_normalized == 3
        by_pass = {p["pass"]: p for p in done["passes"]}
        assert {"normalize_characters", "normalize_bullets", "collapse_blank_runs"} <= set(by_pass)
        # A pass that changed nothing records only its time…
        assert set(by_pass["strip_repeated_blocks"]) == {"pass", "ms"}
        # …one that did says how many lines it touched, with samples of them.
        rewritten = by_pass["normalize_bullets"]
        assert rewritten["lines_gone"] == 3 and rewritten["ms"] >= 0
        assert rewritten["samples"][0] == "• first point"

    def test_failed_route_is_traced_with_its_traceback(self, session, tmp_path):
        fake = tmp_path / "fake.pdf"
        fake.write_text("plain text wearing a .pdf extension", encoding="utf-8")
        ConversionEngine().convert_file(fake)
        failed = [e for e in _events(session) if e.get("ev") in ("route", "pdf.analysis") and e.get("ok") is False]
        assert failed and all("PdfiumError" in e["error"] and e["tb"] for e in failed)
        error = _events(session, "error")[0]
        assert error["code"] == "MS-125" and error["ref"]


class TestDiagnostics:
    def test_digest_puts_problems_performance_and_quality_in_one_place(self, session, tmp_path):
        good = tmp_path / "doc.pdf"
        good.write_bytes(make_pdf(["A page with plenty of ordinary text on it for the reader."] * 2))
        engine = ConversionEngine(enable_ocr=False)
        result = engine.convert_file(good)
        with debuglog.file_context(good):
            from markdown_sidekick.quality import assess_markdown

            assess_markdown(clean_markdown(result.markdown, engine=result.engine)[0])
        engine.convert_file(tmp_path / "gone.docx")
        debuglog.disable()

        digest = diagnostics.summarize(sessions=1)
        assert "## Error log" in digest and "MS-103" in digest
        assert f"## Debug session {session.name}" in digest
        assert "### Problems" in digest and "### Performance" in digest and "### Quality" in digest
        assert "doc.pdf" in digest and "pdflayout" in digest
        assert "| doc.pdf | 100 |" in digest  # the quality table

    def test_digest_without_any_logs_says_how_to_get_them(self, tmp_path, monkeypatch):
        monkeypatch.setenv(debuglog.ENV_LOG_DIR, str(tmp_path / "empty"))
        debuglog.disable()
        digest = diagnostics.summarize()
        assert "No errors recorded" in digest and "--debug" in digest

    def test_torn_last_line_is_skipped(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text('{"ev": "a"}\n{"ev": "b", "unfinished', encoding="utf-8")
        assert [e["ev"] for e in diagnostics.load_jsonl(log)] == ["a"]

    def test_report_zip_bundles_digest_logs_and_settings(self, session, tmp_path, monkeypatch):
        from markdown_sidekick.settings import Settings

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
        Settings(debug_mode=True).save()
        with debuglog.context(file="a.pdf", conv=1):
            debuglog.snapshot("raw", "secret document text")
        errors.report("MS-299", detail="for the report", where="unit report")
        debuglog.disable()

        full = diagnostics.build_report(tmp_path / "full.zip")
        names = set(zipfile.ZipFile(full).namelist())
        assert {"summary.md", "settings.json", "environment.json", "logs/errors.jsonl"} <= names
        assert f"logs/sessions/{session.name}/events.jsonl" in names
        assert any(n.endswith("001-a.raw.md") for n in names)
        # Document text can be left out.
        lean = diagnostics.build_report(tmp_path / "lean.zip", include_snapshots=False)
        assert not any("snapshots" in n for n in zipfile.ZipFile(lean).namelist())


class TestCli:
    def test_debug_flag_writes_a_session(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(debuglog.ENV_LOG_DIR, str(tmp_path / "logs"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
        doc = tmp_path / "a.html"
        doc.write_text("<h1>A</h1><p>text</p>", encoding="utf-8")
        assert cli.main(["convert", str(doc), "--out", str(tmp_path / "o"), "--debug"]) == 0
        assert "Debug trace:" in capsys.readouterr().err
        (folder,) = diagnostics.recent_sessions(5)
        assert folder.name.endswith("-cli")
        names = {e["ev"] for e in _events(folder)}
        assert {"session.start", "convert.start", "convert.end", "cleanup.done", "quality",
                "export.single", "session.end"} <= names

    def test_diagnostics_command_prints_the_digest_and_writes_a_report(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(debuglog.ENV_LOG_DIR, str(tmp_path / "logs"))
        errors.report("MS-201", detail="for the command", where="unit cli")
        rc = cli.main(["diagnostics", "--report", str(tmp_path / "r.zip")])
        assert rc == 0
        assert "# Markdown Sidekick diagnostics" in capsys.readouterr().out
        assert "summary.md" in zipfile.ZipFile(tmp_path / "r.zip").namelist()
