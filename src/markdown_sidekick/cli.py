"""Headless command-line interface.

Runs the same pipeline as the GUI (markitdown + OCR + audio + cleanup) so
conversions can be scripted from shells, CI, or AI agents:

    markdown-sidekick-cli convert book.pdf --split-chapters --quality
    markdown-sidekick-cli convert docs\\*.docx --out md\\
    markdown-sidekick-cli capabilities
    markdown-sidekick-cli diagnostics            (digest of the error log + debug traces)

Progress goes to stderr; per-file result lines go to stdout. Exit code is 0
when every file converted, 1 otherwise. Every failure carries an error code
(errors.py) and is recorded in the error log; ``--debug`` adds a full trace.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

from . import debuglog, errors, export
from .cleanup import clean_markdown
from .converter import ConversionEngine, ConversionResult, default_output_path
from .quality import assess_markdown
from .settings import Settings


def _expand(patterns: list[str]) -> list[Path]:
    """Expand globs ourselves — cmd.exe/PowerShell don't."""
    paths: list[Path] = []
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            paths.extend(Path(p) for p in sorted(glob.glob(pat, recursive=True)))
        else:
            paths.append(Path(pat))
    return paths


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="markdown-sidekick-cli",
        description="Convert documents, images, scanned PDFs, audio and video to Markdown.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    conv = sub.add_parser("convert", help="convert one or more files")
    conv.add_argument("files", nargs="+", help="input files (globs allowed)")
    conv.add_argument("--out", type=Path, default=None, help="output directory (default: next to each source)")
    conv.add_argument("--split-chapters", action="store_true", help="write a book folder: one file per # heading, plus index.md and manifest.json")
    conv.add_argument("--max-tokens", type=int, default=export.DEFAULT_MAX_TOKENS, help="sub-split chapters larger than this (est. tokens)")
    conv.add_argument(
        "--ai-target",
        choices=list(export.AI_TARGETS),
        default=None,
        help="write AI-sized book folders: every part fits this platform's "
        "context budget, even for heading-less documents "
        "(implies --split-chapters; overrides --max-tokens). "
        "'Gemini Notebook' writes upload-ready sources instead: one per "
        "chapter, within the per-source limit, no index/manifest",
    )
    conv.add_argument("--no-clean", action="store_true", help="skip the cleanup pass")
    conv.add_argument("--no-front-matter", action="store_true", help="omit YAML front matter (the source header for Gemini Notebook)")
    conv.add_argument("--quality", action="store_true", help="print a quality report per file")
    conv.add_argument("--json", action="store_true", help="emit one JSON object per file instead of text lines")
    conv.add_argument("--anchors", action="store_true", help="insert <!-- page N --> markers in PDF conversions (citation grounding)")
    conv.add_argument("--images", action="store_true", help="extract PDF figures (>=120 px) to an images/ folder and link each where it sits in the text (default: Settings > Output)")
    conv.add_argument("--no-images", action="store_true", help="skip figure extraction even if enabled in Settings")
    conv.add_argument("--no-layout", action="store_true", help="read PDFs with the legacy markitdown path instead of the column-aware layout engine")
    conv.add_argument("--polish", action="store_true", help="repair residual artifacts with the configured local AI (needs an endpoint + polish model in Settings > Local AI; Ollama or any OpenAI-compatible server)")
    conv.add_argument("--summarize", action="store_true", help="write a 2-3 sentence document summary into the front matter using the configured local AI (needs an endpoint + summary model in Settings > Local AI)")
    conv.add_argument("--no-ocr", action="store_true", help="disable the OCR route")
    conv.add_argument("--no-audio", action="store_true", help="disable audio/video transcription")
    conv.add_argument("--whisper-model", default=None, help="whisper model size (tiny/base/small/medium)")
    conv.add_argument("--debug", action="store_true", help="record a detailed trace of this run (timings, routing decisions, cleanup passes) under the log folder; see the diagnostics command")

    sub.add_parser("capabilities", help="report which local engines are available")
    diag = sub.add_parser(
        "diagnostics",
        help="print a digest of the error log and recent debug traces (problems, performance, quality)",
    )
    diag.add_argument("--sessions", type=int, default=1, help="how many of the newest debug sessions to include (default 1)")
    diag.add_argument("--report", type=Path, default=None, help="also write a shareable .zip (digest + logs + settings) to this path")
    diag.add_argument("--no-snapshots", action="store_true", help="leave the Markdown snapshots (document text) out of the --report zip")
    return parser


def _print_capabilities() -> int:
    from . import audio, ocr

    info = {
        "ocr": ocr.ocr_available(),
        "pdf_ocr": ocr.pdf_ocr_available(),
        "audio": audio.audio_available(),
        "settings": str(Settings.config_path()),
        "logs": str(debuglog.log_dir()),
    }
    print(json.dumps(info, indent=2))
    return 0


def _diagnostics(args: argparse.Namespace) -> int:
    from . import diagnostics

    # The digest uses characters a legacy console code page can't encode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(diagnostics.summarize(sessions=args.sessions))
    if args.report is not None:
        try:
            path = diagnostics.build_report(
                args.report, sessions=max(args.sessions, 5), include_snapshots=not args.no_snapshots
            )
        except Exception as exc:
            _print_incident(errors.report("MS-603", exc=exc, where="diagnostics --report"))
            return 1
        print(f"Report written: {path}", file=sys.stderr)
    return 0


def _print_incident(incident: errors.Incident, subject: str = "", label: str = "ERROR ") -> None:
    """What happened, the next step, and where the details are."""
    lead = f"{subject} — " if subject else ""
    print(f"{label} [{incident.code}] {lead}{incident.detail}".rstrip(" —"))
    print(f"       {incident.title}")
    print(f"       Next step: {incident.next_step}")
    print(f"       Ref {incident.ref} · details in {incident.log_path or debuglog.error_log_path()}")


def _incident_fields(incident: errors.Incident) -> dict:
    return {
        "error_code": incident.code,
        "error_ref": incident.ref,
        "error_title": incident.title,
        "next_step": incident.next_step,
        "error_hint": f"{incident.title} {incident.next_step}",
        "error_log": str(incident.log_path or debuglog.error_log_path()),
    }


def _conversion_incident(result: ConversionResult) -> errors.Incident:
    """The incident convert_file logged for a failed result, for display."""
    code = result.error_code or errors.classify_conversion(result.error or "")
    return errors.Incident(
        errors.CATALOG.get(code, errors.CATALOG["MS-199"]),
        result.error_ref or "—",
        result.error or "",
        log_path=debuglog.error_log_path(),
    )


def _convert(args: argparse.Namespace) -> int:
    settings = Settings.load()
    if args.debug or settings.debug_mode or debuglog.debug_requested():
        session = debuglog.enable("cli", snapshots=settings.debug_snapshots)
        if session is not None:
            print(f"Debug trace: {session}", file=sys.stderr)
    engine = ConversionEngine(
        enable_ocr=settings.enable_ocr and not args.no_ocr,
        enable_audio=settings.enable_audio and not args.no_audio,
        whisper_model=args.whisper_model or settings.whisper_model,
        mineru_endpoint=settings.mineru_endpoint,
        page_anchors=args.anchors or settings.page_anchors,
        pdf_layout=settings.pdf_layout and not args.no_layout,
    )
    want_images = (args.images or settings.extract_images) and not args.no_images
    # Figure markers let extraction link each image where it sits.
    engine.figure_markers = want_images
    files = _expand(args.files)
    if not files:
        print("No input files matched.", file=sys.stderr)
        return 1

    failures = 0
    total = len(files)
    for n, path in enumerate(files, start=1):
        print(f"[{n}/{total}] {path.name} …", file=sys.stderr, flush=True)
        result = engine.convert_file(
            path,
            on_subprogress=lambda src, cur, tot, unit: print(
                f"    {cur:.0f}/{tot:.0f} {unit}", file=sys.stderr, flush=True
            ),
        )
        record: dict = {"source": str(path), "engine": result.engine, "ok": result.ok}
        if not result.ok:
            failures += 1
            incident = _conversion_incident(result)
            record["error"] = result.error
            record.update(_incident_fields(incident))
            if args.json:
                print(json.dumps(record, ensure_ascii=False))
            else:
                _print_incident(incident, str(path))
            continue
        notes = list(result.warnings)  # coded problems that didn't stop the file
        try:
            with debuglog.file_context(path):
                written, report = _write_outputs(
                    path, result, args, settings, want_images, record, notes
                )
        except Exception as exc:
            # Writing failed (or, rarely, a post-processing step crashed):
            # code it, log it, and carry on with the next file.
            failures += 1
            code = errors.classify_os_error(exc) if isinstance(exc, OSError) else "MS-901"
            incident = errors.report(code, exc=exc, where=f"save {path.name}", file=str(path))
            record["ok"] = False
            record["error"] = incident.detail
            record.update(_incident_fields(incident))
            if args.json:
                print(json.dumps(record, ensure_ascii=False))
            else:
                _print_incident(incident, str(path))
            continue
        if notes:
            record["warnings"] = [
                {"code": n.code, "ref": n.ref, "title": n.title, "next_step": n.next_step}
                for n in notes
            ]

        if args.json:
            print(json.dumps(record, ensure_ascii=False))
        else:
            target = written[0] if len(written) == 1 else f"{len(written)} files in {Path(written[0]).parent}"
            print(f"ok     {path.name} [{result.engine}] -> {target}")
            if args.quality:
                print(f"       {report.summary()}")
            if report.binary_noise:
                print(f"warn   {path.name}: output looks like binary noise; source file may be corrupt or unsupported")
            for note in notes:
                print(f"warn   [{note.code}] {path.name}: {note.title}")
                print(f"       Next step: {note.next_step} (ref {note.ref})")
    return 1 if failures else 0


def _write_outputs(
    path: Path,
    result: ConversionResult,
    args: argparse.Namespace,
    settings: Settings,
    want_images: bool,
    record: dict,
    notes: list,
):
    """Clean, enrich and save one converted file; returns (written, quality).

    ``notes`` collects coded problems that leave the output usable (a skipped
    AI step); anything that stops the save raises.
    """
    markdown = result.markdown
    if not args.no_clean:
        markdown, stats = clean_markdown(markdown, engine=result.engine)
        record["cleanup"] = stats.summary()

    if args.polish and settings.ollama_endpoint and settings.polish_model:
        from . import polish

        markdown, chunks_changed = polish.polish_markdown(
            markdown,
            settings.ollama_endpoint,
            settings.polish_model,
            on_progress=lambda n, t: print(f"    polish {n}/{t}", file=sys.stderr, flush=True),
        )
        record["polished_chunks"] = chunks_changed
        if (failure := polish.take_last_failure()) is not None:
            notes.append(failure)

    summary = ""
    if args.summarize and settings.ollama_endpoint and settings.summary_model:
        from . import polish

        print("    summarizing…", file=sys.stderr, flush=True)
        summary = (
            polish.summarize_markdown(
                markdown,
                settings.ollama_endpoint,
                settings.summary_model,
                title=result.doc_title,
            )
            or ""
        )
        record["summary"] = summary
        if not summary and (failure := polish.take_last_failure()) is not None:
            notes.append(failure)

    out_dir = args.out if args.out is not None else path.parent
    book = bool(args.split_chapters or args.ai_target)
    from . import figures

    if want_images and path.suffix.lower() == ".pdf":
        # Book folders keep images/ beside the parts; single files get a
        # per-document subfolder so several conversions can share out_dir.
        if book:
            images_dir, rel_dir = out_dir / path.stem / "images", "images"
        else:
            images_dir, rel_dir = out_dir / "images" / path.stem, f"images/{path.stem}"
        try:
            figs = figures.extract_pdf_figures(path, images_dir)
        except Exception as exc:  # the Markdown is still worth saving
            figs = []
            notes.append(errors.report("MS-401", exc=exc, where=f"figures {path.name}", file=str(path)))
        if figs and settings.ollama_endpoint and settings.caption_model:
            from . import polish

            for fig in figs:
                fig.caption = (
                    polish.caption_image(
                        fig.path, settings.ollama_endpoint, settings.caption_model
                    )
                    or ""
                )
            if (failure := polish.take_last_failure()) is not None:
                notes.append(failure)
        markdown = figures.insert_figure_links(markdown, figs, rel_dir)
        if figs:
            record["figures"] = len(figs)
    else:
        markdown = figures.strip_figure_markers(markdown)
    if book:
        res = export.export_book(
            markdown,
            out_dir / path.stem,
            source=path.name,
            engine=result.engine,
            front_matter=not args.no_front_matter,
            max_tokens=(
                export.AI_TARGETS[args.ai_target] if args.ai_target else args.max_tokens
            ),
            ai_sections=args.ai_target is not None,
            summary=summary,
            title=result.doc_title,
            author=result.doc_author,
            notebook=args.ai_target in export.NOTEBOOK_TARGETS,
        )
        written = [str(p) for p in res.paths]
        if res.index_path:
            written.append(str(res.index_path))
        if res.manifest_path:
            written.append(str(res.manifest_path))
    else:
        out_path = default_output_path(path, out_dir)
        export.export_single(
            markdown,
            out_path,
            source=path.name,
            engine=result.engine,
            front_matter=not args.no_front_matter,
            summary=summary,
            title=result.doc_title,
            author=result.doc_author,
        )
        written = [str(out_path)]
    record["written"] = written

    report = assess_markdown(markdown)
    if args.quality:
        record["quality"] = report.as_dict()
    if report.binary_noise:
        record["warning"] = "output looks like binary noise; source file may be corrupt or unsupported"
    return written, report


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    errors.install_crash_hooks()
    try:
        if args.command == "capabilities":
            return _print_capabilities()
        if args.command == "diagnostics":
            return _diagnostics(args)
        return _convert(args)
    except Exception as exc:  # a bug, not a bad input: code it and say where to look
        _print_incident(errors.report("MS-900", exc=exc, where=f"cli {args.command}"))
        return 2
    finally:
        debuglog.disable()


if __name__ == "__main__":
    sys.exit(main())
