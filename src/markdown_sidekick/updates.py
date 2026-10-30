"""Checking for and installing updates (docs/specs/install-and-update.md).

Nothing here runs unless the user asked: the manual check, or the weekly
check they agreed to on first run. A check fetches one small file from
GitHub — ``latest.json`` and its signature — and sends nothing about the user
or their files. The update information is signed with an offline Ed25519 key
(the app carries the public half), so a hijacked GitHub account can't push an
update: the installer the app downloads carries no Mark of the Web, so
Windows won't vet it — this signature, and the installer's sha256 inside it,
are the only gate.

Updating means running the per-user installer silently once the app closes
(``/RELAUNCH=1`` reopens it). A marker file records the attempt; the next
start compares versions and reports MS-704 if it didn't take.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from . import __version__, debuglog, errors

REPO = "boblauzon/markdown-sidekick"
MANIFEST_URL = f"https://github.com/{REPO}/releases/latest/download/latest.json"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
# The update-signing public keys (raw Ed25519, base64). The first signs
# today's releases; to rotate, ship a release that lists the new key too,
# then sign with it (docs/release-signing.md).
PUBLIC_KEYS = ("NphJtbq1WlSsw66jDhx6OsB2j4BfIMOZKh4EdzQEZq0=",)
CHECK_EVERY = timedelta(days=7)
TIMEOUT_S = 10
# Debug mode only: check a draft or local manifest instead of GitHub's.
ENV_URL = "MARKDOWN_SIDEKICK_UPDATE_URL"
# No version, no machine details: GitHub sees an IP and this.
_USER_AGENT = "MarkdownSidekick-updater"
_MAX_MANIFEST = 64 * 1024


class UpdateError(Exception):
    """A failed check, download or install, with its MS code (70x)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass
class Release:
    version: str
    notes: str
    important: bool
    installer_name: str
    installer_url: str
    size: int
    sha256: str
    published: str = ""


def parse_version(text: str) -> tuple[int, ...]:
    """``1.10.2`` -> (1, 10, 2); anything else sorts below every release."""
    if not re.fullmatch(r"\d+(\.\d+){0,3}", str(text or "")):
        return (-1,)
    return tuple(int(p) for p in text.split("."))


def manifest_url() -> str:
    override = os.environ.get(ENV_URL, "").strip()
    return override if override and debuglog.enabled() else MANIFEST_URL


def _get(url: str, *, headers: dict | None = None, limit: int | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = resp.read(limit + 1 if limit else -1)
    if limit and len(data) > limit:
        raise UpdateError("MS-702", f"{url} is larger than an update manifest can be")
    return data


def verify(manifest: bytes, signature: bytes, keys: tuple[str, ...] | None = None) -> dict:
    """The manifest's content, if one of ``keys`` (default: the app's)
    signed exactly these bytes."""
    keys = keys or PUBLIC_KEYS
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        sig = base64.b64decode(signature.strip(), validate=True)
    except ValueError as exc:
        raise UpdateError("MS-702", "the signature isn't base64") from exc
    for key in keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(sig, manifest)
            break
        except InvalidSignature:
            continue
    else:
        raise UpdateError("MS-702", "the update information isn't signed by Markdown Sidekick's key")
    try:
        return json.loads(manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("MS-702", "the update information isn't valid JSON") from exc


# Signed with the real key: app.py --selftest checks it, which proves the
# frozen build can verify an update offline (cryptography's Ed25519 is in).
_SELFTEST_BODY = b'{"selftest": "Markdown Sidekick update signature"}'
_SELFTEST_SIG = b"v4/c/rRQ/Su2lImbdpIw8mGsPWqvLW9gOEJGMarbAuid4zQe6sowXnVb1Y/L/RFqykas4fH96DUDndcBf2c5Ag=="


def selftest() -> bool:
    """The genuine vector verifies, and the same one with a byte added doesn't."""
    try:
        genuine = verify(_SELFTEST_BODY, _SELFTEST_SIG) == {"selftest": "Markdown Sidekick update signature"}
    except UpdateError:
        return False
    try:
        verify(_SELFTEST_BODY + b" ", _SELFTEST_SIG)
    except UpdateError:
        return genuine
    return False


def _release(data: dict) -> Release:
    try:
        inst = data["installer"]
        rel = Release(
            version=str(data["version"]),
            notes=str(data.get("notes", "")),
            important=bool(data.get("important", False)),
            installer_name=str(inst["name"]),
            installer_url=str(inst["url"]),
            size=int(inst["size"]),
            sha256=str(inst["sha256"]).lower(),
            published=str(data.get("published", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError("MS-702", f"the update information is missing {exc}") from exc
    if not re.fullmatch(r"[0-9a-f]{64}", rel.sha256):
        raise UpdateError("MS-702", "the update information has no usable installer checksum")
    testing = debuglog.enabled() and os.environ.get(ENV_URL)  # a local test server is plain http
    if not rel.installer_url.startswith("https://") and not testing:
        raise UpdateError("MS-702", "the installer isn't served over https")
    return rel


def check(current: str = __version__, url: str | None = None) -> Release | None:
    """The newer stable release, or None when this is the latest. Raises
    UpdateError: MS-701 (couldn't reach it) or MS-702 (didn't verify)."""
    url = url or manifest_url()
    try:
        manifest = _get(url, limit=_MAX_MANIFEST)
        signature = _get(url + ".sig", limit=1024)
    except UpdateError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # the latest release predates the updater: nothing is offered
            debuglog.event("update.no_manifest", url=url)
            return None
        raise UpdateError("MS-701", f"HTTP {exc.code} from {url}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError("MS-701", f"{type(exc).__name__}: {exc}") from exc
    data = verify(manifest, signature)
    if data.get("channel", "stable") != "stable":
        raise UpdateError("MS-702", f"the update information is for the {data.get('channel')!r} channel")
    rel = _release(data)
    debuglog.event("update.check", url=url, latest=rel.version, current=current)
    # Only ever forwards: an old, validly signed manifest replayed can't
    # walk an install back to a release with known problems.
    return rel if parse_version(rel.version) > parse_version(current) else None


def due(settings, today: date | None = None) -> bool:
    """Whether the weekly check the user agreed to should run now."""
    if settings.update_check != "weekly":
        return False
    try:
        last = date.fromisoformat(settings.update_last_check)
    except ValueError:
        return True
    return (today or date.today()) - last >= CHECK_EVERY


# -- installing -------------------------------------------------------------
def updates_dir() -> Path:
    from .settings import app_data_dir

    return app_data_dir("updates")


@functools.lru_cache(maxsize=1)  # one answer per run; the snapshot asks often
def install_dir() -> Path | None:
    """This copy's folder when it can update itself: the per-user installer's
    copy (frozen, with its uninstall entry pointing here). The portable zip,
    a pip install and a source checkout get a download link instead."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return None
    import winreg

    here = Path(sys.executable).resolve().parent
    root = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    try:  # whichever per-user entry installed this folder (Inno: "<AppId>_is1")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root) as uninstall:
            for i in range(winreg.QueryInfoKey(uninstall)[0]):
                name = winreg.EnumKey(uninstall, i)
                if not name.endswith("_is1"):
                    continue
                try:
                    with winreg.OpenKey(uninstall, name) as k:
                        if Path(winreg.QueryValueEx(k, "InstallLocation")[0]).resolve() == here:
                            return here
                except OSError:
                    continue
    except OSError:
        pass
    return None


def download(release: Release, dest_dir: Path | None = None,
             on_progress: Callable[[int, int], None] | None = None,
             cancelled: Callable[[], bool] | None = None) -> Path:
    """Download the release's installer, resuming a partial one, and check
    its size and sha256 before anything can run it. Raises UpdateError:
    MS-703 (download failed or cancelled), MS-702 (wrong bytes; deleted)."""
    dest_dir = dest_dir or updates_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / Path(release.installer_name).name
    part = final.with_name(final.name + ".part")
    if final.exists() and _sha256(final) == release.sha256:
        return final
    have = part.stat().st_size if part.exists() else 0
    if have >= release.size:
        part.unlink()
        have = 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    req = urllib.request.Request(release.installer_url, headers={"User-Agent": _USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            if have and resp.status != 206:  # the server ignored Range: start over
                have = 0
            with open(part, "ab" if have else "wb") as out:
                done = have
                while chunk := resp.read(1 << 20):
                    if cancelled is not None and cancelled():
                        raise UpdateError("MS-703", "the download was cancelled")
                    out.write(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, release.size)
    except UpdateError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError("MS-703", f"{type(exc).__name__}: {exc}") from exc
    if part.stat().st_size != release.size or _sha256(part) != release.sha256:
        part.unlink(missing_ok=True)
        raise UpdateError("MS-702", "the downloaded installer doesn't match the signed update information")
    os.replace(part, final)
    debuglog.event("update.downloaded", file=final.name, bytes=release.size)
    return final


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def start_install(installer: Path, release: Release) -> None:
    """Run the installer silently; it closes any running copy (the caller is
    exiting), installs, and reopens the app. The marker lets the next start
    tell whether it worked. Raises UpdateError (MS-704) if it can't start."""
    folder = updates_dir()
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "install.log"
    marker = folder / "pending.json"
    marker.write_text(
        json.dumps({"version": release.version, "from": __version__, "log": str(log),
                    "installer": str(installer), "started": time.time()}),
        encoding="utf-8",
    )
    try:
        subprocess.Popen(  # noqa: S603 - our own installer, verified against the signed manifest
            [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
             f"/LOG={log}", "/RELAUNCH=1"],
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as exc:  # blocked or deleted (antivirus): nothing ran, so nothing is pending
        marker.unlink(missing_ok=True)
        raise UpdateError("MS-704", f"the installer couldn't start: {type(exc).__name__}: {exc}") from exc
    debuglog.event("update.install_started", version=release.version)


def finish_pending() -> errors.Incident | None:
    """At startup: tidy up after an update that worked, or report MS-704
    once (with the installer's log) for one that didn't."""
    marker = updates_dir() / "pending.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except OSError:
        return None  # no update was started
    except ValueError:  # not ours to judge: drop it rather than read it at every start
        marker.unlink(missing_ok=True)
        return None
    marker.unlink(missing_ok=True)
    if not isinstance(data, dict):
        return None
    if parse_version(__version__) >= parse_version(str(data.get("version", ""))):
        try:
            Path(data["installer"]).unlink(missing_ok=True)  # it did its job
        except (KeyError, OSError, TypeError) as exc:
            debuglog.event("update.cleanup_failed", error=f"{type(exc).__name__}: {exc}")
        return None
    return errors.report("MS-704", where="update", detail=f"still {__version__}, wanted {data.get('version')}",
                         log=str(data.get("log", "")))


# -- the flow both windows drive ------------------------------------------------
class UpdateFlow:
    """One update offer, from check to install-on-close, for a window to draw.

    Thread-safe: checks and downloads run on their own threads, and
    ``on_change`` is called (from whichever thread) after every change, so a
    window redraws from :meth:`snapshot`. ``on_message`` gets the one-line
    results of a check the user asked for. ``save`` persists the settings.
    """

    def __init__(self, settings, save: Callable[[], None], on_change: Callable[[], None],
                 on_message: Callable[[str], None]) -> None:
        import threading

        self._settings, self._save = settings, save
        self._on_change, self._on_message = on_change, on_message
        self._lock = threading.Lock()
        self.state = "idle"  # idle | checking | available | downloading | ready
        self.release: Release | None = None
        self.pct = 0
        self._installer: Path | None = None
        self._cancel = False

    def _set(self, **fields) -> None:
        with self._lock:
            for k, v in fields.items():
                setattr(self, k, v)
        self._on_change()

    def _thread(self, target) -> None:
        import threading

        threading.Thread(target=target, daemon=True, name="updates").start()

    def snapshot(self) -> dict | None:
        """What the update bar shows, or None when it's hidden."""
        with self._lock:
            if self.state not in ("available", "downloading", "ready") or self.release is None:
                return None
            r = self.release
            return {"state": self.state, "version": r.version, "notes": r.notes, "important": r.important,
                    "pct": self.pct, "canUpdate": install_dir() is not None, "releasesUrl": RELEASES_URL}

    def start(self) -> errors.Incident | None:
        """At startup: report an update that didn't take, and run the weekly
        check when it's due. Returns the incident to show, if any."""
        incident = finish_pending()
        if due(self._settings):
            self.check(manual=False)
        return incident

    def check(self, manual: bool) -> None:
        with self._lock:
            if self.state in ("checking", "downloading", "ready", "installing"):
                return  # a downloaded update stays ready until the app closes
            self.state = "checking"
        self._thread(lambda: self._check(manual))

    def _check(self, manual: bool) -> None:
        try:
            release = check()
        except UpdateError as exc:
            incident = errors.report(exc.code, where="update check", detail=str(exc))
            self._set(state="idle")
            if manual:  # an automatic check that can't connect stays quiet
                self._on_message(f"⚠ {incident.title} {incident.next_step} ({incident.code})")
            return
        self._settings.update_last_check = date.today().isoformat()
        self._save()
        if release is None:
            self._set(state="idle")
            if manual:
                self._on_message(f"You have the latest version ({__version__}).")
        elif manual or release.version != self._settings.update_skip_version:
            self._set(state="available", release=release, pct=0)
            if manual:  # replaces "Checking for updates…"
                self._on_message(f"Markdown Sidekick {release.version} is available.")
        else:
            self._set(state="idle")

    def skip(self) -> None:
        with self._lock:
            release = self.release
        if release is not None:
            self._settings.update_skip_version = release.version
            self._save()
        self._set(state="idle", release=None)

    def download(self) -> None:
        with self._lock:
            if self.state != "available" or self.release is None:
                return
            self.state, self._cancel, release = "downloading", False, self.release
        self._on_change()
        self._thread(lambda: self._download(release))

    def _download(self, release: Release) -> None:
        last = [-1]

        def progress(done: int, total: int) -> None:
            pct = int(done * 100 / max(total, 1))
            if pct != last[0]:
                last[0] = pct
                self._set(pct=pct)

        try:
            path = download(release, on_progress=progress, cancelled=lambda: self._cancel)
        except UpdateError as exc:
            incident = errors.report(exc.code, where="update download", detail=str(exc))
            self._set(state="available", pct=0)
            self._on_message(f"⚠ {incident.title} {incident.next_step} ({incident.code})")
            return
        self._set(state="ready", pct=100, _installer=path)

    def install(self) -> bool:
        """Start the downloaded installer (the caller then exits). False when
        nothing is ready, or it couldn't start (the message says why)."""
        with self._lock:
            path, release, ready = self._installer, self.release, self.state == "ready"
            self._cancel = True  # a download still running is abandoned
            if ready and path is not None and release is not None:
                self.state = "installing"  # once: closing after Restart now mustn't start a second
        if not ready or path is None or release is None:
            return False
        try:
            start_install(path, release)
        except UpdateError as exc:
            incident = errors.report(exc.code, where="update install", detail=str(exc))
            self._set(state="available", pct=0)
            self._on_message(f"⚠ {incident.title} {incident.next_step} ({incident.code}, ref {incident.ref})")
            return False
        return True
