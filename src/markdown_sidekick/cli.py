"""Headless command-line interface.

Runs the same pipeline as the GUI (markitdown + OCR + audio + cleanup) so
conversions can be scripted from shells, CI, or AI agents:

    markdown-sidekick-cli convert book.pdf --split-chapters --quality
    markdown-sidekick-cli convert docs\\*.docx --out md\\
    markdown-sidekick-cli capabilities

Progress goes to stderr; per-file result lines go to stdout. Exit code is 0
when every file converted, 1 otherwise.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

from . import export
from .cleanup import clean_markdown
from .converter import ConversionEngine, default_output_path, explain_error
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
        "(implies --split-chapters; overrides --max-tokens)",
    )
    conv.add_argument("--no-clean", action="store_true", help="skip the cleanup pass")
    conv.add_argument("--no-front-matter", action="store_true", help="omit YAML front matter")
    conv.add_argument("--quality", action="store_true", help="print a quality report per file")
    conv.add_argument("--json", action="store_true", help="emit one JSON object per file instead of text lines")
    conv.add_argument("--anchors", action="store_true", help="insert <!-- page N --> markers in PDF conversions (citation grounding)")
    conv.add_argument("--images", action="store_true", help="extract PDF figures to an assets/ folder and link them")
    conv.add_argument("--polish", action="store_true", help="repair residual artifacts with the configured local LLM (needs ollama_endpoint + polish_model in settings)")
    conv.add_argument("--no-ocr", action="store_true", help="disable the OCR route")
    conv.add_argument("--no-audio", action="store_true", help="disable audio/video transcription")
    conv.add_argument("--whisper-model", default=None, help="whisper model size (tiny/base/small/medium)")

    sub.add_parser("capabilities", help="report which local engines are available")
    return parser


def _print_capabilities() -> int:
    from . import audio, ocr

    info = {
        "ocr": ocr.ocr_available(),
        "pdf_ocr": ocr.pdf_ocr_available(),
        "audio": audio.audio_available(),
        "settings": str(Settings.config_path()),
    }
    print(json.dumps(info, indent=2))
    return 0


def _convert(args: argparse.Namespace) -> int:
    settings = Settings.load()
    engine = ConversionEngine(
        enable_ocr=settings.enable_ocr and not args.no_ocr,
        enable_audio=settings.enable_audio and not args.no_audio,
        whisper_model=args.whisper_model or settings.whisper_model,
        mineru_endpoint=settings.mineru_endpoint,
        page_anchors=args.anchors or settings.page_anchors,
    )
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
            record["error"] = result.error
            what, fix = explain_error(result.error or "")
            record["error_hint"] = f"{what} {fix}"
            if args.json:
                print(json.dumps(record, ensure_ascii=False))
            else:
                print(f"ERROR  {path} — {result.error}")
                print(f"       {what} {fix}")
            continue

        markdown = result.markdown
        if not args.no_clean:
            markdown, stats = clean_markdown(markdown)
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

        out_dir = args.out if args.out is not None else path.parent
        if (args.images or settings.extract_images) and path.suffix.lower() == ".pdf":
            from . import figures

            asset_root = (
                (out_dir / path.stem) if (args.split_chapters or args.ai_target) else out_dir
            )
            figs = figures.extract_pdf_figures(path, asset_root / "assets")
            if figs and settings.ollama_endpoint and settings.caption_model:
                from . import polish

                for fig in figs:
                    fig.caption = (
                        polish.caption_image(
                            fig.path, settings.ollama_endpoint, settings.caption_model
                        )
                        or ""
                    )
            if figs:
                markdown = figures.insert_figure_links(markdown, figs)
                record["figures"] = len(figs)
        if args.split_chapters or args.ai_target:
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
            )
            written = [str(out_path)]
        record["written"] = written

        report = assess_markdown(markdown)
        if args.quality:
            record["quality"] = report.as_dict()
        if report.binary_noise:
            record["warning"] = "output looks like binary noise; source file may be corrupt or unsupported"

        if args.json:
            print(json.dumps(record, ensure_ascii=False))
        else:
            target = written[0] if len(written) == 1 else f"{len(written)} files in {Path(written[0]).parent}"
            print(f"ok     {path.name} [{result.engine}] -> {target}")
            if args.quality:
                print(f"       {report.summary()}")
            if report.binary_noise:
                print(f"warn   {path.name}: output looks like binary noise; source file may be corrupt or unsupported")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "capabilities":
        return _print_capabilities()
    return _convert(args)


if __name__ == "__main__":
    sys.exit(main())
