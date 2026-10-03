"""An MCP server for Permit.io access requests and operation approvals.

The access-request and operation-approval tools act as a Permit user that the server binds
in code: one fixed user for the `permit-mcp` command, or the caller a host's identity
resolver returns. list_resource_instances lists with the server's credentials, and still
requires an identified caller. No tool takes a user argument.
"""

from importlib.metadata import version

from permit_mcp.config import ConfigError, Settings
from permit_mcp.identity import (
    IdentityError,
    IdentityResolver,
    access_token_subject,
    bound_user,
)
from permit_mcp.server import create_server
from permit_mcp.tools import TOOL_NAMES, PermitTools

__version__ = version("permit-mcp")

__all__ = [
    "TOOL_NAMES",
    "ConfigError",
    "IdentityError",
    "IdentityResolver",
    "PermitTools",
    "Settings",
    "__version__",
    "access_token_subject",
    "bound_user",
    "create_server",
]
