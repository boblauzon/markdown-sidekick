# Markdown Sidekick — User Guide

Convert documents, images, scanned PDFs and audio into clean Markdown — entirely
on your own computer. Nothing is uploaded anywhere; every engine runs locally.

Markdown Sidekick is **freeware** by VibeProSoft. If it saves you time, you can
support development at [ko-fi.com/roblauzon](https://ko-fi.com/roblauzon) ☕

---

## Quick Start

1. **Add files** — drag & drop them onto the file list, or click *Add files…*
   Conversion starts **automatically**; each row shows which engine handled it.
2. **Click a file** to see its Markdown in the preview pane.
3. Pick an **Output** shape next to the Save button — *One Markdown file*,
   *Chapter files (book folder)*, or *AI-sized sections* (with an **Optimize
   for** picker: Claude, ChatGPT, Gemini, Gemini Notebook, Local LLM).
4. **💾 Save Markdown…** — one file gets a save dialog, several get a folder.
   (Or **Copy** the previewed file straight to the clipboard.)

That's the whole workflow: drop, then save. Everything below is detail you
can read when you need it.

---

## The Main Window

**Files panel (left)**

- *Add files…* / *Remove* / *Clear* manage the conversion list — added files
  convert immediately, and you can keep dropping more while a batch runs
- The **Engine** column shows how each file was converted:

| Badge | Meaning |
| ----- | ------- |
| pdflayout | Digital PDF read by its page layout: columns in order, printer's marks dropped, bookmarks as headings |
| markitdown | Other digital documents (Word, PowerPoint, HTML, …), converted directly |
| ocr | Image file read by local OCR |
| ocr+text | PDF with scanned/vector pages: OCR where needed, text kept elsewhere |
| whisper | Audio transcribed by the local Whisper model |
| mineru | Converted by your optional MinerU server |
| error | Conversion failed — click the row for a plain-English explanation, what to try, and a **↻ Retry** button (also on right-click) |

- **OCR images & scanned PDFs** — untick to skip OCR (text-layer extraction only)

**Preview panel (right)**

- **Rendered** — shows styled Markdown (headings, bold, code boxes, tables).
  Untick it to see the raw Markdown text exactly as it will be saved.
- **Clean output** — applies the cleanup pass (see below). Copy and Save always
  match whatever this toggle is set to.
- **Copy** puts the currently selected file's Markdown on the clipboard.
- Very large documents preview their beginning (a notice says so) to keep the
  app instant — **Copy and Save always use the complete document**.

**Footer**

- Progress bar and status line (per-page OCR progress, per-second transcription
  progress, batch results); the selected file's **cleanup summary and quality
  score** appear beside the preview, next to *Copy*
- The **export bar** sits next to the Save button:
  - **Output** — *One Markdown file* (one .md per source), *Chapter files*
    (each book becomes a folder of per-chapter files + index.md +
    manifest.json), or *AI-sized sections* (parts guaranteed to fit an AI's
    context window, even for documents with no headings; short chapters are
    packed together, so a book of 130 one-page topics becomes a handful of
    parts rather than 130 files)
  - **Optimize for** — sizes AI sections for Claude (~30k tokens), ChatGPT
    (~12k), Gemini (~60k), or a small Local LLM (~4k). **Gemini Notebook**
    (formerly NotebookLM) instead writes a folder of sources ready to upload
    — see *Exporting for Gemini Notebook* below
  - **💾 Save Markdown…** — a single file gets a save dialog; batches and
    split modes ask for a folder (pre-filled with your default output folder)
    and offer to open it afterwards

Changing **Settings** offers to re-convert the files you already loaded, so
results always match the current configuration.

**Keyboard shortcuts**

| Keys | Action |
| ---- | ------ |
| Ctrl+O | Add files |
| Delete | Remove the selected file |
| Ctrl+S | Save Markdown |
| Ctrl+Shift+C | Copy the previewed Markdown |
| Esc | Close the Settings or Help window |

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

- **Printer's marks removed** — InDesign slug lines ("…_001-077.indd 1
  3/23/17"), job tickets ("Job No: … Title: …"), DTP stamps and "(RAY)(Text)"
  operator tags from print-ready PDFs are stripped (the PDF layout reader
  already leaves them out; this catches older or non-PDF conversions)
- **Doubled "shadow" text repaired** — drop-shadow type extracted twice
  ("DDrraawwiinngg") is restored ("Drawing"); ordinary words with repeated
  letters are never touched
- **Characters normalized** — PDF ligatures (ﬁ → fi, ﬂ → fl) are decomposed so
  text is searchable; soft hyphens and no-break spaces are fixed; runs of the
  � replacement character are scrubbed
- **Page noise removed** — leaked page numbers (arabic *and* roman), footers,
  and repeating running headers from PDFs are stripped (conservatively: a lone
  year in prose is kept)
- **Contents tables removed** — Table-of-Contents / index pages are dropped
  whether they arrive as broken tables, dot-leader lines ("Title ..... 26"), or
  "Section • 26" entries; *real* data tables are detected and preserved
- **Chapter headings restored** — book chapter titles harvested from the TOC
  become proper `# Chapter N: Title` headings, and their per-page running
  headers are deleted
- **Publisher boilerplate removed** — blocks repeated verbatim throughout the
  document (e.g. per-chapter QR-code / "unlock your book" ads) are dropped
- **Code listings fenced** — detected code is wrapped in fenced blocks with the
  language guessed from the content (python, go, csharp, cpp, ruby, kotlin,
  java, javascript — or a plain fence when ambiguous); fragmented fences are
  merged, double-spaced listings tightened, and wrong labels re-guessed
- **Bullets normalized** — "•" bullets become Markdown "- " lists, and lists
  the PDF sheared apart (lone "•" lines separated from their text) are re-paired
- **Hard-wrapped prose joined** — lines the PDF broke mid-sentence are joined
  back into paragraphs, and words split with a line-end hyphen are rejoined
- **Blank-line runs collapsed**

The line beside the preview reports what was changed, e.g.
*"Cleaned (276 fixes, 1,824 chars normalized)"*.
Untick **Clean output** at any time to see or save the unmodified conversion.

---

## How PDFs are read

Digital PDFs are read by their **page layout**, not as a stream of text:

- **Columns in order** — multi-column pages are read one column at a time,
  top to bottom, so paragraphs never come out sliced across columns into
  tables, and margin notes land after the text they annotate instead of
  inside its sentences. Genuine number tables stay Markdown tables.
- **Printer's marks dropped** — print-ready PDFs carry job tickets, crop-mark
  labels and even the facing page of a spread *outside the trimmed page*;
  anything beyond the PDF's trim box is left out.
- **Doubled type repaired** — drop-shadow and fake-bold lettering drawn twice
  is read once.
- **Bookmarks become headings** — the PDF's own table of contents (its
  bookmarks) turns chapter and section titles into `#`/`##` headings, which
  is what *Chapter files* export splits on. Cover, title, copyright and
  contents bookmarks are treated as front matter.
- **Code stays code** — text set in a fixed-width font becomes a fenced code
  block with its indentation intact.
- **Words stay whole** — words hyphenated across a line break are rejoined
  (real compounds like "long-term" keep their hyphen), and ligatures a font
  failed to label ("di?erent") are restored.

If a particular PDF reads worse this way, untick **Column-aware PDF reading**
in Settings → Conversion to use the simpler text-stream reader instead.

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

- OCR runs on your **graphics card** when you have one (any modern AMD,
  NVIDIA, or Intel GPU — under a second per page) and otherwise on the CPU
  at roughly **4–8 seconds per page** — the status bar
  shows page-by-page progress. It is a one-time cost; keep the saved `.md`.
- The OCR engine (RapidOCR) ships **inside** the app. No downloads, no cloud.
- Everything stays on your machine.

---

## Audio Transcription

Drop in an MP3, WAV, M4A, FLAC or OGG file — or a **video** (MP4, MKV, MOV,
WEBM, AVI; the audio track is transcribed) — and convert. You get a transcript
grouped into timestamped paragraphs with the detected language:

```
**[00:00]** Hello, this is a test. The transcript flows in readable
paragraphs, with a new timestamp at each pause in speech...
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

Settings are grouped into four tabs — **Conversion**, **Output**,
**Local AI**, and **Diagnostics**:

| Tab | Setting | What it does |
| --- | ------- | ------------ |
| Conversion | OCR images & scanned PDFs | Master switch for the OCR engine |
| Conversion | OCR device | *auto* uses your GPU (DirectML) when present — about 5× faster; *cpu*/*gpu* force one |
| Conversion | Column-aware PDF reading | Read PDFs by page layout (see *How PDFs are read*); untick for the simpler text-stream reader |
| Conversion | Transcribe audio files | Master switch for audio/video transcription |
| Conversion | Whisper model | Speech model size (see above) |
| Conversion | MinerU endpoint URL | Optional high-fidelity PDF server (blank = off) |
| Output | Default output folder | Pre-selected in every save dialog |
| Output | Clean output / Rendered preview | Default states for the preview toggles |
| Output | Front matter / source header | Saved files start with title/source/date/token metadata (YAML), or a one-line book/part header for Gemini Notebook |
| Output | Page anchors | PDF conversions keep `<!-- page N -->` markers for citations |
| Output | Extract PDF figures | On by default: images of at least 120 px land in an `images/` folder, linked right where they appear in the text |
| Local AI | Endpoint + Detect | **Detect** probes your machine for a running local AI — Ollama, LM Studio, Jan, or any OpenAI-compatible server — fills the model pickers with what's installed, and tells you if it's running but has no models loaded yet |
| Local AI | Polish / caption / summary model | Optional local-LLM passes: artifact repair, figure alt-text, and a 2–3 sentence document summary written into the saved file's front matter (blank = that pass is off) |
| Diagnostics | Record a detailed trace | Debug mode: logs everything each conversion did (see *Debug mode and diagnostic reports*) |
| Diagnostics | Also keep raw and cleaned Markdown | In debug mode, saves both versions of every conversion for quality comparison |
| Diagnostics | Create diagnostic report… / Open log folder | Zip the logs for a bug report, or browse them |

Everything on the Local AI tab talks only to your own machine — detection
probes localhost and never calls out to the internet.

**AI-friendly export, in short:** big single files overflow AI context windows.
With *Chapter files* or *AI-sized sections* selected in the export bar, a
500-page book becomes a folder of
chapter files split at the book's own chapters (`00-front-matter.md`,
`01-…`, each with front matter saying which book, author and part it is),
an `images/` folder, an `index.md`, and a `manifest.json` (titles, token and
image counts) — ready for Claude, ChatGPT, Gemini, or any RAG pipeline. A **quality score** and estimated token count for the selected
file appear beside the preview.

**Exporting for Gemini Notebook.** Gemini Notebook (formerly NotebookLM)
doesn't paste files into a chat — every uploaded file becomes a *source* it
searches and cites. Choose *AI-sized sections* → **Optimize for: Gemini
Notebook** and the book folder is shaped for that:

- **One source per chapter**, so you can tick a single chapter to scope a
  chat, study guide or Audio Overview to it. Chapters are packed together
  (evenly, as few per source as possible) only when a book would otherwise
  take more than 25 sources — half of a free notebook's 50, leaving room for
  a second book.
- **Every source fits the 500,000-word limit**, whatever the language:
  anything longer is split.
- **Filenames start with the book's short title**
  (`pragmatic-programmer-03-….md`), so sources from several books stay
  distinguishable in the notebook's source list.
- **A one-line header instead of YAML** — the book, author and part. The
  first source also carries the AI summary and the list of parts. (YAML
  would be read as text, and its conversion date could be quoted as the
  book's date.) Settings → Output → *Front matter / source header* turns it
  off.
- **Image links become their captions** (`[Figure 3.1]`) — a notebook can't
  open links to local files. Upload a key figure from `images/` as its own
  source if you need it (each image counts as a source).
- **No index.md or manifest.json** — select every file in the folder and
  drop them into the notebook.

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

- `convert_local_file(file_path, clean, save_to, max_chars)` — returns the
  Markdown (or writes it to `save_to` and returns a short quality summary)
- `convert_outline(file_path)` — document structure + per-section token counts,
  without the content (the conversion is cached for follow-up reads)
- `convert_section(file_path, section_index)` — one chapter at a time
- `convert_url(url)` — download and convert a web page or remote PDF
- `list_capabilities()` — reports which engines are available

There is also a **command line** for scripts and automation:

```
markdown-sidekick-cli convert book.pdf --split-chapters --quality
markdown-sidekick-cli convert book.pdf --ai-target Claude
MarkdownSidekick.exe --cli convert book.pdf --split-chapters   (standalone app)
```

`--ai-target` (Claude / ChatGPT / Gemini / "Gemini Notebook" / "Local LLM")
writes the same AI-sized book folders as the export bar — every part fits
that platform's context budget (for Gemini Notebook: one upload-ready source
per chapter). The MCP tools take a matching `max_tokens` argument.
`--images` / `--no-images` override the figure-extraction setting, and
`--no-layout` reads PDFs with the simpler text-stream reader. Failures print
their error code, the next step and the log reference; `--debug` traces the
run and `diagnostics` summarises the logs (see *Debug mode and diagnostic
reports*).

---

## Where files live

| What | Where |
| ---- | ----- |
| Settings | `%LOCALAPPDATA%\MarkdownSidekick\settings.json` |
| Whisper models | `%LOCALAPPDATA%\MarkdownSidekick\models\` |
| Error log (always on) | `%LOCALAPPDATA%\MarkdownSidekick\logs\errors.jsonl` |
| Debug traces (debug mode only) | `%LOCALAPPDATA%\MarkdownSidekick\logs\sessions\` |
| Your output | Wherever you save it |

Deleting the `MarkdownSidekick` app-data folder resets the app completely.

---

## Troubleshooting

An error message in the app always carries a code (`MS-…`) and its next
step — see *Error codes* below for the full list.

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

## Error codes — what went wrong and what to do

Every problem Markdown Sidekick reports follows the same pattern:

1. **What happened**, in plain words.
2. **What to do next** — and, when the app can do it for you, a button that
   does it (*Turn on OCR & retry*, *Choose another folder…*, *Open Local AI
   settings*).
3. **An error code and a reference**, e.g. `Error MS-102 · Ref 7F3A2C`. The
   code says what kind of problem it is (the tables below); the reference
   points at the exact entry in the error log, which holds the technical
   details.

Where you see it:

- **A file that failed to convert** shows its message in the preview, with
  the fix button, *Retry* and *Copy details* underneath. Its row reads
  `error MS-…`.
- **A file that converted, but not as well as it could have** (a better
  reader failed and a simpler one took over) is marked ⚠ in the list and
  explained in a bar above the progress line when you select it.
- **Something that stops what you were doing** (a save that fails) opens a
  small dialog with the fix as its default button.
- **Something that doesn't stop you** (the AI summary was skipped, your
  settings file was reset) appears in the same bar, which you can dismiss.

*Copy details* puts everything a bug report needs on the clipboard: the
code, the reference, the technical message, your app version and system.

All errors are **recorded automatically** in
`%LOCALAPPDATA%\MarkdownSidekick\logs\errors.jsonl` — no setting needed.
The log stays on your PC, holds the last few megabytes, and never contains
your documents' text (file names and paths, yes).

**Converting a file**

| Code | What happened | What to do | One-click fix |
| ---- | ------------- | ---------- | ------------- |
| MS-101 | This document is password-protected or encrypted. | Remove the protection (open it and print or export it to a new file), then retry. | Retry |
| MS-102 | The file is locked or in use by another program. | Close the program that has it open (often a PDF reader or Office), then retry. | Retry |
| MS-103 | The file can't be found — it may have been moved, renamed, or deleted. | Add the file again from its current location. | Remove from list |
| MS-104 | That's a folder, not a file. | Add the files inside it instead. | Add files… |
| MS-105 | No conversion engine understands this file type. | Export the content as PDF, DOCX, HTML, or another supported format, then add that file. | — |
| MS-106 | The conversion finished but produced no text. | The file may be corrupt, image-only, or a binary format with the wrong extension. Re-export it in a supported format, then add it again. | — |
| MS-107 | The file appears to be corrupt or incomplete. | Re-download or re-export the file, then retry. | Retry |
| MS-108 | The file is too large to convert with the memory available. | Close other programs (or split the document into parts), then retry. | Retry |
| MS-109 | The audio/video stream couldn't be decoded. | Convert it to a common format (MP3, WAV, or MP4), then add that file. | — |
| MS-110 | This file has no text layer — it's a scan or an image — and OCR is turned off. | Turn on OCR so the text can be read from the images. | Turn on OCR & retry |
| MS-111 | The speech-recognition model couldn't be downloaded. | Connect to the internet once so the model can download (it's kept afterwards), then retry. | Retry |
| MS-112 | The transcription engine failed on this file. | Choose a smaller Whisper model in Settings → Conversion, then retry. | Open Conversion settings |
| MS-113 | The OCR engine failed. | Switch OCR to the CPU — the graphics driver is the usual cause — and retry. | Use CPU for OCR & retry |
| MS-114 | The MinerU server didn't return a result, so the PDF was converted locally. *(warning)* | Check the server is running at the endpoint in Settings → Conversion, or clear the endpoint to stop using it. | Open Conversion settings |
| MS-115 | Audio/video transcription is turned off. | Turn on transcription to convert this file. | Turn on transcription & retry |
| MS-120 | The column-aware PDF reader failed, so the basic reader was used — columns and tables may be out of order. *(warning)* | Retry. If it happens again, report it with this error's reference. | Retry |
| MS-121 | OCR failed on this PDF's scanned pages, so only its text layer was kept. *(warning)* | Switch OCR to the CPU — the graphics driver is the usual cause — and retry. | Use CPU for OCR & retry |
| MS-122 | OCR failed on this image, so only basic image information was extracted. *(warning)* | Switch OCR to the CPU — the graphics driver is the usual cause — and retry. | Use CPU for OCR & retry |
| MS-123 | Transcription failed, so only basic file information was extracted. *(warning)* | Choose a smaller Whisper model in Settings → Conversion, then retry. | Open Conversion settings |
| MS-124 | The page-by-page PDF reader failed, so this PDF has no page anchors. *(warning)* | Retry. If it happens again, report it with this error's reference. | Retry |
| MS-125 | This file couldn't be opened as a PDF (it may be damaged, or not a PDF at all), so only its raw text was converted. *(warning)* | Re-download or re-export the PDF, then retry. | Retry |
| MS-130 | The address couldn't be downloaded. | Check the address and your internet connection, then retry. | Retry |
| MS-131 | That link can't be converted: only http/https downloads up to 50 MB are supported. | Download the file yourself and convert the local copy. | — |
| MS-132 | There is no section with that number. | Get the section list from convert_outline and use one of its indexes (with the same max_tokens). | — |
| MS-199 | The conversion engine reported an unexpected error. | Retry. If it keeps failing, report it — the technical details say why. | Retry |

**Saving**

| Code | What happened | What to do | One-click fix |
| ---- | ------------- | ---------- | ------------- |
| MS-201 | Markdown Sidekick isn't allowed to write to that folder. | Choose a different folder, such as Documents. | Choose another folder… |
| MS-202 | The disk is full. | Free up space, or choose a folder on another drive. | Choose another folder… |
| MS-203 | The save location's path is too long for Windows. | Choose a folder with a shorter path, such as C:\Markdown. | Choose another folder… |
| MS-204 | A file being saved is open in another program. | Close the program that has it open (often a Markdown editor), then save again. | Save again |
| MS-299 | The Markdown couldn't be saved. | Try saving to a different folder. If it keeps failing, report it with this error's reference. | Choose another folder… |

**Local AI (optional extras — the save itself still succeeds)**

| Code | What happened | What to do | One-click fix |
| ---- | ------------- | ---------- | ------------- |
| MS-301 | The local AI didn't answer, so its step (summary, captions or polish) was skipped. *(warning)* | Start your local AI app (Ollama, LM Studio…), or clear the model in Settings → Local AI to stop using it. | Open Local AI settings |
| MS-302 | The local AI doesn't have the chosen model, so its step was skipped. *(warning)* | Download the model (e.g. ollama pull llama3.2) or pick an installed one in Settings → Local AI. | Open Local AI settings |
| MS-303 | The local AI took too long, so its step was skipped. *(warning)* | Pick a smaller or faster model in Settings → Local AI. | Open Local AI settings |
| MS-304 | The AI summary didn't pass the quality checks, so it was left out. *(note)* | Nothing needs fixing. For better summaries, try another summary model in Settings → Local AI. | Open Local AI settings |
| MS-399 | The local AI step failed and was skipped. *(warning)* | Check the endpoint and model in Settings → Local AI. | Open Local AI settings |

**Figures**

| Code | What happened | What to do | One-click fix |
| ---- | ------------- | ---------- | ------------- |
| MS-401 | Figures couldn't be extracted, so the Markdown was saved without them. *(warning)* | Save again, or turn off figure extraction in Settings → Output. | Open Output settings |
| MS-402 | Some figures couldn't be saved; the rest of the document is complete. *(warning)* | If the missing figures matter, report the file with this error's reference. | — |

**Settings**

| Code | What happened | What to do | One-click fix |
| ---- | ------------- | ---------- | ------------- |
| MS-501 | Your settings file couldn't be read, so the defaults were loaded. *(warning)* | The old file was kept as settings.json.bad — check your choices in Settings. | Open Settings |
| MS-502 | Your settings couldn't be saved. | Make sure the folder %LOCALAPPDATA%\MarkdownSidekick isn't read-only or full, then save again. | — |

**Clipboard, folders, reports, help**

| Code | What happened | What to do | One-click fix |
| ---- | ------------- | ---------- | ------------- |
| MS-601 | The clipboard is busy — another program is using it. *(warning)* | Wait a moment, then copy again. | Copy again |
| MS-602 | Windows couldn't open that folder. *(warning)* | Open it yourself — its path is in the details. | — |
| MS-603 | The diagnostic report couldn't be created. | Choose another folder for the report, or open the log folder and send the files from there. | Open log folder |
| MS-604 | The built-in user guide couldn't be loaded — a file is missing from this installation. | Reinstall Markdown Sidekick to restore it. | — |

**Unexpected errors**

| Code | What happened | What to do | One-click fix |
| ---- | ------------- | ---------- | ------------- |
| MS-900 | Markdown Sidekick hit an unexpected error and had to stop. | Restart the app. The details are in the error log — send them to support. | Open log folder |
| MS-901 | Something went wrong inside Markdown Sidekick, but it recovered. | Carry on. If anything looks wrong, restart the app and report this error's reference. | Open log folder |
| MS-902 | Conversion stopped unexpectedly. | The files that didn't finish are marked as failed — retry them, or restart the app. | Retry failed files |
| MS-903 | The preview couldn't be drawn for this file. *(warning)* | Show the raw Markdown instead — Copy and Save still work. | Show raw Markdown |

---

## Debug mode and diagnostic reports

The error log records what *failed*. **Debug mode** records everything the
app *did* — useful when output looks wrong, a conversion is slow, or you are
asked for more detail on a bug report.

Turn it on in **Settings → Diagnostics → Record a detailed trace** (the
title bar shows *DEBUG MODE* while it is on), or for one run with
`MarkdownSidekick.exe --debug`. Each run then writes a folder under
`%LOCALAPPDATA%\MarkdownSidekick\logs\sessions\` with:

| File | What it holds |
| ---- | ------------- |
| `events.jsonl` | A timeline: your system and settings, every file converted, which reader was tried and why it was or wasn't used, time per page, each cleanup pass (how long it took, how many lines it removed, samples of them), quality scores, saves, local-AI calls, anything the window did that took over a second |
| `snapshots\` | Each conversion's raw and cleaned Markdown side by side (only with *Also keep each conversion's raw and cleaned Markdown* ticked) |
| `faulthandler.log` | Stack dumps if the app crashed hard or froze for 20 seconds |

**Create diagnostic report…** (same tab) zips the error log, the most recent
debug sessions, your settings and a readable `summary.md` into one file to
attach to a bug report. Read the summary yourself first if you like: it
lists problems, then the slowest steps, then quality findings.

Privacy: nothing is ever uploaded. Traces contain file names and paths; the
snapshots contain your documents' text, so untick that option (or leave
snapshots out with `diagnostics --no-snapshots`) before sharing a report
about a confidential document. The 15 most recent sessions are kept; older
ones are deleted automatically. Debug mode slows conversion very slightly.

From the command line:

```
markdown-sidekick-cli convert book.pdf --debug      (trace this run)
markdown-sidekick-cli diagnostics                   (print the summary)
markdown-sidekick-cli diagnostics --report r.zip    (write the shareable zip)
```

---

## Technical Reference

**Architecture** — each engine is a separate module behind one router:

| Module | Role |
| ------ | ---- |
| converter.py | Routing: picks the PDF layout reader / markitdown / OCR / whisper / MinerU per file |
| pdflayout.py | Digital-PDF reader: trim-box clipping, column order, bookmark headings |
| figures.py | Figure extraction to images/ and in-place linking |
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
| Microsoft markitdown | Digital documents (non-PDF) | MIT |
| RapidOCR (ONNX) | OCR | Apache-2.0 |
| pypdfium2 / PDFium | PDF layout reading, rendering, triage, figures | Apache/BSD |
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
