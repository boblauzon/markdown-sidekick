"""Updates (spec R6–R17): the signed manifest, the version gate, the
download check and the release step, against a local HTTP server."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import threading
from datetime import date
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from markdown_sidekick import updates
from markdown_sidekick.settings import Settings

KEY = Ed25519PrivateKey.generate()
PUB = base64.b64encode(KEY.public_key().public_bytes(serialization.Encoding.Raw,
                                                     serialization.PublicFormat.Raw)).decode()


def _manifest(version="9.0.0", payload=b"installer bytes", url="https://example.invalid/i.exe", **extra):
    data = {"version": version, "channel": "stable", "notes": "Better.", "important": False,
            "installer": {"name": "MarkdownSidekick-Update-9.0.0.exe", "url": url, "size": len(payload),
                          "sha256": hashlib.sha256(payload).hexdigest()}, **extra}
    body = json.dumps(data).encode()
    return body, base64.b64encode(KEY.sign(body))


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(updates, "PUBLIC_KEYS", (PUB,))


@pytest.fixture
def server(tmp_path):
    """Serves tmp_path/www over http, honouring Range like GitHub does."""
    root = tmp_path / "www"
    root.mkdir()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            path = root / self.path.lstrip("/")
            if rng and path.is_file():
                start = int(rng.split("=")[1].split("-")[0])
                data = path.read_bytes()[start:]
                self.send_response(206)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            super().do_GET()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _publish(root: Path, body: bytes, sig: bytes) -> None:
    (root / "latest.json").write_bytes(body)
    (root / "latest.json.sig").write_bytes(sig)


class TestCheck:
    def test_newer_release_is_offered(self, keys, server):
        root, base = server
        _publish(root, *_manifest())
        rel = updates.check("1.3.0", url=f"{base}/latest.json")
        assert rel.version == "9.0.0" and rel.notes == "Better." and rel.size == len(b"installer bytes")

    def test_same_or_older_release_is_up_to_date(self, keys, server):
        root, base = server
        _publish(root, *_manifest(version="1.3.0"))
        assert updates.check("1.3.0", url=f"{base}/latest.json") is None
        assert updates.check("1.10.0", url=f"{base}/latest.json") is None  # numeric, not text, order

    def test_a_changed_byte_is_refused(self, keys, server):
        root, base = server
        body, sig = _manifest()
        _publish(root, body.replace(b"Better.", b"Worse!."), sig)
        with pytest.raises(updates.UpdateError) as err:
            updates.check("1.3.0", url=f"{base}/latest.json")
        assert err.value.code == "MS-702"

    def test_another_key_is_refused(self, server):
        root, base = server  # PUBLIC_KEYS is the app's real key, not the test key
        _publish(root, *_manifest())
        with pytest.raises(updates.UpdateError) as err:
            updates.check("1.3.0", url=f"{base}/latest.json")
        assert err.value.code == "MS-702"

    def test_other_channels_and_plain_http_installers_are_refused(self, keys, server):
        root, base = server
        _publish(root, *_manifest(channel="beta"))
        with pytest.raises(updates.UpdateError):
            updates.check("1.3.0", url=f"{base}/latest.json")
        _publish(root, *_manifest(url="http://example.invalid/i.exe"))
        with pytest.raises(updates.UpdateError) as err:
            updates.check("1.3.0", url=f"{base}/latest.json")
        assert "https" in str(err.value)

    def test_a_release_without_update_information_offers_nothing(self, keys, server):
        _, base = server  # GitHub's 404: the latest release predates the updater
        assert updates.check("1.3.0", url=f"{base}/missing.json") is None

    def test_an_unreachable_server_is_ms_701(self, keys):
        with pytest.raises(updates.UpdateError) as err:
            updates.check("1.3.0", url="http://127.0.0.1:9/latest.json")
        assert err.value.code == "MS-701"

    def test_versions_compare_as_numbers(self):
        assert updates.parse_version("1.10.0") > updates.parse_version("1.9.9")
        assert updates.parse_version("junk") < updates.parse_version("0.0.1")

    def test_the_url_override_needs_debug_mode(self, monkeypatch):
        from markdown_sidekick import debuglog

        monkeypatch.setenv(updates.ENV_URL, "http://127.0.0.1:1/latest.json")
        assert updates.manifest_url() == updates.MANIFEST_URL
        debuglog.enable("test", snapshots=False)
        try:
            assert updates.manifest_url() == "http://127.0.0.1:1/latest.json"
        finally:
            debuglog.disable()


class TestDue:
    def test_only_when_weekly_and_a_week_has_passed(self):
        s = Settings(update_check="weekly", update_last_check="2026-10-20")
        assert updates.due(s, today=date(2026, 10, 27)) and not updates.due(s, today=date(2026, 10, 26))
        assert updates.due(Settings(update_check="weekly"), today=date(2026, 10, 27))  # never checked
        assert not updates.due(Settings(update_check="ask"), today=date(2026, 10, 27))
        assert not updates.due(Settings(update_check="off"), today=date(2026, 10, 27))


class TestDownload:
    def _release(self, base, payload):
        body, _ = _manifest(payload=payload, url=f"{base}/i.exe")
        data = json.loads(body)["installer"]
        return updates.Release("9.0.0", "", False, "MarkdownSidekick-Update-9.0.0.exe", data["url"],
                               data["size"], data["sha256"])

    def test_downloads_and_checks(self, server, tmp_path):
        root, base = server
        payload = b"x" * 3_000_000
        (root / "i.exe").write_bytes(payload)
        seen = []
        path = updates.download(self._release(base, payload), tmp_path / "dl", on_progress=lambda d, t: seen.append(d))
        assert path.read_bytes() == payload and seen[-1] == len(payload)

    def test_resumes_a_partial_download(self, server, tmp_path):
        root, base = server
        payload = bytes(range(256)) * 10_000
        (root / "i.exe").write_bytes(payload)
        dl = tmp_path / "dl"
        dl.mkdir()
        (dl / "MarkdownSidekick-Update-9.0.0.exe.part").write_bytes(payload[:1000])
        assert updates.download(self._release(base, payload), dl).read_bytes() == payload

    def test_wrong_bytes_are_deleted_never_kept(self, server, tmp_path):
        root, base = server
        (root / "i.exe").write_bytes(b"tampered installer")
        with pytest.raises(updates.UpdateError) as err:
            updates.download(self._release(base, b"genuine installer"), tmp_path / "dl")
        assert err.value.code == "MS-702"
        assert not list((tmp_path / "dl").iterdir())

    def test_cancel_and_network_failure_are_ms_703(self, server, tmp_path):
        root, base = server
        (root / "i.exe").write_bytes(b"y" * 2_000_000)
        rel = self._release(base, b"y" * 2_000_000)
        with pytest.raises(updates.UpdateError) as err:
            updates.download(rel, tmp_path / "dl", cancelled=lambda: True)
        assert err.value.code == "MS-703"
        rel.installer_url = "http://127.0.0.1:9/i.exe"
        with pytest.raises(updates.UpdateError) as err:
            updates.download(rel, tmp_path / "dl2")
        assert err.value.code == "MS-703"


class TestPending:
    def test_a_failed_install_is_reported_once(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        folder = updates.updates_dir()
        folder.mkdir(parents=True)
        (folder / "pending.json").write_text(json.dumps({"version": "99.0.0", "log": "x.log"}), encoding="utf-8")
        incident = updates.finish_pending()
        assert incident is not None and incident.code == "MS-704"
        assert updates.finish_pending() is None  # once

    def test_a_finished_install_is_tidied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        folder = updates.updates_dir()
        folder.mkdir(parents=True)
        installer = folder / "MarkdownSidekick-Update-1.0.0.exe"
        installer.write_bytes(b"done")
        (folder / "pending.json").write_text(json.dumps({"version": "1.0.0", "installer": str(installer)}),
                                             encoding="utf-8")
        assert updates.finish_pending() is None and not installer.exists()


def test_release_step_writes_a_manifest_the_app_accepts(keys, tmp_path, monkeypatch):
    import importlib.util

    from markdown_sidekick import __version__

    spec = importlib.util.spec_from_file_location("release_manifest", Path(__file__).parents[1] / "tools" / "release_manifest.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    installer = tmp_path / f"MarkdownSidekick-Setup-{__version__}.exe"
    installer.write_bytes(b"setup")
    assert tool.main([str(installer), "--notes", "Fixes.", "--key", str(key_file)]) == 0
    data = updates.verify((tmp_path / "latest.json").read_bytes(), (tmp_path / "latest.json.sig").read_bytes())
    assert data["version"] == __version__ and data["installer"]["name"] == f"MarkdownSidekick-Update-{__version__}.exe"
    assert (tmp_path / data["installer"]["name"]).read_bytes() == b"setup"
    wrong = tmp_path / "MarkdownSidekick-Setup-0.0.1.exe"
    wrong.write_bytes(b"x")
    with pytest.raises(SystemExit):
        tool.main([str(wrong), "--notes", "x", "--key", str(key_file)])


def test_an_installer_that_cannot_start_leaves_nothing_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    rel = updates.Release("9.0.0", "", False, "x.exe", "https://example.invalid/x.exe", 1, "0" * 64)
    with pytest.raises(updates.UpdateError) as err:
        updates.start_install(tmp_path / "missing-installer.exe", rel)
    assert err.value.code == "MS-704"
    assert not (updates.updates_dir() / "pending.json").exists()


def test_a_corrupt_marker_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    folder = updates.updates_dir()
    folder.mkdir(parents=True)
    (folder / "pending.json").write_text("{not json", encoding="utf-8")
    assert updates.finish_pending() is None and not (folder / "pending.json").exists()


class TestFlow:
    def _flow(self, tmp_path, monkeypatch, started):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(updates, "start_install", lambda path, rel: started.append(rel.version))
        flow = updates.UpdateFlow(Settings(), lambda: None, on_change=lambda: None, on_message=lambda t: None)
        flow.release = updates.Release("9.0.0", "", False, "u.exe", "https://example.invalid/u.exe", 1, "0" * 64)
        flow.state, flow._installer = "ready", tmp_path / "u.exe"
        return flow

    def test_install_starts_once(self, tmp_path, monkeypatch):
        started = []
        flow = self._flow(tmp_path, monkeypatch, started)
        assert flow.install() is True and flow.install() is False  # Restart now, then the closing window
        assert started == ["9.0.0"]

    def test_a_check_keeps_a_downloaded_update(self, tmp_path, monkeypatch):
        flow = self._flow(tmp_path, monkeypatch, [])
        flow.check(manual=True)
        assert flow.state == "ready"
