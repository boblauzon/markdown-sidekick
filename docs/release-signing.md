# Release signing (updates)

Every release's update information (`latest.json`) is signed with an Ed25519 key. The app ships the public half (`updates.PUBLIC_KEYS`) and only installs an update whose manifest that key signed. This is separate from code signing (Authenticode), which waits on an organization certificate; see the spec's P2.

## The key

- **Where:** `%USERPROFILE%\.markdown-sidekick-release\update-signing-key.pem`, created 2026-10-30. It is never in the repo.
- **Public half:** `NphJtbq1WlSsw66jDhx6OsB2j4BfIMOZKh4EdzQEZq0=`.
- **Back it up offline** (an encrypted USB key or a password manager's file vault). If it's lost, existing installs can't be sent another update until users reinstall by hand. If it leaks, rotate it (below).

## Each release

1. Build the exe and the installer as usual (README → Releasing).
2. Sign:

   ```powershell
   .venv\Scripts\python.exe tools\release_manifest.py installer\Output\MarkdownSidekick-Setup-X.Y.Z.exe --notes "One paragraph: what changed." [--important]
   ```

   `--important` is for a privacy or security fix: the app's notice says so.
3. Upload `MarkdownSidekick-Update-X.Y.Z.exe`, `latest.json` and `latest.json.sig` with the other assets. Updates read `releases/latest/download/latest.json`, so only a published (not draft, not pre-release) release is offered.

The script refuses an installer whose version isn't the package's `__version__`, and checks the signature against the app's own public keys before writing anything.

## Testing an update before publishing

In debug mode the app reads `MARKDOWN_SIDEKICK_UPDATE_URL` instead of GitHub's URL (and accepts a plain-http installer URL from it). Serve a folder with `latest.json`, `latest.json.sig` and the installer, and run `release_manifest.py --base-url http://127.0.0.1:PORT`.

## Rotating the key

1. Generate a new key pair. Add its public half to `PUBLIC_KEYS`, after the current one.
2. Ship a release signed with the **old** key, so every install learns the new key.
3. Sign later releases with the new key, and drop the old public half a release or two later.
