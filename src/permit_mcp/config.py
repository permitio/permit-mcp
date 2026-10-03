"""Configuration of the Permit MCP server: `Settings`, read from code or the environment."""

import dataclasses
import os
import urllib.parse
from collections.abc import Mapping
from typing import Self

from permit_mcp._log import get_logger, redact

logger = get_logger(__name__)

DEFAULT_API_URL = "https://api.permit.io"
DEFAULT_PDP_URL = "https://cloudpdp.api.permit.io"

ENV_VARS: Mapping[str, str] = {
    "api_key": "PERMIT_API_KEY",
    "resource": "PERMIT_RESOURCE",
    "tenant": "PERMIT_TENANT",
    "api_url": "PERMIT_API_URL",
    "pdp_url": "PERMIT_PDP_URL",
    "access_request_element": "PERMIT_ACCESS_REQUEST_ELEMENT",
    "operation_approval_element": "PERMIT_OPERATION_APPROVAL_ELEMENT",
    "user": "PERMIT_MCP_USER",
}

# The 0.1 variables, which are no longer read, and the setting that replaced each.
# None: no replacement, the value now comes from the API key.
_LEGACY_VARS: Mapping[str, str | None] = {
    "TENANT": "tenant",
    "RESOURCE_KEY": "resource",
    "PROJECT_ID": None,
    "ENV_ID": None,
    "ACCESS_ELEMENTS_CONFIG_ID": "access_request_element",
    "OPERATION_ELEMENTS_CONFIG_ID": "operation_approval_element",
}


class ConfigError(Exception):
    """The configuration is missing a value or has an invalid one.

    The message names the setting, its environment variable, and how to fix it.
    """


@dataclasses.dataclass(frozen=True, kw_only=True)
class Settings:
    """What the server needs to reach Permit and which objects its tools manage.

    The project and environment are not settings: the server asks the Permit API which
    ones the API key belongs to. Construction validates every value and registers the API
    key for redaction, so the key never appears in a log record or an error message. The
    key is left out of `repr()` too.

    Attributes:
        api_key: An environment-level Permit API key (`PERMIT_API_KEY`).
        resource: Key of the resource type the tools manage, such as "documents"
            (`PERMIT_RESOURCE`).
        tenant: Key of the tenant the tools work in (`PERMIT_TENANT`).
        api_url: Base URL of the Permit API (`PERMIT_API_URL`).
        pdp_url: Base URL of the Permit PDP that answers permission checks
            (`PERMIT_PDP_URL`): the cloud PDP by default, or a container PDP.
        access_request_element: ID or key of the User Management element whose access
            requests the access-request tools manage (`PERMIT_ACCESS_REQUEST_ELEMENT`).
            When unset, those tools are not registered.
        operation_approval_element: ID or key of the Approval Management element whose
            operation approvals the operation-approval tools manage
            (`PERMIT_OPERATION_APPROVAL_ELEMENT`). When unset, those tools are not registered.
        user: Key of the Permit user that the `permit-mcp` command acts as
            (`PERMIT_MCP_USER`). Every tool call is made as this user.

    """

    api_key: str = dataclasses.field(repr=False)
    resource: str
    tenant: str = "default"
    api_url: str = DEFAULT_API_URL
    pdp_url: str = DEFAULT_PDP_URL
    access_request_element: str | None = None
    operation_approval_element: str | None = None
    user: str | None = None

    def __post_init__(self) -> None:
        """Validate every value and normalize the URLs.

        Raises:
            ConfigError: A value is empty or invalid, or neither element is set. The message
                never contains the value of a URL, which could hold a secret.

        """
        redact(self.api_key)
        for name in ("api_key", "resource", "tenant"):
            if not getattr(self, name).strip():
                msg = f"{_describe(name)} is empty. Set it to a non-empty value."
                raise ConfigError(msg)
        for name in ("access_request_element", "operation_approval_element", "user"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                msg = (
                    f"{_describe(name)} is empty. Set it to a non-empty value, or leave it "
                    "unset (None)."
                )
                raise ConfigError(msg)
        object.__setattr__(self, "api_url", _validate_url("api_url", self.api_url, DEFAULT_API_URL))
        object.__setattr__(self, "pdp_url", _validate_url("pdp_url", self.pdp_url, DEFAULT_PDP_URL))
        if self.access_request_element is None and self.operation_approval_element is None:
            msg = (
                f"Neither {ENV_VARS['access_request_element']} nor "
                f"{ENV_VARS['operation_approval_element']} is set, so no access-request or "
                "operation-approval tool would be available. Set one or both to the ID or key "
                "of the Permit Elements configuration (User Management element for access "
                "requests, Approval Management element for operation approvals)."
            )
            raise ConfigError(msg)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: str | None) -> Self:
        """Build settings from the environment, with values passed in code taking precedence.

        A value passed in `overrides` that is not None wins over the environment. An
        environment variable that is empty or only whitespace counts as unset, and the
        others are used without surrounding whitespace. The 0.1 variables (TENANT,
        RESOURCE_KEY, PROJECT_ID, ENV_ID, ACCESS_ELEMENTS_CONFIG_ID,
        OPERATION_ELEMENTS_CONFIG_ID) are not read: when one is set and its replacement is
        not, one warning names the replacement.

        Args:
            environ: The environment to read; `os.environ` when None.
            **overrides: Settings by attribute name, such as `resource="documents"`.

        Returns:
            The validated settings.

        Raises:
            ConfigError: PERMIT_API_KEY or PERMIT_RESOURCE is missing, or a value is invalid.
            TypeError: An override names no setting.

        """
        env = os.environ if environ is None else environ
        unknown = sorted(set(overrides) - set(ENV_VARS))
        if unknown:
            msg = f"Unknown setting(s) {', '.join(unknown)}; valid names: {', '.join(ENV_VARS)}"
            raise TypeError(msg)
        values: dict[str, str] = {}
        for name, variable in ENV_VARS.items():
            override = overrides.get(name)
            if override is not None:
                values[name] = override
            elif (from_env := env.get(variable, "").strip()) != "":
                values[name] = from_env
        _warn_about_legacy_vars(env, values)
        for name in ("api_key", "resource"):
            if name not in values:
                msg = (
                    f"{ENV_VARS[name]} is not set. Set the environment variable, or pass "
                    f"{name}= in code."
                )
                raise ConfigError(msg)
        return cls(**values)


def _describe(name: str) -> str:
    return f"{name} ({ENV_VARS[name]})"


def _validate_url(name: str, value: str, example: str) -> str:
    """Return `value` without surrounding whitespace and trailing slashes.

    Raises:
        ConfigError: `value` is not an absolute http(s) URL with a host and a valid port, or
            it has credentials, a query or a fragment. The message describes the problem
            without the value, which could hold a secret.

    """
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        problem = f"is not an absolute http or https URL such as {example}"
    elif "@" in parsed.netloc:
        problem = "contains credentials (user:password@); remove them"
    elif parsed.query or parsed.fragment:
        problem = "has a query or fragment; remove it, the server appends API paths to this URL"
    elif not _has_valid_port(parsed):
        problem = "has an invalid port; use a number from 0 to 65535, or none"
    else:
        return value.strip().rstrip("/")
    msg = f"{_describe(name)} {problem}."
    raise ConfigError(msg)


def _has_valid_port(parsed: urllib.parse.SplitResult) -> bool:
    try:
        _ = parsed.port
    except ValueError:
        return False
    return True


def _warn_about_legacy_vars(env: Mapping[str, str], values: Mapping[str, str]) -> None:
    """Log one warning listing every 0.1 variable that is set and not replaced."""
    notes: list[str] = []
    for legacy, replacement in _LEGACY_VARS.items():
        if not env.get(legacy, "").strip():
            continue
        if replacement is None:
            notes.append(
                f"{legacy} is no longer needed: the project and environment come from the API key"
            )
        elif replacement not in values:
            notes.append(f"{legacy} is no longer read: set {ENV_VARS[replacement]} instead")
    if notes:
        logger.warning("Ignoring permit-mcp 0.1 configuration: %s.", "; ".join(notes))
