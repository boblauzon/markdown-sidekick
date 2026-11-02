"""The address guard behind convert_url. Real sockets on loopback, no internet:
127.0.0.2 plays a public host, 127.0.0.1 the private one it must not reach."""

from __future__ import annotations

import http.client
import http.server
import socket
import ssl
import threading
import urllib.error
import urllib.request

import pytest
from cryptography.hazmat.primitives import serialization
from tlsgen import issue, server_context, use_as_mozillas_list

from markdown_sidekick import netguard

PUBLIC = "127.0.0.2"
PRIVATE = "127.0.0.1"


class _Server:
    """Answers GET with text, or a redirect when the path is in ``redirects``."""

    def __init__(
        self, host: str, redirects: dict[str, str] | None = None, tls: ssl.SSLContext | None = None
    ) -> None:
        self.requests: list[str] = []
        self.hosts: list[str] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append(self.path)
                owner.hosts.append(self.headers.get("Host", ""))
                target = (redirects or {}).get(self.path)
                self.send_response(302 if target else 200)
                if target:
                    self.send_header("Location", target)
                self.send_header("Content-Length", "2" if not target else "0")
                self.end_headers()
                if not target:
                    self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer((host, 0), Handler)
        if tls:
            self.httpd.socket = tls.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _fetch(url: str) -> bytes:
    with netguard.opener().open(urllib.request.Request(url), timeout=10) as resp:
        return resp.read()


@pytest.fixture(autouse=True)
def no_system_proxy(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", dict)


@pytest.fixture()
def public_is_public(monkeypatch):
    monkeypatch.setattr(netguard, "_is_public", lambda ip: ip == PUBLIC)


@pytest.fixture()
def private_server():
    server = _Server(PRIVATE)
    yield server
    server.close()


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1", "127.8.8.8", "0.0.0.0", "10.1.2.3", "172.16.0.1", "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier NAT, Tailscale
        "224.0.0.1", "255.255.255.255", "240.0.0.1",
        "::1", "::", "fe80::1", "fe80::1%12", "fc00::1", "fd12:3456::1", "fec0::1", "ff02::1",
        "::ffff:127.0.0.1", "::ffff:192.168.1.1", "::7f00:1",
        "not an address", "",
    ],
)
def test_not_public(ip):
    assert not netguard._is_public(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public(ip):
    assert netguard._is_public(ip)


@pytest.mark.parametrize("form", ["127.0.0.1", "localhost"])
def test_this_computer_is_refused(private_server, form):
    with pytest.raises(netguard.BlockedAddress):
        _fetch(f"http://{form}:{private_server.port}/secret")
    assert private_server.requests == []


@pytest.mark.parametrize("form", ["2130706433", "0x7f.0.0.1", "127.1"])
def test_other_spellings_of_this_computer_never_arrive(private_server, form):
    # Refused where the resolver understands them, a failed lookup where it
    # doesn't (Windows): either way, no request.
    with pytest.raises((netguard.BlockedAddress, urllib.error.URLError)):
        _fetch(f"http://{form}:{private_server.port}/secret")
    assert private_server.requests == []


def test_ipv6_loopback_is_refused():
    with pytest.raises(netguard.BlockedAddress):
        _fetch("http://[::1]:9/secret")


def test_a_public_address_is_fetched(public_is_public):
    server = _Server(PUBLIC)
    try:
        assert _fetch(f"http://{PUBLIC}:{server.port}/page") == b"ok"
        assert server.requests == ["/page"]
    finally:
        server.close()


def test_a_redirect_to_a_private_address_is_refused(public_is_public, private_server):
    server = _Server(PUBLIC, {"/go": f"http://{PRIVATE}:{private_server.port}/secret"})
    try:
        with pytest.raises(netguard.BlockedAddress):
            _fetch(f"http://{PUBLIC}:{server.port}/go")
        assert server.requests == ["/go"]
        assert private_server.requests == []
    finally:
        server.close()


def test_a_redirect_to_another_public_page_is_followed(public_is_public):
    server = _Server(PUBLIC, {"/go": "/page"})
    try:
        assert _fetch(f"http://{PUBLIC}:{server.port}/go") == b"ok"
        assert server.requests == ["/go", "/page"]
    finally:
        server.close()


@pytest.mark.parametrize("target", ["ftp://127.0.0.1/secret", "file:///c:/windows/win.ini", "data:,secret"])
def test_a_redirect_cant_leave_http(public_is_public, target):
    server = _Server(PUBLIC, {"/go": target})
    try:
        with pytest.raises(urllib.error.URLError):
            _fetch(f"http://{PUBLIC}:{server.port}/go")
    finally:
        server.close()


def _resolves_to(monkeypatch, name: str, *answers: str) -> list[str]:
    """``name`` answers with the next address each lookup; returns the lookups."""
    real = socket.getaddrinfo
    lookups: list[str] = []

    def fake(host, port, *args, **kwargs):
        if host != name:
            return real(host, port, *args, **kwargs)
        lookups.append(host)
        ip = answers[min(len(lookups), len(answers)) - 1]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return lookups


def test_a_name_that_resolves_private_is_refused(monkeypatch, private_server):
    _resolves_to(monkeypatch, "innocent.test", PRIVATE)
    with pytest.raises(netguard.BlockedAddress):
        _fetch(f"http://innocent.test:{private_server.port}/secret")
    assert private_server.requests == []


def test_a_name_cant_change_its_answer_after_the_check(monkeypatch, public_is_public, private_server):
    # DNS rebinding: public for the check, private for the connect. There is
    # only one lookup, and the socket goes to the address it returned.
    server = _Server(PUBLIC)
    try:
        lookups = _resolves_to(monkeypatch, "rebind.test", PUBLIC, PRIVATE)
        assert _fetch(f"http://rebind.test:{server.port}/page") == b"ok"
        assert lookups == ["rebind.test"]
        assert server.requests == ["/page"]
        assert server.hosts == [f"rebind.test:{server.port}"]  # still the name, for virtual hosts and TLS
        assert private_server.requests == []
    finally:
        server.close()


def test_one_private_answer_refuses_the_host(monkeypatch, public_is_public, private_server):
    def fake(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PRIVATE, port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with pytest.raises(netguard.BlockedAddress):
        _fetch(f"http://mixed.test:{private_server.port}/secret")


def test_an_unknown_name_is_a_download_failure_not_a_block():
    with pytest.raises(urllib.error.URLError):
        _fetch("http://no-such-host.invalid/")


class TestSystemProxy:
    @pytest.fixture()
    def proxy(self, monkeypatch):
        # The proxy's own address is the user's setting, so a local one is fine.
        server = _Server(PRIVATE)
        proxies = {"http": f"http://{PRIVATE}:{server.port}", "https": f"http://{PRIVATE}:{server.port}"}
        monkeypatch.setattr(urllib.request, "getproxies", lambda: proxies)
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
        yield server
        server.close()

    def test_a_public_target_goes_through_a_local_proxy(self, proxy):
        assert _fetch("http://8.8.8.8/page") == b"ok"
        assert proxy.requests == ["http://8.8.8.8/page"]

    def test_a_private_target_never_reaches_the_proxy(self, proxy):
        with pytest.raises(netguard.BlockedAddress):
            _fetch("http://192.168.1.1/admin")
        assert proxy.requests == []

    def test_a_name_only_the_proxy_can_resolve_goes_through(self, proxy):
        assert _fetch("http://no-such-host.invalid/page") == b"ok"
        assert proxy.requests == ["http://no-such-host.invalid/page"]

    def test_https_through_a_proxy_is_checked_too(self):
        req = urllib.request.Request("https://10.0.0.1/admin")
        req.set_proxy("127.0.0.1:8888", "https")
        with pytest.raises(netguard.BlockedAddress):
            netguard._connection_class(req, netguard._PinnedHTTPS, http.client.HTTPSConnection)

        req = urllib.request.Request("https://8.8.8.8/page")
        req.set_proxy("127.0.0.1:8888", "https")
        assert netguard._connection_class(req, netguard._PinnedHTTPS, http.client.HTTPSConnection) is (
            http.client.HTTPSConnection
        )


def test_https_connections_are_pinned_too():
    # The stdlib hook both classes rely on: if a Python upgrade dropped it,
    # the plain-http refusals above would fail and this says why for https.
    assert netguard._PinnedHTTPS("example.com")._create_connection is netguard._connect_public
    assert netguard._PinnedHTTP("example.com")._create_connection is netguard._connect_public


class TestCertificates:
    """A throwaway root plays one on Mozilla's list that Windows hasn't fetched."""

    HOST = "secure.test"

    @pytest.fixture(autouse=True)
    def host_is_public(self, monkeypatch, public_is_public):
        _resolves_to(monkeypatch, self.HOST, PUBLIC)

    @pytest.fixture()
    def serve(self, tmp_path):
        servers = []

        def start(leaf) -> _Server:
            servers.append(_Server(PUBLIC, tls=server_context(leaf, tmp_path)))
            return servers[-1]

        yield start
        for server in servers:
            server.close()

    def _url(self, server: _Server) -> str:
        return f"https://{self.HOST}:{server.port}/page"

    @pytest.fixture()
    def on_mozillas_list(self, monkeypatch, tmp_path):
        return lambda root: use_as_mozillas_list(monkeypatch, tmp_path, root)

    def _assert_refused(self, server: _Server) -> None:
        with pytest.raises(urllib.error.URLError) as failure:
            _fetch(self._url(server))
        assert isinstance(failure.value.reason, ssl.SSLCertVerificationError)
        assert server.requests == []

    def test_a_root_windows_hasnt_fetched_is_trusted(self, serve, on_mozillas_list):
        # The bug: a site that works in a browser failed verification here.
        root = issue("Test Root", ca=True)
        server = serve(issue(self.HOST, root))
        on_mozillas_list(root)
        assert _fetch(self._url(server)) == b"ok"
        with pytest.raises(urllib.error.URLError) as failure:
            urllib.request.build_opener().open(self._url(server), timeout=10)  # Windows' store alone
        assert isinstance(failure.value.reason, ssl.SSLCertVerificationError)

    def test_a_root_installed_in_windows_still_counts(self, monkeypatch, serve):
        # Where a company's inspecting proxy puts its root. Mozilla's list stays
        # the real one, which doesn't have it.
        root = issue("Company Root", ca=True)
        server = serve(issue(self.HOST, root))
        der = root[0].public_bytes(serialization.Encoding.DER)
        monkeypatch.setattr(ssl, "enum_certificates", lambda store: [(der, "x509_asn", True)] if store == "ROOT" else [])
        assert _fetch(self._url(server)) == b"ok"

    def test_an_unknown_root_is_refused(self, serve, on_mozillas_list):
        server = serve(issue(self.HOST, issue("Unknown Root", ca=True)))
        on_mozillas_list(issue("Test Root", ca=True))
        self._assert_refused(server)

    def test_a_self_signed_certificate_is_refused(self, serve, on_mozillas_list):
        server = serve(issue(self.HOST))
        on_mozillas_list(issue("Test Root", ca=True))
        self._assert_refused(server)

    def test_a_certificate_for_another_name_is_refused(self, serve, on_mozillas_list):
        root = issue("Test Root", ca=True)
        server = serve(issue("other.test", root))
        on_mozillas_list(root)
        self._assert_refused(server)

    def test_an_expired_certificate_is_refused(self, serve, on_mozillas_list):
        root = issue("Test Root", ca=True)
        server = serve(issue(self.HOST, root, expired=True))
        on_mozillas_list(root)
        self._assert_refused(server)

    def test_selftest_notices_a_build_without_the_roots(self, monkeypatch, tmp_path):
        assert netguard.selftest()
        monkeypatch.setattr(netguard.certifi, "where", lambda: str(tmp_path / "missing.pem"))
        assert not netguard.selftest()
