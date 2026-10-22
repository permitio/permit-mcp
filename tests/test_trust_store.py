"""The permit SDK's aiohttp verifies with the context server.py builds (CVE-2026-111943, PER-17672)."""

import os
import ssl
from pathlib import Path

import aiohttp.connector
import certifi
import pytest

from permit_mcp import server

COMPROMISED_ROOTS = {"emSign ECC Root CA - G3", "AAA Certificate Services"}


def _subjects(context: ssl.SSLContext) -> list[str]:
    return [dict(field[0] for field in cert["subject"]).get("commonName", "") for cert in context.get_ca_certs()]


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
    pem = ssl.DER_cert_to_PEM_cert(ssl.create_default_context(cafile=certifi.where()).get_ca_certs(True)[0])
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
