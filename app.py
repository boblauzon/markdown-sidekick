"""Convenience launcher so the app can run from the project root.

Usage:
    python app.py             # launch the GUI
    python app.py --selftest  # verify the conversion pipeline, write a JSON
                              # report next to this file (used to validate
                              # frozen/PyInstaller builds), exit 0/1.
"""

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

        from markdown_sidekick.guide import load_user_guide

        report["checks"]["user_guide"] = len(load_user_guide()) > 2000

        report["ok"] = all(v for v in report["checks"].values() if isinstance(v, bool))
    except Exception as exc:  # the report must always be written
        report["error"] = f"{type(exc).__name__}: {exc}"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    from markdown_sidekick.ui import run

    run()
