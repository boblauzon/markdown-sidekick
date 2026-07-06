# Markdown Sidekick — User Guide

Convert documents, images, scanned PDFs and audio into clean Markdown — entirely
on your own computer. Nothing is uploaded anywhere; every engine runs locally.

Markdown Sidekick is **freeware** by VibeProSoft. If it saves you time, you can
support development at [ko-fi.com/roblauzon](https://ko-fi.com/roblauzon) ☕

---

## Quick Start

1. **Add files** — drag & drop them onto the file list, or click *Add files…*
2. **Click Convert all** — each file's row shows which engine handled it
3. **Click a file** to see its Markdown in the preview pane
4. **Copy** it to the clipboard, or **Save as…** / **Save all…** to `.md` files

That's the whole workflow. Everything below is detail you can read when you need it.

---

## The Main Window

**Files panel (left)**

- *Add files…* / *Remove* / *Clear* manage the conversion list
- The **Engine** column shows how each file was converted:

| Badge | Meaning |
| ----- | ------- |
| markitdown | Digital document, converted directly (fastest) |
| ocr | Image file read by local OCR |
| ocr+text | PDF with scanned/vector pages: OCR where needed, text kept elsewhere |
| whisper | Audio transcribed by the local Whisper model |
| mineru | Converted by your optional MinerU server |
| error | Conversion failed — click the row to see why |

- **OCR images & scanned PDFs** — untick to force everything through markitdown

**Preview panel (right)**

- **Rendered** — shows styled Markdown (headings, bold, code boxes, tables).
  Untick it to see the raw Markdown text exactly as it will be saved.
- **Clean output** — applies the cleanup pass (see below). Copy and Save always
  match whatever this toggle is set to.
- **Copy** / **Save as…** act on the currently selected file.

**Footer**

- Progress bar and status line (per-page OCR progress, per-second transcription
  progress, cleanup summaries)
- **Save all…** writes every converted file to a folder of your choice —
  or straight to your default output folder if you set one in Settings
- **Convert all** starts the batch

---

## Supported Formats

| Category | Formats |
| -------- | ------- |
| Documents | PDF, DOCX, PPTX, XLSX, XLS, EPUB |
| Web / data | HTML, CSV, JSON, XML, TXT, MD |
| Images | PNG, JPG, GIF, BMP, TIFF, WEBP |
| Audio | MP3, WAV, M4A, FLAC, OGG |
| Archives | ZIP (contents converted recursively) |

---

## Clean Output — what it actually does

When **Clean output** is on (the default), the raw conversion is tidied:

- **Page noise removed** — leaked page numbers and repeating running headers
  from PDFs are stripped (conservatively: a lone year in prose is kept)
- **Scrambled contents tables removed** — multi-column Table-of-Contents and
  index pages that arrive as broken tables are dropped; *real* data tables are
  detected and preserved
- **Code listings fenced** — detected code is wrapped in ```` ```python ````
  blocks so AI tools and editors treat it as code
- **Blank-line runs collapsed**

The status bar reports what was changed, e.g.
*"Cleanup: 276 header/page-number line(s) removed, 235 code block(s) fenced."*
Untick **Clean output** at any time to see or save the unmodified conversion.

---

## OCR — images and scanned PDFs

Some content has no machine-readable text: photos of documents, scanned books,
and PDFs whose text was outlined into vector shapes ("print to PDF" exports
often do this). Markdown Sidekick detects these automatically:

- **Image files** are always OCR'd when OCR is enabled
- **PDFs** are inspected page by page; a page with almost no text layer but
  visible content (a page-filling scan image, or many vector shapes) is
  rendered and OCR'd, while normal text pages keep their original text

Notes:

- OCR runs on the CPU at roughly **5–10 seconds per page** — the status bar
  shows page-by-page progress. It is a one-time cost; keep the saved `.md`.
- The OCR engine (RapidOCR) ships **inside** the app. No downloads, no cloud.
- Everything stays on your machine.

---

## Audio Transcription

Drop in an MP3, WAV, M4A, FLAC or OGG file and convert. You get a timestamped
transcript with the detected language:

```
[00:00:00.000 --> 00:00:05.339] Hello, this is a test...
```

Notes:

- **No ffmpeg needed** — audio decoding ships inside the app
- The Whisper speech model (~150 MB for *base*) downloads **once**, on your
  first audio conversion, then lives in your local app-data folder. That first
  run needs an internet connection; after that it's fully offline.
- Model sizes (Settings → *Whisper model*): `tiny` (fastest) → `base`
  (default, good balance) → `small` / `medium` (more accurate, slower —
  each step roughly doubles time and download size)

---

## Settings (⚙ in the header)

| Setting | What it does |
| ------- | ------------ |
| OCR images & scanned PDFs | Master switch for the OCR engine |
| Transcribe audio files | Master switch for audio transcription |
| Whisper model | Speech model size (see above) |
| MinerU endpoint URL | Optional high-fidelity PDF server (blank = off) |
| Default output folder | Save all… writes here without asking |
| Clean output / Rendered preview | Default states for the preview toggles |

Settings persist between sessions in `settings.json` (see *Where files live*).

**MinerU (optional, advanced):** if you run a local
[MinerU](https://github.com/opendatalab/mineru) server, enter its URL (e.g.
`http://127.0.0.1:2364`) and PDFs will be sent to it for layout-aware,
high-fidelity conversion. If the server is unreachable, Markdown Sidekick
falls back to its built-in pipeline automatically.

---

## Using Markdown Sidekick from AI tools (MCP)

The app includes an MCP server so Claude Desktop, Cursor and VS Code can
convert local files mid-conversation using this same pipeline.

**The easy way:** open **⚙ Settings → 📋 Copy AI setup prompt**, then paste it
into Claude, Cursor, or any AI assistant. The prompt contains this install's
exact launch command — the assistant does the configuration for you.

**Manual setup** (if you prefer): the standalone app serves MCP via
`MarkdownSidekick.exe --mcp`; a source checkout uses the commands below.

**Claude Desktop** — add to `%APPDATA%\Claude\claude_desktop_config.json`:

```
{
  "mcpServers": {
    "markdown-sidekick": {
      "command": "E:\\Apps\\Markdown_Sidekick\\.venv\\Scripts\\python.exe",
      "args": ["E:\\Apps\\Markdown_Sidekick\\run_mcp.py"]
    }
  }
}
```

**Cursor / VS Code** — add an MCP server with transport *stdio*, command
`…\.venv\Scripts\python.exe`, argument `…\run_mcp.py`, then restart.

Tools exposed:

- `convert_local_file(file_path, clean=true)` — returns the Markdown
- `list_capabilities()` — reports which engines are available

---

## Where files live

| What | Where |
| ---- | ----- |
| Settings | `%LOCALAPPDATA%\MarkdownSidekick\settings.json` |
| Whisper models | `%LOCALAPPDATA%\MarkdownSidekick\models\` |
| Your output | Wherever you save it — nothing else is written |

Deleting the `MarkdownSidekick` app-data folder resets the app completely.

---

## Troubleshooting

**"Failed to load Python DLL … build\…" when launching the exe**
You launched the exe from the `build\` folder of a source checkout. Only
`dist\MarkdownSidekick\MarkdownSidekick.exe` is the complete app — `build\` is
temporary scaffolding. When sharing the app, zip the whole
`dist\MarkdownSidekick` folder, not just the exe.

**A PDF converts to (almost) empty output**
Turn **OCR images & scanned PDFs** on and re-convert — the PDF likely has no
text layer. Scanned and vector-text PDFs are detected and OCR'd automatically.

**OCR feels slow**
That's expected on CPU (~5–10 s/page). Progress is shown per page. Convert big
scans once and keep the `.md`.

**First audio file takes a long time**
The speech model downloads on first use (~150 MB). Later runs are fast and
offline. If the download was interrupted, just convert again — it resumes
with a completeness check.

**Antivirus flags the exe**
A false positive common to PyInstaller apps. The app makes no network
connections except the one-time Whisper model download and your optional
MinerU endpoint. Build from source if your policy requires it.

**Audio file won't transcribe**
Check Settings → *Transcribe audio files* is on. A silent file produces
"(No speech detected.)" rather than an error.

---

## Technical Reference

**Architecture** — each engine is a separate module behind one router:

| Module | Role |
| ------ | ---- |
| converter.py | Routing: picks markitdown / OCR / whisper / MinerU per file |
| ocr.py | RapidOCR + pypdfium2: page triage, rendering, recognition |
| audio.py | faster-whisper transcription, model management |
| cleanup.py | Noise stripping, TOC removal, code fencing |
| mdrender.py | The rendered preview (pure Tkinter, no browser) |
| settings.py | Persisted JSON settings |
| mcp_server.py | FastMCP stdio server for AI tools |
| ui.py | The Tkinter application |

**PDF routing thresholds** — a page is treated as scanned when it has fewer
than 24 characters of visible text AND visual content (raster image covering
≥ 45% of the page, or ≥ 12 vector path objects). A PDF switches to the OCR
path when ≥ 15% of its pages look scanned.

**Engines and licensing** — all engines are permissively licensed and bundled:

| Engine | Used for | License |
| ------ | -------- | ------- |
| Microsoft markitdown | Digital documents | MIT |
| RapidOCR (ONNX) | OCR | Apache-2.0 |
| pypdfium2 / PDFium | PDF rendering & triage | Apache/BSD |
| faster-whisper + PyAV | Audio transcription | MIT/BSD |
| MinerU (optional, external) | High-fidelity PDF | Apache-2.0-based |

**Output** — always UTF-8. Same-named files saved to one folder are
auto-suffixed (`report.md`, `report-2.md`) so nothing is overwritten.

---

## Support this project ☕

Markdown Sidekick is free software. If it's useful to you, a coffee keeps
development going:

[Support on Ko-fi — ko-fi.com/roblauzon](https://ko-fi.com/roblauzon)

Bug reports and feature ideas are just as valuable — thank you!

*Markdown Sidekick v1.0.0 · a VibeProSoft freeware project*
