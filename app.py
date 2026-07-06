"""Convenience launcher so the app can run from the project root.

Usage:
    python app.py             # launch the GUI
    python app.py --mcp       # run the MCP server over stdio (for AI clients)
    python app.py --selftest  # verify the conversion pipeline, write a JSON
                              # report next to this file (used to validate
                              # frozen/PyInstaller builds), exit 0/1.
"""

import os
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    # Running from source: make src/ importable. Frozen builds bundle the package.
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def _selftest() -> int:
    """Exercise the real pipeline end-to-end; report to selftest_report.json."""
    import json
    import tempfile

    report: dict = {"ok": False, "checks": {}}
    out_path = Path(tempfile.gettempdir()) / "markdown_sidekick_selftest.json"
    try:
        from markdown_sidekick import audio, ocr
        from markdown_sidekick.cleanup import clean_markdown
        from markdown_sidekick.converter import ConversionEngine

        report["checks"]["ocr_available"] = ocr.ocr_available()
        report["checks"]["pdf_ocr_available"] = ocr.pdf_ocr_available()
        report["checks"]["audio_available"] = audio.audio_available()

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
