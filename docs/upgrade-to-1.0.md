# Upgrading from 0.1 to 1.0

Version 0.1 was run from a clone of this repository. Version 1.0 is published on PyPI as
`permit-mcp`. This guide lists what to change. The [changelog](../CHANGELOG.md) lists every change.

## Who the acting user is

In 0.1, every tool took a `user_id` argument, and the model filled it in. In 1.0, the acting user
is bound in code; tools no longer take a user argument.

- The `permit-mcp` command acts as `PERMIT_MCP_USER` for every call. One process serves one
  person.
- A host that serves several people passes an identity resolver that returns each caller's Permit
  user key. See [Embedders](#embedders).

## Environment variables

| 0.1 | 1.0 |
| --- | --- |
| `PERMIT_API_KEY` | `PERMIT_API_KEY`, unchanged. It must be an environment-level key. |
| `RESOURCE_KEY` | `PERMIT_RESOURCE` |
| `TENANT` | `PERMIT_TENANT` |
| `ACCESS_ELEMENTS_CONFIG_ID` | `PERMIT_ACCESS_REQUEST_ELEMENT` |
| `OPERATION_ELEMENTS_CONFIG_ID` | `PERMIT_OPERATION_APPROVAL_ELEMENT` |
| `PERMIT_PDP_URL` | `PERMIT_PDP_URL`, unchanged. `check_permission` uses it. |
| `PROJECT_ID` | Removed. The server reads the project from the API key. |
| `ENV_ID` | Removed. The server reads the environment from the API key. |
| none | `PERMIT_MCP_USER`: the Permit user key the `permit-mcp` command acts as. Required by the command. |
| none | `PERMIT_API_URL`: the base URL of the Permit API. Optional. |

1.0 does not read the 0.1 names. When one is still set, the server logs a warning that names its
replacement (unless the replacement is set too), or says that the variable is no longer needed.

## The `.env` file

0.1 loaded a `.env` file from the working directory. 1.0 does not. Put the variables in your MCP
client's `env` block, or pass the file explicitly:

```shell
uv run --env-file .env permit-mcp
```

## Claude Desktop

Before, with 0.1 run from a clone and its settings in `.env`:

```json
{
  "mcpServers": {
    "permit": {
      "command": "uv",
      "args": [
        "--directory",
        "/ABSOLUTE/PATH/TO/PARENT/FOLDER/src/permit_mcp",
        "run",
        "server.py"
      ]
    }
  }
}
```

After, with 1.0 from PyPI:

```json
{
  "mcpServers": {
    "permit": {
      "command": "uvx",
      "args": ["permit-mcp"],
      "env": {
        "PERMIT_API_KEY": "permit_key_...",
        "PERMIT_RESOURCE": "documents",
        "PERMIT_ACCESS_REQUEST_ELEMENT": "<user-management-element-id>",
        "PERMIT_OPERATION_APPROVAL_ELEMENT": "<approval-management-element-id>",
        "PERMIT_MCP_USER": "alice"
      }
    }
  }
}
```

## Hosts that put the user in the system prompt

Some 0.1 hosts told the model who the user was in the system prompt, so that it would pass that
key as `user_id`. Remove that. 1.0 tools have no user argument, and the model cannot change the
acting user. Bind the identity in code instead: an identity resolver, or `access_token_subject()`
when your server uses the MCP SDK's authentication.

## Embedders

`PermitServer` and `FastMCP` are gone. Build an `MCPServer` (mcp 2) and register the tools on it.

Before:

<!-- docs-check: skip, 0.1 code -->
```python
from mcp.server.fastmcp import FastMCP
from permit_mcp.server import PermitServer

mcp = FastMCP("my-app")
PermitServer(mcp, exclude_tools=["create_access_request", "create_operation_approval"])
```

After:

```python
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from permit_mcp import PermitTools, Settings


async def resolve(ctx: Context[Any, Any], /) -> str:
    """Return the caller's Permit user key; raise IdentityError when there is none."""
    ...


tools = PermitTools(Settings.from_env(), resolve)


@contextlib.asynccontextmanager
async def lifespan(_server: MCPServer[None]) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await tools.aclose()


server = MCPServer("my-app", lifespan=lifespan)
tools.register(server, exclude={"create_access_request", "create_operation_approval"})
```

Or let `create_server(settings, identity=resolve, exclude_tools=...)` build the server, with the
lifespan included. The [README](../README.md#embedding-the-tools) has complete examples,
including one over HTTP with a `TokenVerifier`.

Other changes for embedders:

- An unknown name in `exclude` raises `ValueError`.
- `check_permission`, `cancel_access_request` and `cancel_operation_approval` are new. Exclude
  them if your application should not offer them.
- Tools whose element is not set are not registered.
- Tool results are JSON objects; see the [changelog](../CHANGELOG.md#api).
- Remove any `user_id` your own code passed to the tools.
