"""Throwaway certificates, so tests can run real TLS on loopback with no internet."""

from __future__ import annotations

import datetime
import ipaddress
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from markdown_sidekick import netguard


def issue(name: str, signer=None, *, ca: bool = False, expired: bool = False):
    """(certificate, key) for ``name``, self-signed unless ``signer`` is another
    such pair, with the extensions strict verification (Python 3.13) insists on."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    signer_cert, signer_key = signer or (None, key)
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(signer_cert.subject if signer_cert else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=2))
        .not_valid_after(now + datetime.timedelta(days=-1 if expired else 1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer_key.public_key()), critical=False)
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
    else:
        try:
            alt_name = x509.IPAddress(ipaddress.ip_address(name))
        except ValueError:
            alt_name = x509.DNSName(name)
        builder = builder.add_extension(x509.SubjectAlternativeName([alt_name]), critical=False)
    return builder.sign(signer_key, hashes.SHA256()), key


def server_context(leaf, folder: Path) -> ssl.SSLContext:
    """A context for a test server presenting ``leaf``."""
    cert, key = leaf
    pem = folder / "server.pem"
    pem.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pem)
    return context


def use_as_mozillas_list(monkeypatch, folder: Path, root) -> None:
    """Make ``root`` the one root downloads take from certifi. Windows' store
    doesn't have it, which plays a root Windows hasn't fetched."""
    pem = folder / "mozilla.pem"
    pem.write_bytes(root[0].public_bytes(serialization.Encoding.PEM))
    monkeypatch.setattr(netguard.certifi, "where", lambda: str(pem))
