"""Convenience launcher so the app can run from the project root.

Usage:
    python app.py             # launch the GUI
    python app.py --mcp       # run the MCP server over stdio (for AI clients)
    python app.py --selftest  # verify the conversion pipeline, write a JSON
                              # report next to this file (used to validate
                              # frozen/PyInstaller builds), exit 0/1.
    python app.py --debug     # any of the above with debug mode on for this
                              # run (a detailed trace under the log folder)
"""

import os
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    # Running from source: make src/ importable. Frozen builds bundle the package.
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def _two_column_pdf() -> bytes:
    """A one-page PDF whose content stream interleaves two columns row by
    row — the layout engine must still read the left column first."""
    rows = [("LEFT one", "RIGHT one"), ("LEFT two", "RIGHT two"), ("LEFT three", "RIGHT three")]
    ops = []
    for n, (left, right) in enumerate(rows):
        y = 700 - 14 * n
        ops.append(f"BT /F1 11 Tf 72 {y} Td ({left}) Tj ET BT /F1 11 Tf 330 {y} Td ({right}) Tj ET")
    content = " ".join(ops).encode("ascii")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R "
        b"/Resources << /Font << /F1 3 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (num, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _selftest() -> int:
    """Exercise the real pipeline end-to-end; report to selftest_report.json."""
    import json
    import tempfile

    report: dict = {"ok": False, "checks": {}}
    out_path = Path(tempfile.gettempdir()) / "markdown_sidekick_selftest.json"
    # The selftest's logs go to their own folder (not the user's), and the
    # whole run is traced: every check doubles as a test of debug mode.
    log_root = Path(tempfile.mkdtemp(prefix="ms-selftest-logs-"))
    previous_log_dir = os.environ.get("MARKDOWN_SIDEKICK_LOG_DIR")
    os.environ["MARKDOWN_SIDEKICK_LOG_DIR"] = str(log_root)
    try:
        from markdown_sidekick import audio, ocr
        from markdown_sidekick.cleanup import clean_markdown
        from markdown_sidekick.converter import ConversionEngine

        report["checks"]["ocr_available"] = ocr.ocr_available()
        report["checks"]["pdf_ocr_available"] = ocr.pdf_ocr_available()
        report["checks"]["audio_available"] = audio.audio_available()

        from markdown_sidekick import debuglog, diagnostics

        debuglog.enable("selftest")
        with tempfile.TemporaryDirectory() as td:
            html = Path(td) / "t.html"
            html.write_text("<h1>Self Test</h1><p><b>bold</b> works.</p>", encoding="utf-8")
            engine = ConversionEngine()
            r = engine.convert_file(html)
            report["checks"]["html_convert"] = bool(r.ok and "Self Test" in r.markdown)

            # OCR a generated text image through the real engine.
            if ocr.ocr_available():
                from PIL import Image, ImageDraw

                img = Image.new("RGB", (640, 120), "white")
                ImageDraw.Draw(img).text((20, 40), "SELFTEST OCR 12345", fill="black")
                png = Path(td) / "t.png"
                img.save(png)
                r2 = engine.convert_file(png)
                report["checks"]["image_ocr"] = bool(r2.ok and r2.engine == "ocr")

            # Column-aware PDF reading: left column before right, despite a
            # row-interleaved content stream.
            pdf = Path(td) / "columns.pdf"
            pdf.write_bytes(_two_column_pdf())
            r3 = ConversionEngine(enable_ocr=False).convert_file(pdf)
            md3 = r3.markdown if r3.ok else ""
            report["checks"]["pdf_layout"] = bool(
                r3.engine == "pdflayout"
                and "LEFT three" in md3
                and "RIGHT one" in md3
                and md3.index("LEFT three") < md3.index("RIGHT one")
            )

            cleaned, _ = clean_markdown("import os\nx = 1\n")
            report["checks"]["cleanup"] = "```python" in cleaned

            # AI-friendly export + quality assessment round-trip.
            from markdown_sidekick.export import export_book
            from markdown_sidekick.quality import assess_markdown

            book = export_book(
                "# One\n\nalpha\n\n# Two\n\nbeta\n", Path(td) / "book", source="s.pdf"
            )
            report["checks"]["export_split"] = (
                len(book.paths) == 2 and book.manifest_path is not None
            )
            report["checks"]["quality"] = assess_markdown("# T\n\nbody\n").score > 0

            # A failure must get its code and an error-log entry, and the
            # debug trace must read back as a digest.
            missing = engine.convert_file(Path(td) / "missing.pdf")
            debuglog.disable()
            logged = diagnostics.load_errors(None)
            report["checks"]["error_codes"] = bool(
                missing.error_code == "MS-103"
                and any(r.get("ref") == missing.error_ref for r in logged)
            )
            digest = diagnostics.summarize(sessions=1)
            report["checks"]["debug_trace"] = "MS-103" in digest and "t.html" in digest

        from markdown_sidekick.guide import load_user_guide

        report["checks"]["user_guide"] = len(load_user_guide()) > 2000

        # The MCP server (and fastmcp) must be present so `--mcp` works.
        try:
            import markdown_sidekick.mcp_server  # noqa: F401

            report["checks"]["mcp_server"] = True
        except Exception:
            report["checks"]["mcp_server"] = False

        report["ok"] = all(v for v in report["checks"].values() if isinstance(v, bool))
    except Exception as exc:  # the report must always be written
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            from markdown_sidekick import debuglog

            debuglog.disable()
        except Exception:
            pass
        if previous_log_dir is None:
            os.environ.pop("MARKDOWN_SIDEKICK_LOG_DIR", None)
        else:
            os.environ["MARKDOWN_SIDEKICK_LOG_DIR"] = previous_log_dir
        import shutil

        shutil.rmtree(log_root, ignore_errors=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


def _run_mcp() -> None:
    """Host the MCP server over stdio (how AI clients launch the frozen exe).

    In a windowed (no-console) build the unpiped std streams are None; an MCP
    client always provides pipes, so valid stdin/stdout mean we can serve.
    """
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if sys.stdin is None or sys.stdout is None:
        sys.exit("The MCP server must be launched by an MCP client over stdio.")
    from markdown_sidekick.mcp_server import main as mcp_main

    mcp_main()


if __name__ == "__main__":
    if "--debug" in sys.argv:
        # Debug mode for this run, whichever front-end starts (debuglog.ENV_DEBUG).
        sys.argv.remove("--debug")
        os.environ["MARKDOWN_SIDEKICK_DEBUG"] = "1"
    if "--mcp" in sys.argv:
        _run_mcp()
        sys.exit(0)
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--cli" in sys.argv:
        # Headless conversion: `MarkdownSidekick.exe --cli convert file.pdf ...`
        from markdown_sidekick.cli import main as cli_main

        args = sys.argv[sys.argv.index("--cli") + 1 :]
        sys.exit(cli_main(args))
    from markdown_sidekick.ui import run

    run()
