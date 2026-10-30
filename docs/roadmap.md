# Roadmap

**Updated:** 2026-10-30 · **Branch:** `claude/ui-webview`, plus the 1.3.0 release on `claude/v1.4-library-batch` (PR #5).

This is the working list: what's being built, in order, and what waits on something outside the code (a decision, an account, real users). Each item lands with a `/verify` run against the real app and a `/code-review`.

## Now (2026-10-30), in order

| # | Item | Source | Status |
|---|---|---|---|
| 1 | **New window polish.** Collapse repeated status-line notes; keep the notes when some files fail; show debug mode in the title; files removed or cleared while a batch runs are skipped, not converted. | verify findings 2026-10-29/30 | Done (`b51a7b4`) |
| 2 | **Installer, phase 1.** Per-user installs only; running copies (including `--mcp`) are closed, not overwritten; a one-time move from an all-users install; uninstall asks about the user's data. Answers spec Q4 and Q6. | spec R1, R3–R5 | Done (`2238df4`) |
| 3 | **Updates, phases 2–3.** Check for updates (manual and opt-in weekly), update information signed with an offline Ed25519 key, a notice in both windows, download and verify, install when the app closes, a failed install reported on next start, the 7xx error codes, the privacy wording, the release step, `cli update --check`, a separate update asset name, resumable downloads. | spec R6–R17, R20–R22 | Done (`8b89862`) |
| 4 | **Dependencies.** Pillow 12.3 → 13 (37 CVEs, reachable through every dropped image); fastmcp, certifi, filelock patch releases; `markitdown[all]` replaced by explicit extras so the online speech converter isn't installed at all. | assessment 2026-10-27, ADR-001 item 2 | Done (`a66b988`) |
| 5 | **Frozen build and end to end.** Rebuild into `build-webui\`, frozen self-test, the new window from the exe, and the updater end to end with look-alike installers. | | Built and checked: 552 MB (45 MB smaller), selftest 15/15, 741/741 files identical to source, MCP and Whisper from the exe. The updater end to end last ran on the build before the dependency update (`8b89862`); rerun on this build pending. |

## Done

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
| PyPI publishing | Needs Rob's PyPI account | PyPI → Publishing → add a pending publisher (project `markdown-sidekick`, owner `boblauzon`, repo `markdown-sidekick`, workflow `publish.yml`, environment `pypi`), then re-run the publish workflow. |
| Code signing (exes, installer, uninstaller, bundled DLLs) | An organization certificate needs VibeProSoft registered as a business | Register the business → order the certificate (cloud signing, so CI can use it) → spec P2. |
| Back up the update-signing key | It's a secret that must live offline | Copy the key folder named in `docs/release-signing.md` to offline storage; without it no update can ever be signed again. |
| Spec Q1, Q2, Q3, Q5 | Product decisions | Defaults were chosen and are listed in the spec; confirm or change them. |
| Explorer drag-and-drop and a clean Windows 10 PC for the new window | Needs a person at the desktop / another PC | Drop a file from Explorer onto `app.py --new-ui`; run the installer on a PC without Python. |
| Usability sessions (X7) → Guided steps, Revise with AI, making the new window the default (ADR-003 go/no-go) | ADR-003: nothing is rebuilt on these until real users have tried the prototype | Run 3–5 sessions on the Designer prototype; decide per feature. |
| winget listing | The submission is a PR from Rob's GitHub account to `microsoft/winget-pkgs` | After 1.4.0 ships: `wingetcreate new <installer URL>`. |

### Later (ideas, not commitments)

- Design: the Archivo font (OFL, would need bundling), and the red accent doubling as the error colour.
- Release automation on GitHub Actions with cloud signing; a beta channel; smaller updates when dependencies haven't changed.
- A library (batch) mode in the window; a watch folder; MOBI/AZW3 discovery.
- PDF tail: a Docling benchmark, text tables, logo OCR noise.
- Test coverage for `ui.py`, `mdrender.py`, `errorview.py`, `guide.py` (0% today).
- A VibeProSoft website.
