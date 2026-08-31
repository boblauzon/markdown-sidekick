# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Markdown Sidekick — a Windows desktop app (Python 3.10+, Tkinter) that converts documents/images/scanned PDFs/audio to Markdown. Wraps Microsoft `markitdown` with local OCR (RapidOCR), local transcription (faster-whisper), a post-conversion cleanup pipeline, and an MCP server. Freeware by VibeProSoft; MIT-licensed code, but **avoid GPL/AGPL dependencies** (pypdfium2 was chosen over PyMuPDF, MinerU is endpoint-only not bundled, Marker was rejected — keep it that way).

## Commands

All commands use the project venv; there is no global Python assumption.

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt   # setup (or run setup.bat)
.venv\Scripts\python.exe app.py                               # launch the GUI
.venv\Scripts\python.exe app.py --selftest                    # end-to-end pipeline check, JSON report to %TEMP%, exit 0/1
.venv\Scripts\python.exe app.py --mcp                         # MCP server over stdio (also: run_mcp.py)
.venv\Scripts\python.exe -m markdown_sidekick.cli convert x.pdf --split-chapters   # headless CLI
.venv\Scripts\python.exe -m pytest tests/ -q                  # run tests
.venv\Scripts\python.exe -m pytest tests/test_cleanup.py -k toc -q    # single test / pattern
.venv\Scripts\pyinstaller.exe MarkdownSidekick.spec --noconfirm       # build standalone exe (~4 min)
```

- `pytest` is a dev-only dependency — it is deliberately **not** in `requirements.txt`; install it into the venv if missing.
- After building, run the exe from `dist\MarkdownSidekick\`, never `build\` (the build\ copy has no runtime and fails with "Failed to load Python DLL"). Validate frozen builds with `dist\MarkdownSidekick\MarkdownSidekick.exe --selftest`.

## Architecture

`src/` layout; the core is UI-agnostic and reused by four front-ends: the Tkinter GUI (`ui.py`), the MCP server (`mcp_server.py`), the headless CLI (`cli.py`), and scripts/tests.

**AI-friendly export layer** (applies at *save/export* time, never to preview/copy): `export.py` (YAML front matter, chapter splitting on `#` headings with a book-mode filter — when ≥3 `# Chapter/Part N` headings exist only those split, because stray unfenced code comments also look like H1s — plus index.md/manifest.json), `quality.py` (artifact scanner + 0-100 score + token estimate), `figures.py` (pypdfium2 image extraction to assets/, deduped by content hash), `polish.py` (opt-in Ollama repair pass with a size guardrail — a response outside 0.7–1.3× the chunk is discarded). Tests build real PDFs via `tests/pdfgen.py` (hand-assembled PDF bytes, supports an embedded image).

The output shape is chosen in the GUI's export bar and persisted as `Settings.export_style` (`"single"` / `"chapters"` / `"ai"`) + `ai_target`. `"ai"` uses `export.split_for_ai` with a per-platform budget from `export.AI_TARGETS`; its guarantees (encoded in `tests/test_export_quality.py`) are: the budget is a **hard cap** checked *before* a boundary is passed, fences are **never** split, unbalanced fences (a real OCR artifact) disable fence tracking rather than disable splitting, and only a single indivisible fenced block may exceed the budget. `AI_TARGETS["Claude"]` deliberately aliases `DEFAULT_MAX_TOKENS` — they are one number, don't let them drift.

**Conversion routing** (`converter.py::ConversionEngine.convert_file`) — tries specialised engines in order, each wrapped in try/except that falls through to the next; `convert_file` never raises, errors land on `ConversionResult.error`:

1. image file + OCR enabled → RapidOCR (`engine="ocr"`)
2. PDF + MinerU endpoint configured → remote MinerU (`"mineru"`)
3. PDF that triages as scanned (`ocr.analyze_pdf`: low text layer + page-dominating image **or** ≥12 vector paths) → per-page OCR keeping text pages (`"ocr+text"`)
4. audio → faster-whisper (`"whisper"`)
5. everything else → markitdown (`"markitdown"`)

**Cleanup pipeline** (`cleanup.py::clean_markdown`) — ~10 independent, toggleable passes. Two invariants:

- *Conservative by design*: every heuristic favours false negatives; a pass must never delete real content. Removal is gated (min repeat counts, cluster sizes, both-signals rules) — keep that bar when adding passes.
- *Pass order matters*: chapter titles are harvested **before** the TOC (their source) is stripped; page-noise/boilerplate removal runs **before** code fencing (running headers split listings); paragraph joining runs **after** fencing (fenced code is exempt from joining); U+FFFD scrubbing runs **after** TOC stripping (the TOC detector keys on FFFD leader runs).

The passes were tuned against a real 25-book PDF-conversion corpus; `tests/test_cleanup.py` encodes those artifact shapes — run it after any cleanup change. Fence language labels come from `_guess_language` (content scoring; returns `""` plain fence when ambiguous — an honest plain fence beats a wrong label). `CleanupStats` fields must all be non-negative int counters: `total_fixes`/`changed` derive from `dataclasses.fields`, so a new pass's counter is picked up automatically but a non-counter field would poison the sum.

**Threading**: the GUI runs conversion on a worker thread; progress callbacks (`on_progress`, `on_subprogress`) must stay thread-safe. Cleanup + quality assessment are precomputed per file inside the worker's `on_progress` (selecting a big book must never stall the Tk thread — misses show a watch cursor); Tk variables (Clean/OCR toggles) are read on the main thread *before* the worker starts and passed in, never read from the worker. The MCP server serializes conversions with a lock because the OCR/whisper model singletons and the stdout redirect are not thread-safe.

**MCP server** (`mcp_server.py`): stdout is reserved for JSON-RPC — logging goes to stderr and conversion runs inside `redirect_stdout(sys.stderr)`. The `FASTMCP_*` env vars are set **before** importing fastmcp (`FASTMCP_CHECK_FOR_UPDATES` must be `off`, not `false` — it's a Literal and `false` crashes the import).

**Settings** (`settings.py`): dataclass persisted to `%LOCALAPPDATA%\MarkdownSidekick\settings.json`; load is fully tolerant of junk — every field is validated in `normalize()` (enum-like strings get membership checks, e.g. `ai_target` against `export.AI_TARGETS`). When renaming/removing a persisted key, add a migration in `load()` (see the legacy `split_chapters` → `export_style` mapping) — unknown keys are silently filtered, so without one the user's preference is lost. `settings.py` imports `export.py` at module level; that's fine only because `export.py` is stdlib-only — keep it that way. Whisper models are materialised as real file copies under the same dir (`local_dir=`, not the HF symlink cache — Windows symlink privilege issue).

**Startup time**: RapidOCR, faster-whisper, and fastmcp are lazy-imported. Don't add module-level imports of heavy libraries to anything `ui.py` pulls in at startup.

**Preview** (`mdrender.py`): a deliberate CommonMark *subset* renderer for the Tk Text widget — underscore emphasis is intentionally unsupported (snake_case would misrender), and links inside `**bold**` are not parsed. Raw Markdown remains the source of truth for copy/save. The preview *displays* at most `ui.PREVIEW_MAX_CHARS` (rendering costs ~250ms/MB on messy text); copy/save always use the full document.

## Packaging touchpoints

Adding a data file or a new heavy dependency usually means touching three places: `pyproject.toml` (package-data / dependencies), `MarkdownSidekick.spec` (datas / collect_all), and `app.py --selftest` if it's pipeline-critical. `USERGUIDE.md` ships inside the package (loaded via `importlib.resources`) — keep its links unwrapped by bold.
