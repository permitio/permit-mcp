"""The backend's configuration, read from the environment when the server starts."""

import dataclasses
import os
from pathlib import Path
from typing import Self

from permit import Permit

from permit_mcp import Settings

JWT_SECRET_VAR = "FOOD_ORDERING_JWT_SECRET"  # noqa: S105 - the variable's name, not its value
DB_VAR = "FOOD_ORDERING_DB"
GEMINI_KEY_VAR = "GEMINI_API_KEY"
GEMINI_MODEL_VAR = "GEMINI_MODEL"

DEFAULT_DB = "food_ordering.db"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
# HS256 keys shorter than the hash output are weak; RFC 7518 section 3.2 asks for at least 256 bits.
MIN_JWT_SECRET_BYTES = 32
_RANDOM_KEY = "a random key, such as the output of `openssl rand -hex 32`"


class ConfigError(Exception):
    """A required variable is missing or invalid. The message names the variable."""


@dataclasses.dataclass(frozen=True, kw_only=True)
class Config:
    """Everything the backend needs, validated.

    Attributes:
        permit: The Permit settings, from the `PERMIT_*` variables. The MCP tools and the
            Permit SDK both use them.
        jwt_secret: The key that signs and verifies the app's JWTs
            (`FOOD_ORDERING_JWT_SECRET`), at least 32 bytes.
        db_path: The SQLite database (`FOOD_ORDERING_DB`), `food_ordering.db` by default.
        gemini_api_key: The Gemini API key (`GEMINI_API_KEY`).
        gemini_model: The Gemini model (`GEMINI_MODEL`), `gemini-3.8-flash` by default.

    """

    permit: Settings
    jwt_secret: str = dataclasses.field(repr=False)
    db_path: Path
    gemini_api_key: str = dataclasses.field(repr=False)
    gemini_model: str

    @classmethod
    def from_env(cls) -> Self:
        """Read and validate the configuration from the environment.

        The JWT secret has no default: a known key would let anyone sign in as anyone.

        Raises:
            ConfigError: The JWT secret or the Gemini API key is missing, or the secret is
                shorter than 32 bytes.
            permit_mcp.ConfigError: A `PERMIT_*` variable is missing or invalid.

        """
        jwt_secret = _required(JWT_SECRET_VAR, _RANDOM_KEY)
        if len(jwt_secret.encode()) < MIN_JWT_SECRET_BYTES:
            msg = (
                f"{JWT_SECRET_VAR} is shorter than {MIN_JWT_SECRET_BYTES} bytes. "
                f"Set it to {_RANDOM_KEY}."
            )
            raise ConfigError(msg)
        return cls(
            permit=Settings.from_env(),
            jwt_secret=jwt_secret,
            db_path=db_path_from_env(),
            gemini_api_key=_required(GEMINI_KEY_VAR, "the API key of your Gemini project"),
            gemini_model=os.environ.get(GEMINI_MODEL_VAR, "").strip() or DEFAULT_GEMINI_MODEL,
        )


def db_path_from_env() -> Path:
    """Return the SQLite database path, `food_ordering.db` when `FOOD_ORDERING_DB` is unset."""
    return Path(os.environ.get(DB_VAR, "").strip() or DEFAULT_DB)


def permit_sdk(settings: Settings) -> Permit:
    """Return the Permit SDK, configured with the settings the MCP tools use."""
    return Permit(token=settings.api_key, pdp=settings.pdp_url, api_url=settings.api_url)


def _required(variable: str, what: str) -> str:
    value = os.environ.get(variable, "").strip()
    if not value:
        msg = f"{variable} is not set. Set it to {what}."
        raise ConfigError(msg)
    return value
