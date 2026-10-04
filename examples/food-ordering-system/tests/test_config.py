"""The server reads its configuration at startup and refuses to start without a JWT secret."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import uvicorn
from food_ordering import app
from food_ordering.config import DEFAULT_GEMINI_MODEL, Config, ConfigError

COMPLETE = {
    "PERMIT_API_KEY": "permit_key_EXAMPLETESTS",
    "PERMIT_RESOURCE": "restaurants",
    "PERMIT_ACCESS_REQUEST_ELEMENT": "restaurant-requests",
    "PERMIT_OPERATION_APPROVAL_ELEMENT": "dish-requests",
    "FOOD_ORDERING_JWT_SECRET": "s" * 32,
    "GEMINI_API_KEY": "gemini-key",
}


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record the server uvicorn would start, instead of listening."""
    started: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda _app, **kwargs: started.append(kwargs))
    return started


def run_main(monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]) -> None:
    set_environment(monkeypatch, environ)
    app.main([])


@pytest.mark.parametrize("secret", [None, "", "   "], ids=["unset", "empty", "blank"])
def test_the_server_refuses_to_start_without_a_jwt_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    served: list[dict[str, Any]],
    secret: str | None,
) -> None:
    environ = {**COMPLETE}
    del environ["FOOD_ORDERING_JWT_SECRET"]
    if secret is not None:
        environ["FOOD_ORDERING_JWT_SECRET"] = secret
    with pytest.raises(SystemExit) as exited:
        run_main(monkeypatch, environ)
    assert exited.value.code == 2
    assert "FOOD_ORDERING_JWT_SECRET is not set" in capsys.readouterr().err
    assert served == []


def test_the_server_refuses_a_short_jwt_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    served: list[dict[str, Any]],
) -> None:
    with pytest.raises(SystemExit) as exited:
        run_main(monkeypatch, {**COMPLETE, "FOOD_ORDERING_JWT_SECRET": "s" * 31})
    assert exited.value.code == 2
    assert "shorter than 32 bytes" in capsys.readouterr().err
    assert served == []


@pytest.mark.parametrize("missing", ["PERMIT_API_KEY", "GEMINI_API_KEY"])
def test_the_server_refuses_to_start_without_the_other_keys(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    served: list[dict[str, Any]],
    missing: str,
) -> None:
    environ = {name: value for name, value in COMPLETE.items() if name != missing}
    with pytest.raises(SystemExit) as exited:
        run_main(monkeypatch, environ)
    assert exited.value.code == 2
    assert f"{missing} is not set" in capsys.readouterr().err
    assert served == []


def test_the_server_starts_with_a_complete_configuration(
    monkeypatch: pytest.MonkeyPatch, served: list[dict[str, Any]]
) -> None:
    run_main(monkeypatch, COMPLETE)
    assert served == [{"host": "127.0.0.1", "port": 8000}]


def set_environment(monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]) -> None:
    for name, value in environ.items():
        monkeypatch.setenv(name, value)


def test_the_configuration_reads_every_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(
        monkeypatch,
        {
            **COMPLETE,
            "FOOD_ORDERING_DB": "family.db",
            "GEMINI_MODEL": "gemini-other",
            "PERMIT_TENANT": "home",
        },
    )
    config = Config.from_env()
    assert config.jwt_secret == "s" * 32
    assert config.db_path == Path("family.db")
    assert config.gemini_model == "gemini-other"
    assert config.permit.resource == "restaurants"
    assert config.permit.tenant == "home"
    assert "s" * 32 not in repr(config)
    assert "gemini-key" not in repr(config)


def test_the_configuration_has_defaults_for_the_database_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_environment(monkeypatch, COMPLETE)
    config = Config.from_env()
    assert config.db_path == Path("food_ordering.db")
    assert config.gemini_model == DEFAULT_GEMINI_MODEL


def test_the_jwt_secret_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(
        monkeypatch, {name: value for name, value in COMPLETE.items() if "JWT" not in name}
    )
    with pytest.raises(ConfigError, match="FOOD_ORDERING_JWT_SECRET is not set"):
        Config.from_env()
