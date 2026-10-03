"""Settings: environment loading, precedence, validation and secrecy of the API key."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from permit_mcp import ConfigError, Settings
from tests.support import API_KEY

if TYPE_CHECKING:
    from tests.conftest import RecordList

BASE_ENV = {
    "PERMIT_API_KEY": API_KEY,
    "PERMIT_RESOURCE": "documents",
    "PERMIT_ACCESS_REQUEST_ELEMENT": "ar-elem",
}
FULL_ENV = {
    **BASE_ENV,
    "PERMIT_TENANT": "acme",
    "PERMIT_API_URL": "https://api.example.test",
    "PERMIT_OPERATION_APPROVAL_ELEMENT": "oa-elem",
    "PERMIT_MCP_USER": "alice",
}


def test_from_env_reads_every_variable() -> None:
    settings = Settings.from_env(FULL_ENV)

    assert settings == Settings(
        api_key=API_KEY,
        resource="documents",
        tenant="acme",
        api_url="https://api.example.test",
        access_request_element="ar-elem",
        operation_approval_element="oa-elem",
        user="alice",
    )


def test_defaults_apply_when_optional_variables_are_unset() -> None:
    settings = Settings.from_env(BASE_ENV)

    assert settings.tenant == "default"
    assert settings.api_url == "https://api.permit.io"
    assert settings.operation_approval_element is None
    assert settings.user is None


def test_from_env_reads_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in FULL_ENV.items():
        monkeypatch.setenv(name, value)

    assert Settings.from_env() == Settings.from_env(FULL_ENV)


def test_override_beats_the_environment() -> None:
    settings = Settings.from_env(FULL_ENV, tenant="from-code", user="carol")

    assert settings.tenant == "from-code"
    assert settings.user == "carol"


def test_none_override_keeps_the_environment_value() -> None:
    settings = Settings.from_env(FULL_ENV, tenant=None, user=None)

    assert settings.tenant == "acme"
    assert settings.user == "alice"


@pytest.mark.parametrize("blank", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
def test_blank_variable_counts_as_unset(blank: str) -> None:
    settings = Settings.from_env({**BASE_ENV, "PERMIT_TENANT": blank, "PERMIT_MCP_USER": blank})

    assert settings.tenant == "default"
    assert settings.user is None


@pytest.mark.parametrize("variable", ["PERMIT_API_KEY", "PERMIT_RESOURCE"])
@pytest.mark.parametrize("value", [None, "", "  "], ids=["absent", "empty", "spaces"])
def test_missing_required_variable_is_named(variable: str, value: str | None) -> None:
    environ = {key: val for key, val in BASE_ENV.items() if key != variable}
    if value is not None:
        environ[variable] = value

    with pytest.raises(ConfigError, match=variable):
        Settings.from_env(environ)


def test_no_element_is_a_config_error() -> None:
    environ = {k: v for k, v in BASE_ENV.items() if k != "PERMIT_ACCESS_REQUEST_ELEMENT"}

    with pytest.raises(ConfigError, match="ELEMENT"):
        Settings.from_env(environ)


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("ftp://api.example.test", "is not an absolute http or https URL"),
        ("api.example.test", "is not an absolute http or https URL"),
        ("https:///v2", "is not an absolute http or https URL"),
        ("not a url", "is not an absolute http or https URL"),
        ("/v2/relative", "is not an absolute http or https URL"),
        ("https://user:hunter2-secret@api.example.test", "contains credentials"),
        ("https://hunter2-secret@api.example.test", "contains credentials"),
        ("https://api.example.test?token=hunter2-secret", "has a query or fragment"),
        ("https://api.example.test/#hunter2-secret", "has a query or fragment"),
        ("https://api.example.test:hunter2-secret", "has an invalid port"),
        ("https://api.example.test:65536", "has an invalid port"),
    ],
)
def test_bad_url_is_a_config_error_that_does_not_echo_it(url: str, problem: str) -> None:
    with pytest.raises(ConfigError) as caught:
        Settings.from_env({**BASE_ENV, "PERMIT_API_URL": url})

    message = str(caught.value)
    assert message.startswith(f"api_url (PERMIT_API_URL) {problem}")
    assert url not in message
    assert "hunter2" not in message


def test_trailing_slash_and_whitespace_are_stripped() -> None:
    settings = Settings.from_env({**BASE_ENV, "PERMIT_API_URL": " https://api.example.test:8443/ "})

    assert settings.api_url == "https://api.example.test:8443"


def test_direct_construction_validates() -> None:
    with pytest.raises(ConfigError):
        Settings(
            api_key=API_KEY,
            resource="documents",
            api_url="nope",
            access_request_element="x",
        )
    with pytest.raises(ConfigError):
        Settings(api_key="", resource="documents", access_request_element="x")
    with pytest.raises(ConfigError):
        Settings(api_key=API_KEY, resource="", access_request_element="x")
    with pytest.raises(ConfigError):
        Settings(api_key=API_KEY, resource="documents")


@pytest.mark.parametrize("name", ["access_request_element", "operation_approval_element", "user"])
def test_blank_optional_value_in_code_is_a_config_error(name: str) -> None:
    values = {"access_request_element": "ar-elem", "operation_approval_element": "oa-elem"}

    with pytest.raises(ConfigError, match=f"{name} .* is empty"):
        Settings(api_key=API_KEY, resource="documents", **{**values, name: "  "})


def test_unknown_override_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="Unknown setting\\(s\\) project_id"):
        Settings.from_env(BASE_ENV, project_id="proj")


def test_repr_and_str_hide_the_api_key() -> None:
    settings = Settings.from_env(FULL_ENV)

    assert API_KEY not in repr(settings)
    assert API_KEY not in str(settings)
    assert API_KEY not in f"{settings}"
    assert "documents" in repr(settings)


def test_config_errors_never_echo_the_api_key() -> None:
    with pytest.raises(ConfigError) as caught:
        Settings.from_env({**BASE_ENV, "PERMIT_API_URL": "nope"})

    assert API_KEY not in str(caught.value)


@pytest.mark.parametrize(
    ("legacy", "replacement"),
    [
        ("TENANT", "PERMIT_TENANT"),
        ("ACCESS_ELEMENTS_CONFIG_ID", "PERMIT_ACCESS_REQUEST_ELEMENT"),
        ("OPERATION_ELEMENTS_CONFIG_ID", "PERMIT_OPERATION_APPROVAL_ELEMENT"),
    ],
)
def test_legacy_variable_logs_one_warning_naming_its_replacement(
    permit_logs: RecordList, legacy: str, replacement: str
) -> None:
    environ = {k: v for k, v in FULL_ENV.items() if k != replacement}
    environ[legacy] = "legacy-value"

    Settings.from_env(environ)

    warnings = [r for r in permit_logs.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert replacement in warnings[0].getMessage()
    assert legacy in warnings[0].getMessage()


@pytest.mark.parametrize("legacy", ["PROJECT_ID", "ENV_ID"])
def test_legacy_project_variables_are_reported_as_unneeded(
    permit_logs: RecordList, legacy: str
) -> None:
    Settings.from_env({**BASE_ENV, legacy: "legacy-value"})

    warnings = [r for r in permit_logs.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "no longer needed" in warnings[0].getMessage()


def test_legacy_variable_is_not_read() -> None:
    settings = Settings.from_env({**BASE_ENV, "TENANT": "legacy-tenant"})

    assert settings.tenant == "default"


def test_legacy_resource_variable_does_not_replace_the_required_one() -> None:
    environ = {k: v for k, v in BASE_ENV.items() if k != "PERMIT_RESOURCE"}

    with pytest.raises(ConfigError, match="PERMIT_RESOURCE"):
        Settings.from_env({**environ, "RESOURCE_KEY": "documents"})


def test_legacy_variable_with_its_replacement_set_is_silent(
    permit_logs: RecordList,
) -> None:
    Settings.from_env({**FULL_ENV, "TENANT": "legacy-tenant"})

    assert [r for r in permit_logs.records if r.levelno >= logging.WARNING] == []
