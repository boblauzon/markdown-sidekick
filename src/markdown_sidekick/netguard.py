"""Downloads that can't be pointed at this computer or its network.

The MCP server's convert_url takes its address from an AI client, which may
have read it in a document: instructions hidden there could ask for a router's
admin page or a local service and get the reply as Markdown. So the address of
every connection, redirects included, must be public. It is checked where the
socket is opened and the socket goes to the checked address, so a name can't
resolve to something public for the check and something private for the connect.

Behind a system proxy the proxy connects, not us: its own address is the user's
setting and is allowed, and the target can only be checked by what its name
resolves to here.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import socket
import ssl
import urllib.parse
import urllib.request

import certifi


class BlockedAddress(Exception):
    """The address belongs to this computer or a private network.

    Not an OSError, which urllib would wrap and retry as a download failure."""


def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6:
        if addr.is_site_local:  # deprecated, and is_global doesn't cover it
            return False
        addr = addr.ipv4_mapped or addr
    return addr.is_global and not (addr.is_multicast or addr.is_reserved)


def _public_addresses(host: str, port: int) -> list[tuple]:
    """What ``host`` resolves to. One private answer among public ones refuses
    the host: that mix is how a name gets a client onto a private address."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for *_, sockaddr in infos:
        if not _is_public(sockaddr[0]):
            raise BlockedAddress(f"{host} is at {sockaddr[0]}")
    return infos


def _connect_public(address: tuple[str, int], timeout: float, source_address=None) -> socket.socket:
    host, port = address
    error = OSError(f"no address for {host}")
    for *_, sockaddr in _public_addresses(host, port):
        try:
            return socket.create_connection(sockaddr[:2], timeout)
        except OSError as exc:
            error = exc
    raise error


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


def _connection_class(req: urllib.request.Request, direct: type, proxied: type) -> type:
    if not (req.has_proxy() or req._tunnel_host):  # as urllib's do_open reads it
        return direct
    target = urllib.parse.urlsplit(req.full_url)
    # A name only the proxy can resolve (common on company networks) goes through.
    with contextlib.suppress(socket.gaierror):
        _public_addresses(target.hostname or "", target.port or 80)
    return proxied


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_connection_class(req, _PinnedHTTP, http.client.HTTPConnection), req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(
            _connection_class(req, _PinnedHTTPS, http.client.HTTPSConnection), req, context=self._context
        )


def tls_context() -> ssl.SSLContext:
    """Windows' certificates plus Mozilla's roots, for convert_url and the update check.

    Windows holds only some roots until its own code asks for the rest, which
    Python's never does, so a site can fail verification here and work in a
    browser. Windows' store stays in: a company's inspecting proxy puts its root
    there, and Mozilla's list won't have it."""
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    context.set_alpn_protocols(["http/1.1"])  # http.client does this only for a context of its own
    return context


def selftest() -> bool:
    """Mozilla's roots are a data file, which a frozen build can lose."""
    try:
        tls_context()
    except OSError:
        return False
    return True


def opener() -> urllib.request.OpenerDirector:
    """An opener for http(s) only. It is built by hand because build_opener
    always adds ftp, file and data handlers, and a redirect can reach ftp."""
    director = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        _HTTPHandler(),
        _HTTPSHandler(context=tls_context()),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        director.add_handler(handler)
    return director
