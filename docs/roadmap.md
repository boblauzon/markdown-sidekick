# Roadmap

**Updated:** 2026-11-10 · **Branch:** `claude/ui-webview` (PR #6), stacked on the 1.3.0 release on `claude/v1.4-library-batch` (PR #5).

This is the working list: what's being built, in order, and what waits on something outside the code (a decision, an account, real users). Each item lands with a `/verify` run against the real app and a `/code-review`.

## Now (2026-10-30), in order

| # | Item | Source | Status |
|---|---|---|---|
| 1 | **New window polish.** Collapse repeated status-line notes; keep the notes when some files fail; show debug mode in the title; files removed or cleared while a batch runs are skipped, not converted. | verify findings 2026-10-29/30 | Done (`b51a7b4`) |
| 2 | **Installer, phase 1.** Per-user installs only; running copies (including `--mcp`) are closed, not overwritten; a one-time move from an all-users install; uninstall asks about the user's data. Answers spec Q4 and Q6. | spec R1, R3–R5 | Done (`2238df4`) |
| 3 | **Updates, phases 2–3.** Check for updates (manual and opt-in weekly), update information signed with an offline Ed25519 key, a notice in both windows, download and verify, install when the app closes, a failed install reported on next start, the 7xx error codes, the privacy wording, the release step, `cli update --check`, a separate update asset name, resumable downloads. | spec R6–R17, R20–R22 | Done (`8b89862`) |
| 4 | **Dependencies.** Pillow 12.3 → 13 (37 CVEs, reachable through every dropped image); fastmcp, certifi, filelock patch releases; `markitdown[all]` replaced by explicit extras so the online speech converter isn't installed at all. | assessment 2026-10-27, ADR-001 item 2 | Done (`a66b988`) |
| 5 | **Frozen build and end to end.** Rebuild into `build-webui\`, frozen self-test, the new window from the exe, and the updater end to end with look-alike installers. | | Done: 552 MB (45 MB smaller), selftest 15/15, 741/741 files identical to source, MCP and Whisper from the exe; the updater end to end on this build took a look-alike 1.3.0 to 1.3.2 and reopened it 58 s after the window closed. |

## Done

- 2026-11-11 · New window with many files: 300 small notes convert in 12.9 s instead of 42 s, with no stutter (frames over 50 ms: 250 → 0). Three causes, all measured: every snapshot rebuilt every row (46 ms each with 300 files; rows now update in place, 1 ms); pywebview's `run_js` waits for the page, so the conversion worker sat out each redraw (snapshots now go out from one sender thread, newest state wins); and every drop froze the page for ~1.1 s while pywebview serialised the event — including the whole document (the page now hands it only the file names, which is all Python reads).
- 2026-11-11 · New window, faster: usable 3.0 → 2.2 s from launch (warm disk, median of 5). markitdown's import now starts once the page is up (it held the interpreter while WebView2 started and the page loaded), and numpy/pdfium and markdown-it no longer load before the first paint (numpy alone was 1.3 s on a cold start; a test keeps them off that path). Going back to a big book's preview takes 1 ms, not 0.2 s (the last four rendered previews are kept). A document's YAML front matter previews as a YAML block, not a rule and a giant heading.
- 2026-11-11 · The new window is the default (Rob's call). A `window` setting (new/classic) picks the window at every start, in both Settings dialogs; `--new-ui` / `--classic` override it for one run. Every install and upgrade from 1.3 opens the new window, and a user who picks classic keeps it across updates. Without the WebView2 runtime the classic window opens with MS-606 ("Get WebView2…"), and Setup's last page offers Microsoft's download page. pip's `markdown-sidekick` and `python -m markdown_sidekick` follow the setting too. Found by `/verify`: a `settings.json` saved with a byte-order mark (Notepad, PowerShell 5) read as unreadable and reset every setting (MS-501); it now loads.
- 2026-11-10 · Tests for the classic window itself (33, on one real hidden window with a stand-in engine): adding and dropping files, what is locked while converting, failures and retry, a crashed batch, clearing, preview and copy, every save path, the export bar. Seven deliberate breakages of `ui.py` were each caught. No defect found.
- 2026-11-10 · Tests for the classic preview, the error dialog and notice bar, and the guide and AI-setup prompt (65 tests on real Tk widgets). They found one defect, fixed: a link whose address has brackets (`…/Mercury_(planet)`) was cut at the first `)`.
- 2026-11-10 · An update started from the new window reopens the new window (`/RELAUNCHUI=new`; Setup accepts that one word and passes nothing else on). Checked with a probe installer built from the real script's lines; not rerun end to end on a full build.
- 2026-10-31 · Build: OpenCV's unused FFmpeg DLL dropped, 27 MB (`aa744e4`).
- 2026-10-31 · MCP: `convert_url` only connects to public addresses (MS-133), every tool is annotated, downloads trust Mozilla's roots as well as Windows' (MS-134) (`4ebe5e7`).
- 2026-10-31 · Export: a table cut across parts repeats its header in each part (`747b27c`).
- 2026-10-30 · New window: a marker inside a paragraph no longer repeats its text; confirmation boxes are owned by the window, one at a time (found by `/verify`).
- 2026-10-30 · New window shows page anchors and figure markers as muted placeholders, not escaped HTML (found by the frozen check).
- 2026-10-30 · Dependencies: Pillow 13, fastmcp 4.1.4, only markitdown's local extras (`a66b988`).
- 2026-10-30 · Updates: opt-in signed check, update bar, install when the app closes (`8b89862`).
- 2026-10-30 · Installer phase 1: per-user only, close running copies, move, uninstall data (`2238df4`).
- 2026-10-30 · New window polish: one note per problem, debug title, removed files skipped (`b51a7b4`).
- 2026-10-30 · Settings in the new window, theme remembered, dialogs open in a real folder (`4d07c89`).
- 2026-10-30 · 1.3.0 installer starts every upgrade from an empty `_internal` (`cb173d4`, PR #5; draft re-uploaded).
- 2026-10-29 · Install/update spec; a helper that answers native dialogs for verification (`063ee4a`).
- 2026-10-29 · One save step for every front-end (ADR-002) and Save in the new window (`eead43c`).
- 2026-10-29 · The redesigned window as a preview, `app.py --new-ui` (ADR-003 spike, `7777b26`).
- 2026-10-28 · Audio never goes to Google's speech API (ADR-001 item 1, `94b76c5`).

## Backlog

### Needs Rob (an account, a decision or a person)

| Item | Why it can't be done here | What to do |
|---|---|---|
| Merge PR #5, close #4, publish the v1.3.0 draft | Publishing ships to users; merging was refused to Claude as "merge without review" | Review PR #5 → merge → close #4 → Releases → v1.3.0 → Publish. Publishing tags `cb173d4`. |
| Review the work stacked behind PR #5 | Six branches wait on a review, and none can land before PR #5 | In order: PR #6 (this branch) → PR #7 (corrupt-file guard, MS-127) → `claude/xenodochial-carson-838135` (legacy text encodings, MS-126; stacked on PR #7). Beside those, forked from this branch: `claude/fervent-robinson-c21e46` (`strip_toc_tables` deleting real tables), `claude/frosty-bell-c42f67` (MCP Bundle), `claude/trusting-kare-e1c7e0` (PDF benchmark report). The last four are on this PC only: not pushed, no PR. |
| PyPI publishing | Needs Rob's PyPI account | PyPI → Publishing → add a pending publisher (project `markdown-sidekick`, owner `boblauzon`, repo `markdown-sidekick`, workflow `publish.yml`, environment `pypi`), then re-run the publish workflow. |
| Code signing (exes, installer, uninstaller, bundled DLLs) | An organization certificate needs VibeProSoft registered as a business | Register the business → order the certificate (cloud signing, so CI can use it) → spec P2. |
| Back up the update-signing key | It's a secret that must live offline | Copy the key folder named in `docs/release-signing.md` to offline storage; without it no update can ever be signed again. |
| Spec Q1, Q2, Q3, Q5 | Product decisions | Defaults were chosen and are listed in the spec; confirm or change them. |
| Explorer drag-and-drop and a clean Windows 10 PC for the new window | Needs a person at the desktop / another PC | Drop a file from Explorer onto `app.py --new-ui`; run the installer on a PC without Python. |
| Usability sessions (X7) → Guided steps, Revise with AI (ADR-003 go/no-go) | ADR-003: nothing is rebuilt on these until real users have tried the prototype. (Making the new window the default was decided without them, 2026-11-11.) | Run 3–5 sessions on the Designer prototype; decide per feature. |
| winget listing | The submission is a PR from Rob's GitHub account to `microsoft/winget-pkgs` | After 1.4.0 ships: `wingetcreate new <installer URL>`. |

### Later (ideas, not commitments)

- Design: the Archivo font (OFL, would need bundling), and the red accent doubling as the error colour.
- Release automation on GitHub Actions with cloud signing; a beta channel; smaller updates when dependencies haven't changed.
- A library (batch) mode in the window; a watch folder; MOBI/AZW3 discovery.
- PDF tail: a Docling benchmark, text tables, logo OCR noise.
- Test coverage for the rest of `ui.py`: the Settings dialog, the help window, the update bar and the AI summary at save time.
- A VibeProSoft website.
