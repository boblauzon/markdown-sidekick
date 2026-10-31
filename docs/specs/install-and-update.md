# Spec: Install, update and uninstall

**Status:** Phases 1–3 built 2026-10-30 (R1–R17, R20–R22); P2 waits on code signing · **Date:** 2026-10-29 · **Owner:** Rob · **Source:** brainstorm of 2026-10-29

## Problem statement

Markdown Sidekick has a real installer (Inno Setup), but nothing after the first install is easy. Nothing tells a user that a new version exists. Getting it means finding the GitHub releases page, downloading 237 MB and clicking through a SmartScreen warning (the exe isn't signed). Applying it has three known defects:

- **Old files stay behind.** The installer never deletes `_internal`, so a new version is installed on top of the old version's files.
- **Two copies are possible.** The installer offers "for me" or "for all users", so one PC can end up with two installs.
- **Uninstall leaves things behind.** Settings, logs and Whisper models (up to about 1.5 GB) stay on disk, and so do AI-tool setups that now point at an exe that's gone.

**The cost is more than convenience.** Version 1.2.0 sends audio to Google's speech service whenever Whisper is off or fails. The fix is in 1.3.0, but a 1.2.0 user has no way to learn it exists. A fix that can't reach users doesn't protect them.

**Design for the user who never visits GitHub.** If that person ends up on the current version, everyone does.

## Goals

1. **Two clicks to the latest version, with no admin prompt.** From the update notice to the new version running, for any installed copy that has the updater (1.4.0 on).
2. **Important fixes reach people within a week.** An opted-in user learns about a release marked important within 7 days of its publication.
3. **Exactly one copy after any upgrade.** No files from an older version remain, and `--selftest` passes on the upgraded copy.
4. **Uninstall leaves nothing the user didn't choose to keep.**
5. **Updating never weakens the privacy promise or safety.**
   - No request is made unless the user turned checks on or clicked Check.
   - Only a version file is fetched.
   - A download whose signature doesn't verify is never run.

## Non-goals

| Not doing | Why |
|---|---|
| **Code signing** (Authenticode) | It needs an organization certificate, which needs VibeProSoft registered as a business first. It's on the roadmap (P2). This spec's update signing works without it. |
| **Updates turned on by default, or silent background installs** | Checking would break the "nothing leaves the computer" promise unless the user turns it on. Installing without asking takes control away from the user. |
| **Install for all users / enterprise deployment** (MSI, Group Policy) | Each update would need admin approval, which rules out a one-click update. Nobody has asked for it. |
| **Updating the portable zip, a pip install or a source checkout in place** | Those copies have no installer to run. They get a "new version" notice with a link instead. |
| **Smaller (patch) updates** | The full installer is simpler and always correct. The download size is acceptable for releases a few times a year. |
| **Telemetry** | Every measure in this spec uses aggregate GitHub download counts or tests on Rob's machine. |

## User stories

**Someone installing it for the first time**
- As a new user, I want to install without an admin prompt, so that I can install on a work laptop where I'm not an admin.
- As a new user, I want to be asked once whether the app may check for updates, and told exactly what that check sends, so that I can keep the privacy I chose this app for.

**Someone on an older version**
- As a user on an older version, I want the app to tell me when a new version is out, and say so plainly when it fixes a privacy or security problem, so that I don't have to watch GitHub.
- As a user in the middle of a long batch, I want the update to wait until I close the app, so that my work is never interrupted.
- As a user, I want the update to finish without any prompt, and the app to reopen with my settings, so that updating takes no effort.
- As a user who doesn't want this release, I want to skip it, so that I'm not reminded about it again.
- As a user offline or behind a firewall, I want a check that can't connect to stay out of my way. When I asked for the check myself, I want to be told why it failed.

**Someone who uses the app from an AI tool** (Claude Desktop or Cursor through MCP)
- As an AI-tool user, I want an update not to break my Claude Desktop setup, and to be told if Claude Desktop needs a restart afterwards, so that my tools keep working.

**Rob's own all-users copy**
- As a user with an old all-users install, I want the new installer to move me to a per-user install once, keeping my settings, so that I end up with a single copy.

**Someone uninstalling**
- As a user removing the app, I want to choose whether my settings, logs and downloaded models go too, and see how much space that frees, so that nothing is left behind unless I want it kept.

**Rob, as maintainer**
- As the maintainer, I want one release step to produce the signed update file alongside the installer, so that every release reaches existing users with no extra manual work.

## Requirements

### P0 — Phase 1: installer fixes (next release)

**R1. Per-user install only.**
- **Change:** `PrivilegesRequired=lowest`; remove `PrivilegesRequiredOverridesAllowed`.
- **Install location:** `{autopf}` resolves to `%LOCALAPPDATA%\Programs\Markdown Sidekick`.
- **Acceptance criteria:**
  - [ ] Installing on a standard (non-admin) account shows no admin prompt.
  - [ ] The uninstall entry is under HKCU, not HKLM.
  - [ ] The install folder is the same for every version, so AI-tool setups pointing at it keep working across upgrades.

**R2. Clean upgrades.**
- **Change:** `[InstallDelete] Type: filesandordirs; Name: "{app}\_internal"`. The two exes and the uninstaller are replaced normally.
- **Acceptance criteria:**
  - [ ] Given 1.3.0 is installed, when the next version is installed over it, then `_internal` has no `*.dist-info` folder for a package version the new build doesn't ship.
  - [ ] `--selftest` passes on the upgraded copy.
  - [ ] A failed install doesn't leave a copy that won't start.

**R3. Running copies are closed, not overwritten.**
- **Change:**
  - The app creates a named mutex at startup, in all modes including `--mcp` and the CLI.
  - The installer declares it with `AppMutex` and uses `CloseApplications=force`.
- **Acceptance criteria:**
  - [ ] An interactive install with the window open asks the user to close it.
  - [ ] An interactive install with `MarkdownSidekick.exe --mcp` running says that an AI tool (such as Claude Desktop) is using Markdown Sidekick and will reconnect after it restarts.
  - [ ] A silent install closes both and completes.
  - [ ] No "file in use" failure in any of these cases.

**R4. One-time move from an all-users install.**
- **Detection:** the installer looks for the HKLM uninstall entry for this app's ID.
- **Behaviour:**
  - The installer offers to remove that copy (one admin prompt, running the old uninstaller), then installs per-user.
  - Settings, logs and models are kept: they always lived per user in `%LOCALAPPDATA%\MarkdownSidekick`.
- **Acceptance criteria:**
  - [ ] On a PC with an all-users 1.2.0, choosing **Move it** leaves one Start-menu entry and one entry in Apps & features.
  - [ ] The settings from 1.2.0 are still in effect.
  - [ ] Choosing **Keep both** installs per-user and warns that two copies now exist.
  - [ ] If AI-tool setups point at the old exe, the user is told they need updating. Fixing them automatically is R18 (P1).

**R5. Uninstall asks about the user's data.**
- **Behaviour:** an interactive uninstall asks: "Also remove your settings, logs and downloaded models (N MB)?" The default answer is **No**.
- **Acceptance criteria:**
  - [ ] **Yes** removes `%LOCALAPPDATA%\MarkdownSidekick`.
  - [ ] **No** removes only the app.
  - [ ] A silent uninstall (`/VERYSILENT`) never asks and keeps the data.
  - [ ] Upgrades never trigger this question (Inno doesn't run the uninstaller on upgrade).

### P0 — Phase 2: checking for updates (1.4.0)

**R6. Check for updates, on request.**
- **Where:** Help → Check for updates, in both the classic and the new window.
- **What it does:** one HTTPS GET of `https://github.com/boblauzon/markdown-sidekick/releases/latest/download/latest.json` and its signature. Nothing else.
- **Results:**
  - **Up to date:** "You have the latest version (X)".
  - **New version:** the version, its "What's new", and an Update button.
  - **Couldn't check:** the reason, its MS code and the next step.
- **Acceptance criteria:**
  - [ ] Offline, the manual check reports MS-701 with a next step. Nothing else breaks.
  - [ ] The request carries no file names, settings or identifiers. Only the default User-Agent is sent, with the app version (decision below).

**R7. One question on first run.**
- **When:** the first time a version with the updater starts, the app asks once: *"Check for updates once a week? This only asks GitHub for the latest version number — nothing about you or your files is sent."*
- **Stored as:** `Settings.update_check`, with values `ask` / `weekly` / `off`. The user can change it later in Settings.
- **Acceptance criteria:**
  - [ ] Starting with fresh settings makes no network request until the question is answered **Yes**.
  - [ ] After **No**, the app never makes a request except from the manual check.
  - [ ] An existing user upgrading sees the question once.
  - [ ] It applies to both windows; the CLI and MCP never ask.

**R8. Weekly check.**
- **When:** if `update_check == "weekly"`, at most once every 7 days (`Settings.update_last_check`). It runs after the window is up, never on the startup path, with a 10-second timeout.
- **Errors:** they're written to the error log with their MS code, but not shown.
- **Acceptance criteria:**
  - [ ] Startup time is unchanged, measured with `MARKDOWN_SIDEKICK_WEBUI_PROBE`.
  - [ ] An offline automatic check shows nothing.

**R9. Update information you can trust.**
- **Contents:** each release publishes `latest.json`, plus `latest.json.sig`, an Ed25519 signature made with Rob's offline key. Fields: `version`, `published`, `installer` (asset name, size, sha256), `notes` (one paragraph), `important` (bool), `channel` (`stable`), and `min_updater` (reserved).
- **Verification:** the app holds the public key and uses `cryptography`, which is already in the build because pdfminer depends on it.
- **The app rejects the update information when:**
  - the signature doesn't verify;
  - the version isn't newer than the running one (no downgrade or replay of an old file);
  - the channel isn't `stable`.
- **Acceptance criteria:**
  - [ ] A `latest.json` changed by one byte → MS-702, and no update is offered.
  - [ ] A correctly signed file for an older version → "up to date".
  - [ ] The frozen `--selftest` gains an `update_signature` check that verifies a bundled test vector, which proves Ed25519 works in the frozen build.

**R10. The update notice.**
- **Shown in:** the existing notice bar in both windows: "Markdown Sidekick X is available — What's new · Update · Skip this version".
- **Important releases:** when `important` is true, the wording becomes "includes a privacy or security fix".
- **Skipping:** stored in `Settings.update_skip_version`.
- **Acceptance criteria:**
  - [ ] A skipped version isn't shown again.
  - [ ] A newer version is.
  - [ ] Whether an important release can be skipped permanently is an open question (Q1).

### P0 — Phase 3: installing the update (1.4.0)

**R11. Download and verify.**
- **Behaviour:** **Update** downloads the installer to `%LOCALAPPDATA%\MarkdownSidekick\updates\` in the background, with progress shown in the notice bar. It checks the size and sha256 against the signed `latest.json` before going any further.
- **Why this check matters:** a file the app downloads itself carries no Mark of the Web, so Windows doesn't run its SmartScreen reputation check when the file starts. The signed manifest is the only gate.
- **Acceptance criteria:**
  - [ ] A wrong hash → MS-702; the file is deleted and never run.
  - [ ] Cancelling or closing mid-download leaves no partial installer that a later run would trust.
  - [ ] The download folder is cleaned after a successful update.

**R12. Install when the app closes (the default).**
- **Two choices:** **Update when I close the app** (the default) and **Restart now**.
- **What happens:**
  - Either way, the app runs the installer with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /LOG=<updates>\install.log`, plus a relaunch flag the installer acts on.
  - The installer reopens the new version when it's done.
- **When work is running:** **Restart now** with a conversion, batch or save in progress asks first. **Update when I close the app** waits until the user quits.
- **Acceptance criteria:**
  - [ ] From clicking Update to the new version open, the user sees no admin prompt, no SmartScreen warning and no installer window.
  - [ ] Settings are kept.
  - [ ] With an `--mcp` process running, the update completes, and the notice beforehand says the AI tool will need to reconnect.

**R13. A failed install says so on next start.**
- **Behaviour:** if the installer exits with an error, the next start reports MS-704. The message points at `install.log` and offers to retry, or to download the installer manually from the release page.
- **Acceptance criteria:**
  - [ ] Forcing the installer to fail (for example by holding a locked file it must replace) leaves the old version working and shows MS-704 at the next start.

**R14. Copies that can't update themselves.**
- **Which copies:** a frozen copy outside the per-user install folder (the portable zip), a pip install, or a source checkout.
- **Behaviour:** these get the notice with a link to the release page instead of an **Update** button.
- **Acceptance criteria:**
  - [ ] An extracted zip shows "Download" (link), not "Update".

**R15. Error codes.**
- **New area:** 7xx, install and updates. Each code follows the existing rule: a code, a log entry and a next step. Each needs its USERGUIDE row (`tests/test_error_codes.py` enforces this).

  | Code | Meaning |
  |---|---|
  | MS-701 | Couldn't check for updates (offline or GitHub unreachable) |
  | MS-702 | The update information or download didn't verify, so it wasn't used |
  | MS-703 | The update download failed |
  | MS-704 | The update couldn't be installed |

- **Wording:** next steps must read correctly in both windows ("Choose …", not "click …").

**R16. Privacy disclosure.**
- **Where:** the USERGUIDE and README say exactly what a check sends.
  - An HTTPS request to github.com, which sees the IP address and User-Agent.
  - GitHub counts downloads in aggregate.
  - Nothing about files or settings is sent.
- **Wording change:** the promise "Nothing is uploaded anywhere" is reworded so that it stays true.
- **Acceptance criteria:**
  - [ ] The wording matches the code.

**R17. The release step.**
- **What it does:** a script, run where the private key lives, writes `latest.json` from the built installer (size, sha256, version from `__version__`), signs it, and adds both files to the release's assets.
- **Release process:** the release checklist (README → Releasing) gains this step. The step refuses to run if the installer's version doesn't match `__version__`.
- **Acceptance criteria:**
  - [ ] A release made with the script is found and verified by the previous version's updater, tested before publishing with a draft URL override that only debug builds accept.

### P1 — fast follow

- **R18. Fix AI-tool setups after a move.** After R4, the app finds Claude Desktop or Cursor setups that point at the old exe and offers to update them, with the user's consent, because these are another app's files.
- **R19. Uninstall offers to remove those setups too.** The app doesn't write them today (it only copies a setup prompt), so this works by finding setups that point at this install.
- **R20. CLI.** `markdown-sidekick-cli update --check` prints the latest version and exits 0 when up to date, 10 when an update is available. No automatic check, ever, from the CLI or MCP.
- **R21. A separate asset name for in-app downloads** (`MarkdownSidekick-Update-X.Y.Z.exe`, the same bytes). GitHub's download counts then tell in-app updates apart from fresh installs, with no telemetry.
- **R22. Resumable downloads**, for slow connections.

### P2 — designed for, not built now

- **Code signing**, once VibeProSoft can get an organization certificate:
  - **What gets signed:** both exes, the installer, the uninstaller (Inno `SignTool` + `SignedUninstaller`), and the bundled `.pyd`/`.dll` files, for Windows 11 Smart App Control.
  - **What the updater then checks:** a valid Authenticode signature from the same publisher, in addition to R9.
- **Release automation:** a GitHub Action builds the release from a tag, signing through the CA's cloud signing service (keys must stay on hardware).
- **winget:** a listing kept up to date by that workflow, so `winget upgrade` works.
- **A beta channel**, using the `channel` field reserved in R9.
- **Smaller updates:** only the exes, when `constraints.txt` hasn't changed.
- **Install-time extras:** "Connect to Claude Desktop" and an Explorer "Convert to Markdown" menu item.

## Success metrics

There's no telemetry, so every measure is aggregate GitHub data or a test on Rob's machine.

**Leading (each release)**

| Measure | Target | How |
|---|---|---|
| Upgrade test set passes | 100%, as a release gate | 1.2.0 all-users → new; 1.3.0 per-user → new; with the window open; with `--mcp` running; a forced install failure |
| From clicking Update to the new version open | ≤ 3 min on 100 Mbps; ≤ 30 s after the download | A stopwatch on Rob's machine |
| Prompts during an in-app update | 0 (no admin prompt, no SmartScreen, no installer window) | The same test |
| A tampered `latest.json` or installer is never run | 0 runs | R9 and R11 tests, and a tampered asset on a draft release |

**Lagging (60 days after 1.4.0)**

| Measure | Target | How |
|---|---|---|
| Weekly `latest.json` downloads | Above 0 and growing. The first 60 days set the baseline (hypothesis: about half of 1.4.0 installs opt in) | `gh api repos/:owner/:repo/releases` download counts |
| Share of a release's downloads in its first 14 days | Higher than 1.2.0's and 1.3.0's | The same API, with R21's separate update asset |
| Issues about leftover files, duplicate installs or broken updates | 0 | GitHub issues |

## Open questions

| # | Question | Who | Blocking? |
|---|---|---|---|
| Q1 | Can an *important* release be skipped permanently? **Default taken (confirm):** yes — Skip works the same, but the bar says it includes a privacy or security fix. | Rob | Done |
| Q2 | Which first-run answer is highlighted? **Default taken (confirm):** the new window highlights neither; the classic window's Yes/No box defaults to No (privacy first). | Rob | Done |
| Q3 | Where does the signing key live? **Done:** `%USERPROFILE%\.markdown-sidekick-release\` (not in the repo); `PUBLIC_KEYS` takes several keys for rotation; backup is Rob's (roadmap backlog); see `docs/release-signing.md`. | Rob / engineering | Done |
| Q4 | Does Inno's Restart Manager close an `--mcp` process whose pipes Claude Desktop holds? **Answered 2026-10-30: yes.** A silent upgrade with `/CLOSEAPPLICATIONS` force-closed the window and an `--mcp` server with a live client (log: "Shutting down applications using our files. (forced)"); the client saw the connection drop, and the upgrade succeeded. The AI tool needs a restart to reconnect, which the interactive notice says. | Engineering | Done |
| Q5 | Does the User-Agent include the app version? **Default taken (confirm):** no — `MarkdownSidekick-updater` only, so GitHub can't tell versions apart. | Rob | Done |
| Q6 | Can a standard user remove an all-users copy that a different admin account installed (R4)? **Answered: only with an administrator's credentials.** The old uninstaller asks Windows for permission itself; if that's declined, Setup says the copy is still installed and carries on, so the user ends with both copies, as with No. | Engineering | Done |
| Q7 | Does 1.3.0 get Phase 1 before it's published? **Tested 2026-10-29** (look-alike installers built from the real script, per-user, in a scratch folder): upgrading 1.2.0 → 1.3.0 leaves all 164 files 1.3.0 dropped, including 13 packages with two versions side by side (markitdown 0.1.6 + 0.1.8, mcp 1.28.1 + 2.3.0 and others). The app *reads* them: the upgraded MCP server reports version 3.4.2 instead of 4.1.0. Nothing else broke: self-test 14/14, conversions byte-identical, all 8 MCP tools work. R2's one line fixes it: 0 leftover files, 14/14, and the right version. Uninstall already removes the leftovers. **The decision left:** fold R2 into 1.3.0 (an installer-only rebuild with the same app files), or ship it next. | Rob | Yes, for publishing 1.3.0 |

## Timeline and phasing

1. **Now:** run the 1.2.0 → 1.3.0 upgrade test in a scratch per-user install, leaving Rob's Program Files copy alone. Its result answers Q7.
2. **Phase 1 (installer fixes, R1–R5):** in the next release, or in 1.3.0 itself if Q7 says so. It needs no app changes except R3's mutex.
3. **Phases 2 and 3 (R6–R17):** 1.4.0. **1.4.0 is the first version that can update itself.** Every earlier copy needs one manual upgrade, so the 1.4.0 release notes and README say so plainly, and winget (P2) helps after that.
4. **Code signing (P2):** after VibeProSoft is registered. Nothing in Phases 1–3 waits on it.

**Dependencies:**
- The new window (`--new-ui`) needs the notice bar and the Help entry before Phase 2.
- `Settings` gains `update_check`, `update_last_check` and `update_skip_version`. These are new keys, so no settings migration is needed, but `normalize()` must validate them.
