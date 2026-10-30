"""Convenience launcher so the app can run from the project root.

Usage:
    python app.py             # launch the GUI
    python app.py --cli ...   # headless CLI (convert, capabilities, diagnostics)
    python app.py --mcp       # run the MCP server over stdio (for AI clients)
    python app.py --selftest  # verify the conversion pipeline, write a JSON
                              # report next to this file (used to validate
                              # frozen/PyInstaller builds), exit 0/1.
    python app.py --debug     # any of the above with debug mode on for this
                              # run (a detailed trace under the log folder)
    python app.py --help      # print this usage and exit (never opens the GUI)

The frozen build ships this launcher twice: MarkdownSidekick.exe (windowed)
and MarkdownSidekick-cli.exe (console). Run under the -cli name, every
argument goes to the CLI, so a terminal waits for it and sees its output.
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

            # Book metadata: an epub's own title reaches the front matter
            # (not its filename), through a verified save.
            import zipfile

            from markdown_sidekick.export import export_single

            epub = Path(td) / "Some_File_Name.epub"
            with zipfile.ZipFile(epub, "w") as zf:
                zf.writestr("mimetype", "application/epub+zip")
                zf.writestr(
                    "META-INF/container.xml",
                    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles><rootfile full-path="c.opf"/></rootfiles></container>',
                )
                zf.writestr(
                    "c.opf",
                    '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                    'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Self Test Book: '
                    "A Subtitle</dc:title><dc:creator>Ada Lovelace</dc:creator></metadata>"
                    '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
                    '</manifest><spine><itemref idref="c1"/></spine></package>',
                )
                zf.writestr(
                    "c1.xhtml",
                    '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Preface</h1>'
                    "<p>Self test body text for the metadata check.</p></body></html>",
                )
            r4 = engine.convert_file(epub)
            saved = Path(td) / "meta.md"
            if r4.ok:
                export_single(r4.markdown, saved, source=epub.name, title=r4.doc_title,
                              author=r4.doc_author, title_source=r4.title_source, meta=r4.doc_meta)
            head = saved.read_text(encoding="utf-8")[:400] if saved.exists() else ""
            report["checks"]["book_metadata"] = (
                "title: Self Test Book\n" in head and "title_source: opf" in head
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

        # Updates verify a signature offline: the crypto must be in the build.
        from markdown_sidekick import updates

        report["checks"]["update_signature"] = updates.selftest()

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


USAGE = """\
Markdown Sidekick {version} - convert documents, scans and audio to Markdown.

Usage:
  MarkdownSidekick.exe                    open the app
  MarkdownSidekick-cli.exe <command> ...  headless conversion in a terminal:
      convert FILES... [--out DIR] [--ai-target Claude|ChatGPT|...]
      capabilities                        which local engines are available
      diagnostics                         digest of the error log and debug traces
      (MarkdownSidekick-cli.exe --help lists every option)
  MarkdownSidekick.exe --cli <command>    the same CLI from the windowed exe
  MarkdownSidekick.exe --mcp              MCP server over stdio, for AI clients
  MarkdownSidekick.exe --selftest         check the conversion pipeline (exit 0/1)
  MarkdownSidekick.exe --new-ui           open the redesigned window (preview)
  --debug                                 add to any of the above to record a trace
  --help, --version                       print this or the version, and exit
"""
_HELP_FLAGS = ("--help", "-h", "/?", "-?")


def _cli_exe() -> bool:
    """Running as the console companion (MarkdownSidekick-cli.exe)?"""
    return bool(getattr(sys, "frozen", False)) and Path(sys.executable).stem.lower().endswith("-cli")


def _ensure_stdout() -> None:
    """Give a windowed (no-console) build somewhere to print: the terminal it
    was started from when there is one, else nowhere. (The windowed exe can't
    make the terminal wait for it — that is what the -cli exe is for.)"""
    if sys.stdout is not None:
        return
    if os.name == "nt":
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8")
            sys.stderr = sys.stdout
            print()  # the prompt already printed: start on a fresh line
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout


def _version() -> str:
    from markdown_sidekick import __version__

    return __version__


if __name__ == "__main__":
    # A batch's worker processes re-launch this exe with multiprocessing's
    # own arguments; this must run first, before any of ours are parsed.
    import multiprocessing

    multiprocessing.freeze_support()
    if "--debug" in sys.argv:
        # Debug mode for this run, whichever front-end starts (debuglog.ENV_DEBUG).
        sys.argv.remove("--debug")
        os.environ["MARKDOWN_SIDEKICK_DEBUG"] = "1"
    if "--mcp" in sys.argv:
        _run_mcp()
        sys.exit(0)
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if sys.argv[1:] == ["--version"] or ("--version" in sys.argv and not _cli_exe() and "--cli" not in sys.argv):
        # Either exe, alone: the app's version. (Inside a CLI command line
        # the flag is the CLI's business.)
        _ensure_stdout()
        print(f"Markdown Sidekick {_version()}")
        sys.exit(0)
    if _cli_exe() or "--cli" in sys.argv:
        # Headless conversion: `MarkdownSidekick-cli.exe convert file.pdf ...`
        # (or `MarkdownSidekick.exe --cli convert ...`).
        _ensure_stdout()
        from markdown_sidekick.cli import main as cli_main

        args = sys.argv[sys.argv.index("--cli") + 1 :] if "--cli" in sys.argv else sys.argv[1:]
        sys.exit(cli_main(args))
    if "--new-ui" in sys.argv and not any(a in _HELP_FLAGS for a in sys.argv[1:]):
        # The redesigned window (pywebview), while it is being built.
        extra = [a for a in sys.argv[1:] if a != "--new-ui"]
        if extra:
            _ensure_stdout()
            print(f"--new-ui takes no other arguments (got: {' '.join(extra)})\n", file=sys.stderr)
            print(USAGE.format(version=_version()))
            sys.exit(2)
        from markdown_sidekick.webui import run as run_webui

        run_webui()
        sys.exit(0)
    unknown = [a for a in sys.argv[1:] if a.startswith("-") and a not in _HELP_FLAGS + ("--new-ui",)]
    if any(a in _HELP_FLAGS for a in sys.argv[1:]) or unknown:
        # Never fall through to the GUI on a flag we don't know: a script or
        # an agent asking for --help must get text back, not a window.
        _ensure_stdout()
        if unknown:
            print(f"Unknown option: {' '.join(unknown)}\n", file=sys.stderr)
        print(USAGE.format(version=_version()))
        sys.exit(2 if unknown else 0)
    from markdown_sidekick.ui import run

    run()
