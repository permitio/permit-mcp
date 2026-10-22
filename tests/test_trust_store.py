"""The permit SDK's aiohttp verifies with the context server.py builds.

CVE-2026-111943, PER-17672.
"""

import asyncio
import datetime
import hashlib
import ipaddress
import os
import ssl
from pathlib import Path
from typing import NamedTuple

import aiohttp
import aiohttp.connector
import certifi
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from permit_mcp import server

COMPROMISED_ROOTS = {"emSign ECC Root CA - G3", "AAA Certificate Services"}


class Pki(NamedTuple):
    ca_file: Path
    ca_dir: Path
    server_context: ssl.SSLContext


def _subjects(context: ssl.SSLContext) -> list[str]:
    return [
        dict(field[0] for field in cert["subject"]).get("commonName", "")
        for cert in context.get_ca_certs()
    ]


@pytest.fixture
def no_cert_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


def test_importing_the_server_pins_aiohttps_default() -> None:
    assert isinstance(aiohttp.connector._SSL_CONTEXT_VERIFIED, ssl.SSLContext)
    assert not COMPROMISED_ROOTS & set(_subjects(aiohttp.connector._SSL_CONTEXT_VERIFIED))


@pytest.mark.usefixtures("no_cert_env")
def test_by_default_only_certifi_is_trusted() -> None:
    # Exactly certifi's roots: neither OpenSSL's default CA file nor (on Windows) the system stores.
    certifi_only = ssl.create_default_context(cafile=certifi.where())
    context = server._verified_context()
    assert sorted(_subjects(context)) == sorted(_subjects(certifi_only))
    assert not COMPROMISED_ROOTS & set(_subjects(context))


def test_ssl_cert_file_wins_as_in_httpx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "one.pem"
    certifi_context = ssl.create_default_context(cafile=certifi.where())
    pem = ssl.DER_cert_to_PEM_cert(certifi_context.get_ca_certs(True)[0])
    bundle.write_text(pem)
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    assert len(server._verified_context().get_ca_certs()) == 1


def test_ssl_cert_dir_is_used_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty directory: nothing preloaded, so no default CA file (or certifi) came with it.
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    assert server._verified_context().get_ca_certs() == []


@pytest.mark.usefixtures("no_cert_env")
def test_the_server_does_not_touch_the_environment() -> None:
    server._pin_aiohttp_trust_store()
    assert "SSL_CERT_FILE" not in os.environ
    assert "SSL_CERT_DIR" not in os.environ


def test_a_missing_aiohttp_default_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(aiohttp.connector, "_SSL_CONTEXT_VERIFIED")
    with pytest.raises(RuntimeError, match="_SSL_CONTEXT_VERIFIED is gone"):
        server._pin_aiohttp_trust_store()


# End to end: TLS handshakes against a local server whose CA is in neither certifi nor the
# platform store. get_ca_certs() can't see a hashed directory (OpenSSL reads it lazily), and
# only a handshake shows which context aiohttp actually uses.

_CA_NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pin-test-ca")])


def _openssl_subject_hash(name: x509.Name) -> str:
    # X509_NAME_hash: SHA-1 of the DER name without its outer SEQUENCE header, first four bytes
    # little-endian. Exact for a short name made of one lowercase UTF8String, like _CA_NAME.
    digest = hashlib.sha1(name.public_bytes()[2:], usedforsecurity=False).digest()
    return f"{int.from_bytes(digest[:4], 'little'):08x}"


def _certificate(
    subject: x509.Name,
    public_key: ec.EllipticCurvePublicKey,
    ca_key: ec.EllipticCurvePrivateKey,
    extensions: list[tuple[x509.ExtensionType, bool]],
) -> x509.Certificate:
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(_CA_NAME)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
    )
    for extension, critical in extensions:
        builder = builder.add_extension(extension, critical=critical)
    return builder.sign(ca_key, hashes.SHA256())


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    directory = tmp_path_factory.mktemp("pki")
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_usage = x509.KeyUsage(
        digital_signature=False,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=True,
        encipher_only=False,
        decipher_only=False,
    )
    ca = _certificate(
        _CA_NAME,
        ca_key.public_key(),
        ca_key,
        [
            (x509.BasicConstraints(ca=True, path_length=None), True),
            (ca_usage, True),
            (x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False),
        ],
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _certificate(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]),
        leaf_key.public_key(),
        ca_key,
        [
            (
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                False,
            ),
            (x509.BasicConstraints(ca=False, path_length=None), True),
            (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False),
        ],
    )
    pem = serialization.Encoding.PEM
    ca_file = directory / "ca.pem"
    ca_file.write_bytes(ca.public_bytes(pem))
    ca_dir = directory / "hashed"
    ca_dir.mkdir()
    (ca_dir / f"{_openssl_subject_hash(_CA_NAME)}.0").write_bytes(ca.public_bytes(pem))
    leaf_file = directory / "leaf.pem"
    leaf_file.write_bytes(
        leaf.public_bytes(pem)
        + leaf_key.private_bytes(
            pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(leaf_file)
    return Pki(ca_file, ca_dir, server_context)


@pytest.fixture
def clean_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tests re-pin aiohttp's default from their own environment; put it back afterwards.
    monkeypatch.setattr(
        aiohttp.connector, "_SSL_CONTEXT_VERIFIED", aiohttp.connector._SSL_CONTEXT_VERIFIED
    )
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


async def _aiohttp_get(server_context: ssl.SSLContext) -> int:
    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    tls_server = await asyncio.start_server(respond, "127.0.0.1", 0, ssl=server_context)
    port = tls_server.sockets[0].getsockname()[1]
    # No ssl= and no connector, as in the permit SDK's sessions.
    async with tls_server, aiohttp.ClientSession() as session:
        async with session.get(f"https://127.0.0.1:{port}/") as response:
            return response.status


def _status_after_pinning(pki: Pki) -> int:
    server._pin_aiohttp_trust_store()
    return asyncio.run(_aiohttp_get(pki.server_context))


@pytest.mark.usefixtures("clean_pin")
def test_aiohttp_rejects_a_ca_outside_certifi(pki: Pki) -> None:
    with pytest.raises(aiohttp.ClientConnectorCertificateError):
        _status_after_pinning(pki)


@pytest.mark.usefixtures("clean_pin")
@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_aiohttp_trusts_the_ca_the_variable_names(
    pki: Pki, variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    location = pki.ca_file if variable == "SSL_CERT_FILE" else pki.ca_dir
    monkeypatch.setenv(variable, str(location))
    # Only the pinned context trusts the test CA, so a 200 shows aiohttp verifies with it.
    assert _status_after_pinning(pki) == 200


@pytest.mark.usefixtures("clean_pin")
def test_ssl_cert_dir_is_not_loaded_when_ssl_cert_file_is_set(
    pki: Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OpenSSL's default directory is SSL_CERT_DIR when it is set, so this also fails if the
    # context loads the platform's default paths on top of the chosen file.
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    monkeypatch.setenv("SSL_CERT_DIR", str(pki.ca_dir))
    with pytest.raises(aiohttp.ClientConnectorCertificateError):
        _status_after_pinning(pki)
