---
name: verify
description: Run Markdown Sidekick's windows and CLI to see a change working — the pywebview new UI driven over CDP, the CLI, and the frozen exe.
---

# Verifying Markdown Sidekick

Always isolate: `LOCALAPPDATA=<scratch>\appdata` (settings.json lands there) and
`MARKDOWN_SIDEKICK_LOG_DIR=<scratch>\logs`. Never build into `dist\` (the
release is built from it): use `--distpath build-webui/dist --workpath build-webui/work`.

## New UI (`app.py --new-ui`, pywebview/WebView2)

WebView2 honours an env var, so the real entry point can be driven over CDP:

```bash
WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9333" \
  LOCALAPPDATA="$S\\appdata" MARKDOWN_SIDEKICK_LOG_DIR="$S\\logs" \
  .venv/Scripts/python.exe app.py --new-ui        # run_in_background
```

Then `node .claude/skills/verify/cdp.mjs 9333 <cmd>` (Node 22+, no deps):
`eval <js>`, `shot <png>`, `click <selector> [i]` (real mouse events),
`key <key> [ctrl|shift|ctrl+shift]`, `wait <js> [ms]`, and
`drop <selector> <abs path>...` — a Chromium native file drag, so pywebview's
`pywebviewFullPath` plumbing and the bridge's drop handler really run.

- Page state is the top-level `const S` (not `window.S`): `S.files`, `S.preview`, `S.status`.
- Useful selectors: `.dropzone` / `.dropmore`, `.row`, `[data-view=raw]`,
  `[data-act=clean|copy|copyDetails|tech|help|theme]`, `#ocrSw`, `[data-style=ai]`,
  `[data-fix=retry|enable_ocr]`, `[data-tab]`.
- Native dialogs (Save As, folder picker, Add files…, confirmations) aren't in the
  page: answer them with
  `powershell -NoProfile -ExecutionPolicy Bypass -File .claude/skills/verify/dialog.ps1 [-Path <abs>] [-Button n] [-Title <part>] [-TimeoutS s]`
  right after the click that opens one (`-ExecutionPolicy Bypass`: Windows'
  default policy refuses to run a script file). It waits up to `-TimeoutS`
  (default 20) for a visible `#32770` of the Markdown Sidekick window's process,
  types `-Path` into the file-name box (it prints what the box then holds), and
  clicks button `n` (1 Save/Open/Select Folder/OK, 2 Cancel, 6 Yes, 7 No).
  With several dialogs open it takes the one accepting input (a nested "replace?"
  prompt, not the Save As behind it), or the one whose title contains `-Title`;
  exit 2 means it couldn't tell. Works for the classic Tk window's dialogs too.
- Close a window with WM_CLOSE to its own `MainWindowHandle` (PowerShell
  `PostMessage(h, 0x10, 0, 0)`), not by killing it — that exercises the real close path.
- Copy writes the user's real clipboard (`Get-Clipboard -Raw` to read it back).
- The frozen exe takes the same env vars: `build-webui/dist/MarkdownSidekick/MarkdownSidekick.exe --new-ui`.
  Check `_internal/markdown_sidekick/webui/static/*` matches the source first — a stale build is easy to miss.

## Inputs worth dropping

A Markdown file with `<script>` and a `javascript:` link (must render as text),
a corrupt `.docx` (MS-107 error page), a PNG with OCR off (MS-110 → "Turn on
OCR & retry"), a folder (MS-104), and a >400k-char book (preview cap notice;
Copy must give the full length).
