---
name: permit-mcp-1-migration
description: Move a project from the permit-mcp 0.1 server (Permit.io's Access Request MCP server, run from a clone) to permit-mcp 1.0 (`uvx permit-mcp`). Use when upgrading permit-mcp or its MCP client config; when PermitServer or mcp.server.fastmcp imports break; when the server warns about RESOURCE_KEY or TENANT or needs PERMIT_MCP_USER; or when tools reject a user_id.
---

# permit-mcp 0.1 to 1.0

Scan the project, settle who the acting user is, make the edits, bring every decision to the
user, then prove the result. The
[upgrade guide](https://github.com/permitio/permit-mcp/blob/main/docs/upgrade-to-1.0.md) and
the [changelog](https://github.com/permitio/permit-mcp/blob/main/CHANGELOG.md) are the source
of truth; the
[README](https://github.com/permitio/permit-mcp/blob/main/README.md#embedding-the-tools) has
complete embedding examples.

The main change: in 0.1 every tool took a `user_id` argument that the model filled in. In 1.0
the acting user is bound in code, and no tool takes a user argument. The `permit-mcp` command
acts as `PERMIT_MCP_USER`; a host that embeds the tools passes an identity resolver.

Bundled resource: `scripts/scan.py`, a read-only scanner. Standard library only, Python 3.10 or
later. It reports each affected site as `path:line: ID SAFETY message`; each ID has a section
under [Changes](#3-changes) below.

## Rules

1. **Never choose the acting user yourself.** The Permit user key for `PERMIT_MCP_USER`, or how
   a host identifies its callers, comes from the user. Don't take it from a `user_id` in a
   prompt, a test or the old config.
2. **Never print or write a secret where it doesn't belong.** Don't echo `PERMIT_API_KEY`.
   When its value has to move (from `.env` into a client's `env` block), say which file it goes
   to and let the user confirm. Never add it to a file that is committed.
3. **Don't call Permit without the user's approval.** Starting the server with stdin closed
   (step 5) is fine; a tool call reaches the Permit API with the user's key.
4. **The 0.1 variable names are generic.** `TENANT`, `PROJECT_ID`, `ENV_ID` and
   `RESOURCE_KEY` can belong to anything. Confirm that a variable is the permit-mcp setting
   before renaming or deleting it. The scanner marks them SAFE only next to `PERMIT_API_KEY`,
   in a file that names permit-mcp, or in the `env` block of a permit-mcp server.

## 1. Scan

Check the Python first: 1.0 runs on Python 3.11 to 3.14, and 0.1 allowed 3.10. If the project
or its host runs on 3.10, stop and tell the user that moving to 3.11 comes first.

```bash
python3 <this skill's directory>/scripts/scan.py <project root>
```

Scan the MCP client configs that live outside the project too, one file at a time:

- Claude Desktop: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS),
  `%APPDATA%\Claude\claude_desktop_config.json` (Windows).
- Cursor `~/.cursor/mcp.json`, Claude Code `~/.claude.json`, and any other client config the
  user names. Configs inside the project (`.mcp.json`, `.cursor/mcp.json`, `.vscode/mcp.json`)
  are part of the project scan.

Exit status:

- **0**: nothing found. It prints `No permit-mcp 0.1 usage found.`
- **1**: findings, one per line.
- **2**: the scan is incomplete: the path does not exist, a directory or file could not be
  read, a file is not a regular file or is larger than 5 MB, or a file could not be parsed (a
  Python file with a syntax error, a JSON file with comments that mentions permit). stderr
  names each one; review those by hand with the sections below. Never treat 2 as a clean
  result.
- Anything else, or a traceback, is a scanner bug: review the project by hand with the
  sections below, and say so in the report.

`SAFE` findings can be edited as the message says. `NEEDS-REVIEW` findings need the user's
decision (step 4). Group the findings by ID and read each ID's section before editing.

A copy of the 0.1 server inside the project is reported once, as M3; don't edit it.

## 2. Decide who the acting user is

Ask the user before editing, and show this table:

| How the project runs permit-mcp | Bind the acting user with |
| --- | --- |
| An MCP client (Claude Desktop, Cursor and the like) starts `permit-mcp` | `PERMIT_MCP_USER` in the client's `env` block. One process serves one person. |
| An HTTP host with MCP authentication (a `TokenVerifier`) | `access_token_subject()`: the access token's `subject` is the Permit user key. |
| A host that knows its signed-in user another way (a session, a websocket) | Its own identity resolver: an async function that returns the caller's Permit user key. |
| A host that serves one fixed user | `bound_user("<key>")`, or `create_server()` without `identity=`, which acts as `PERMIT_MCP_USER`. |

A 0.1 host that put the signed-in user's `user_id` into a system prompt (U2) or into tool
arguments (U1) needs a resolver that returns that same user from the host's own session.

## 3. Changes

### D1: permit-mcp 0.1 requirement

SAFE. Replace the clone, Git or path reference, or the 0.x pin with `permit-mcp>=1.0,<2`, and
remove a `[tool.uv.sources]` entry that points at the clone. Regenerate the lock file with the
project's tool (`uv lock`, `poetry lock`, `pip-compile`).

### D2: mcp below 2

NEEDS-REVIEW. permit-mcp 1.0 requires mcp 2 (`mcp>=2.2.0,<3`). Raising the pin moves the
project's own MCP code to mcp 2 as well (M2, and the MCP SDK's
[migration guide](https://py.sdk.modelcontextprotocol.io/v2/migration/)). Tell the user how much
of their code uses mcp before raising it.

### E1: 0.1 variable, renamed or removed

| 0.1 | 1.0 |
| --- | --- |
| `RESOURCE_KEY` | `PERMIT_RESOURCE` |
| `TENANT` | `PERMIT_TENANT` |
| `ACCESS_ELEMENTS_CONFIG_ID` | `PERMIT_ACCESS_REQUEST_ELEMENT` |
| `OPERATION_ELEMENTS_CONFIG_ID` | `PERMIT_OPERATION_APPROVAL_ELEMENT` |
| `PROJECT_ID`, `ENV_ID` | Removed: delete them. The server reads the project and environment from the API key. |

`PERMIT_API_KEY` and `PERMIT_PDP_URL` keep their names. The API key must be an
environment-level key; 1.0 refuses an organization- or project-level key. 1.0 does not read the
0.1 names; when one is still set, it logs a warning that names the replacement.

- SAFE where the file is surely about permit-mcp (rule 4): rename or delete it.
- NEEDS-REVIEW anywhere else, with a message that starts "If this belongs to permit-mcp".
  Confirm with the user first (rule 4).
- NEEDS-REVIEW in code that reads it (`os.getenv("TENANT")`). If the host reads the variable
  for its own use as well, rename it in both places, or read the settings with
  `permit_mcp.Settings.from_env()`.

At least one of the two element variables must be set. The tools of an element that is not set
are not registered.

### E2: the .env file is no longer loaded

NEEDS-REVIEW. 0.1 loaded `.env` from its working directory; 1.0 does not. Pick one with the
user:

- move the variables, with their 1.0 names, into the MCP client's `env` block (C1);
- start the server with `uv run --env-file .env permit-mcp`;
- for a host, load the file itself before `Settings.from_env()`, as it already does for its own
  settings.

Rename the variables in `.env.example` too, and add `PERMIT_MCP_USER`.

### E3: PERMIT_MCP_USER missing

NEEDS-REVIEW. A client config runs `permit-mcp` without `PERMIT_MCP_USER` in its `env` block.
The 1.0 command stops with exit status 2 without it. Ask the user for the Permit user key
(rule 1) and add it. An entry that runs `python -m permit_mcp` with the Python of a checkout
can be 0.1 or 1.0: check the checkout's `pyproject.toml` version, and treat 0.1 as C1.

### C1: the 0.1 server run from a clone

NEEDS-REVIEW: the new `env` block needs values from the clone's `.env` and the acting user.

Before:

```json
{
  "mcpServers": {
    "permit": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/PARENT/FOLDER/src/permit_mcp", "run", "server.py"]
    }
  }
}
```

After:

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

Take the values from the clone's `.env` with the E1 names, drop `PROJECT_ID` and `ENV_ID`, and
keep `PERMIT_TENANT` and `PERMIT_PDP_URL` only when they differ from the defaults (`default`,
`https://cloudpdp.api.permit.io`). The same applies to code that starts the server, such as
`StdioServerParameters(command="uvx", args=["permit-mcp"], env={...})`, and to scripts and docs
that show the old command. To keep running from a clone instead, use
`uv run --directory /ABSOLUTE/PATH/TO/permit-mcp permit-mcp` with the same `env` block.

### M1: PermitServer

NEEDS-REVIEW: the identity comes from step 2. `PermitServer(mcp, exclude_tools=[...])` is
gone. Build the tools, register them on an `MCPServer`, and close their HTTP session in the
server's lifespan.

Before:

<!-- docs-check: skip, 0.1 code -->
```python
from mcp.server.fastmcp import FastMCP
from permit_mcp import PermitServer

mcp = FastMCP("my-app")
PermitServer(mcp, exclude_tools=["create_access_request", "create_operation_approval"])
```

After:

```python
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from permit_mcp import IdentityError, PermitTools, Settings


async def resolve(ctx: Context[Any, Any], /) -> str:
    """Return the caller's Permit user key from the host's own session."""
    raise IdentityError("replace with the host's lookup of the signed-in user")


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

Or let `create_server(settings, identity=resolve, exclude_tools={...})` build the server with
the lifespan included. Without `identity=`, it acts as `PERMIT_MCP_USER`. Use
`access_token_subject()` or `bound_user("<key>")` in place of `resolve` when step 2 chose them.

Also:

- `exclude` takes a collection of names (a list works); an unknown name raises
  `ValueError`. `TOOL_NAMES` lists them.
- `check_permission`, `cancel_access_request` and `cancel_operation_approval` are new and
  registered by default. Ask whether the application should offer them; exclude them if not.
- 0.1 set each tool as an attribute of the `PermitServer` (`permit_server.create_access_request`).
  1.0 has no such attributes; call the tools through MCP.

### M2: mcp.server.fastmcp

NEEDS-REVIEW. mcp 2, which 1.0 requires, has no `mcp.server.fastmcp`.

| mcp 1 | mcp 2 |
| --- | --- |
| `from mcp.server.fastmcp import FastMCP` | `from mcp.server.mcpserver import MCPServer` |
| `from mcp.server.fastmcp import Context` | `from mcp.server.mcpserver import Context` |
| `from mcp.server.fastmcp.exceptions import ToolError` | `from mcp.server.mcpserver.exceptions import ToolError` |
| `mcp.run(transport="stdio")` | `server.run("stdio")` |

`@server.tool()` works as before. For anything else, such as settings passed to `FastMCP(...)`,
follow the MCP SDK's [migration guide](https://py.sdk.modelcontextprotocol.io/v2/migration/).

### M3: a copy of the 0.1 server in the project

NEEDS-REVIEW. A module or package named `permit_mcp` that defines or imports
`PermitServer`, such as a `permit_mcp.py` kept next to the app or a vendored
`src/permit_mcp/`, is a copy of the 0.1 server. On the import path it shadows the installed
permit-mcp 1.0, so `from permit_mcp import PermitTools` fails or imports the copy. Don't
edit it: with the user's agreement, delete it (or rename it if something else still needs
it) before installing 1.0. The scanner reports nothing else inside it.

### U1: user_id passed to a Permit tool

NEEDS-REVIEW. Remove the `user_id` argument. The call acts as the user bound in code (step 2).
Code that passed a different `user_id` per call needs a resolver that returns that user.

The other argument changes, from the changelog:

- `access_request_id` and `operation_approval_id` must be UUIDs, as the list tools return
  them. Other values are refused before anything is sent.
- `resource_instance` is a string; 0.1 also took an integer.
- `page` is at least 1, `per_page` is 1 to 100, and `status` is one of `pending`, `approved`,
  `denied` or `canceled`. `list_resource_instances` returns 30 results per page by default, not
  100.
- Results are JSON objects. Create, approve, deny and cancel return `status` and the request,
  not a sentence such as `Your request has been successfully sent`; update code that checks
  the old text. The list tools return Permit's paginated object as it is, without the
  `requesting_user` 0.1 added to each item; code that read it reads Permit's
  `requesting_user_email`, `requesting_user_first_name` and `requesting_user_last_name`
  instead.

The tool names are the same as in 0.1.

### U2: text for the model that mentions user_id

NEEDS-REVIEW. A system prompt or template tells the model the user's `user_id` so that it would
pass it to the Permit tools. Remove that part: the 1.0 tools have no user argument, and the
model cannot change the acting user. Keep the rest of the prompt. If the host's own tools still
take a `user_id` from the model, tell the user; that is outside this migration.

## 4. Bring the NEEDS-REVIEW items to the user

Don't guess. For each one, show `path:line`, the line (without secret values), what changed in
1.0 and the recommendation from its section. Group the sites that share one decision: the
acting user (C1, E3, M1, U1, U2), the `.env` (E2), the generic variable names (E1), the
mcp 2 move (D2, M2), and the 0.1 copies to delete (M3). Apply what the user approves and
list the rest in the report.

## 5. Verify

1. Re-scan. It should exit 0, or report only the items the user chose to keep.
2. Install and check the version:
   `python -c "import permit_mcp; print(permit_mcp.__version__)"` prints 1.x.
3. Run the project's tests, type checker and linter the way the project runs them.
4. Start the server with its configuration and stdin closed. It checks the settings and exits
   0 at the end of stdin, without calling Permit (uvx may still download the package):

   ```bash
   uvx permit-mcp < /dev/null
   ```

   Pass the client's `env` values in the environment, or use
   `uv run --env-file .env permit-mcp < /dev/null`. Exit status 2 prints the setting to fix.
   A warning that starts with `Ignoring permit-mcp 0.1 configuration` names a 0.1 variable that
   is still set (E1).
5. For a host, start it and check the names `tools.register()` returns: the tools the user
   wanted, and `aclose()` reached on shutdown.
6. With the user's approval (rule 3), restart the MCP client and call `list_resource_instances`.
   The Permit tools' schemas have no `user_id` argument.

## 6. Report

- **Acting user:** how it is bound now, and who chose it.
- **Changed:** files and edits, grouped by ID.
- **Needs a decision:** each remaining NEEDS-REVIEW item, with its recommendation.
- **Verified:** each command run and its result. Say what could not run and why.

## What the scanner can miss

It reads files one at a time and does not run them, so it misses:

- Values it cannot see: a variable name or server path built at runtime, or a `user_id` that
  reaches a tool through a variable holding the arguments.
- Prompts that name the user without the word `user_id`, and Python strings or `.md` and
  `.txt` lines that mention `user_id` without the word "tool" or "function".
- Client configs outside the scanned path, JSON with comments that doesn't mention permit, and
  TOML client configs (only their `src/permit_mcp` paths are found).
- Settings outside the repository: environment variables set in shells, CI secrets and
  deployment platforms.

Step 5 is the check that catches what the scan misses.
