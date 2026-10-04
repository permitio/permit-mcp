# Permit.io Access Request MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets an AI application work with
[Permit.io](https://www.permit.io) access requests and operation approvals. Through its tools a
model can:

- request a role on a resource, and list, approve, deny or cancel access requests;
- request a one-time approval for an operation, and list, approve, deny or cancel those requests;
- check whether the acting user may perform an action;
- list the instances of the resource type, to find the keys the other tools take.

Reviewers approve or deny in your application, in Permit's
[embeddable Elements](https://docs.permit.io/embeddable-uis/overview), or through the same tools.

Upgrading from the 0.1 server? Read the
[upgrade guide](https://github.com/permitio/permit-mcp/blob/main/docs/upgrade-to-1.0.md).

## Who the acting user is

Every tool call acts as one Permit user: the acting user. The server binds that user in code.
No tool takes a user argument, and the model cannot change the acting user: a user named in a
prompt or a tool argument has no effect.

How the server learns the acting user depends on how it runs:

| Deployment | Acting user |
| --- | --- |
| `permit-mcp` command over stdio | `PERMIT_MCP_USER`, for every call. One process serves one person. |
| HTTP host with MCP authentication | `access_token_subject()`: the `subject` of the access token the host's `TokenVerifier` verified. `create_server(auth=..., token_verifier=...)` uses it by default. |
| Host that embeds the tools | The host's own identity resolver, an async function that returns the caller's Permit user key. |

The access-request and operation-approval tools act in Permit as the acting user, so Permit
applies that user's Elements permissions: a reviewer must be allowed to review, and nobody approves
their own access request. `check_permission` asks the PDP about the acting user.
`list_resource_instances` lists with the server's API key, so its result is not filtered by the
acting user's permissions; it still fails when the caller cannot be identified.

When the caller cannot be identified, the call fails before anything is sent to Permit.

## Quick start

You need [uv](https://docs.astral.sh/uv/) and Python 3.11 to 3.14. In Permit, you need:

- an environment-level [API key](https://docs.permit.io/overview/use-the-permit-api-and-sdk#obtain-your-api-key);
- the key of the resource type the tools manage, such as `documents`;
- a [User Management element](https://docs.permit.io/embeddable-uis/element/user-management) for
  access requests, an
  [Approval Management element](https://docs.permit.io/embeddable-uis/element/approval-management)
  for operation approvals, or both;
- the key of the Permit user the server acts as.

Run the server from PyPI:

```shell
PERMIT_API_KEY=permit_key_... PERMIT_RESOURCE=documents \
PERMIT_ACCESS_REQUEST_ELEMENT=<element-id> PERMIT_MCP_USER=alice \
uvx permit-mcp
```

The server speaks MCP over stdio, so an MCP client usually starts it. For
[Claude Desktop](https://claude.ai/download), add this to `claude_desktop_config.json`:

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

The server does not read a `.env` file. Set the variables in the client's `env` block, or in the
environment of the process.

### Run from a clone

Running from a clone needs uv 0.12.19 or newer.

```shell
git clone https://github.com/permitio/permit-mcp
cd permit-mcp
uv sync --locked
cp .env.example .env    # then fill it in
uv run --env-file .env permit-mcp
```

For Claude Desktop, use `"command": "uv"` and
`"args": ["run", "--directory", "/ABSOLUTE/PATH/TO/permit-mcp", "permit-mcp"]`, with the same
`env` block.

## Configuration

The `permit-mcp` command reads these environment variables. An empty variable counts as unset.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PERMIT_API_KEY` | required | An environment-level Permit API key. The server asks Permit which project and environment the key belongs to. |
| `PERMIT_RESOURCE` | required | Key of the resource type the tools manage, such as `documents`. |
| `PERMIT_MCP_USER` | unset | Key of the Permit user every tool call acts as. The `permit-mcp` command requires it; hosts that pass an identity resolver do not need it. |
| `PERMIT_ACCESS_REQUEST_ELEMENT` | unset | ID or key of the User Management element. Set it to register the access-request tools. |
| `PERMIT_OPERATION_APPROVAL_ELEMENT` | unset | ID or key of the Approval Management element. Set it to register the operation-approval tools. |
| `PERMIT_TENANT` | `default` | Key of the tenant the tools work in. |
| `PERMIT_API_URL` | `https://api.permit.io` | Base URL of the Permit API. |
| `PERMIT_PDP_URL` | `https://cloudpdp.api.permit.io` | Base URL of the PDP that answers `check_permission`: the cloud PDP, or a [container PDP](https://docs.permit.io/how-to/deploy/deploy-to-production/#installing-the-pdp). |

At least one of the two element variables must be set. A missing or invalid value stops the
command with exit status 2 and a message that names the variable.

The server uses the proxy that `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` (or the system
settings) name. It never reads a netrc file. Log records go to stderr, because stdout carries the
protocol. The API key and the Elements tokens are redacted from log records and error messages.

## Tools

The descriptions are the first sentence of what the model sees, with `PERMIT_RESOURCE=documents`
and the default tenant.

| Tool | Description |
| --- | --- |
| `list_resource_instances` | List the instances of the 'documents' resource type in tenant 'default', with the ID and key of each. |
| `check_permission` | Ask Permit whether the acting user may perform an action on the 'documents' resource type in tenant 'default', or on one instance of it (the instance key, not the ID). |
| `create_access_request` | Request a role on the 'documents' resource type in tenant 'default', or on one instance of it. |
| `list_access_requests` | List the access requests of this server's access request element that Permit shows the acting user, each with the requesting user's email and name. |
| `approve_access_request` | Approve an access request, which grants the requested role. |
| `deny_access_request` | Deny an access request. |
| `cancel_access_request` | Cancel an access request. |
| `create_operation_approval` | Request one-time approval for an operation on the 'documents' resource type in tenant 'default', or on one instance of it. |
| `list_operation_approvals` | List the operation approval requests on the 'documents' resource type in tenant 'default' that Permit shows the acting user, each with the requesting user's email and name. |
| `approve_operation_approval` | Approve an operation approval request. |
| `deny_operation_approval` | Deny an operation approval request. |
| `cancel_operation_approval` | Cancel an operation approval request. |

Which tools are registered depends on the elements you set:

- The access-request tools (`*_access_request` and `list_access_requests`) need
  `PERMIT_ACCESS_REQUEST_ELEMENT`, a User Management element.
- The operation-approval tools (`*_operation_approval` and `list_operation_approvals`) need
  `PERMIT_OPERATION_APPROVAL_ELEMENT`, an Approval Management element.
- `list_resource_instances` and `check_permission` need no element and are always registered.

Request IDs are UUIDs; the list tools return them. Cancel withdraws a pending request that the
acting user filed; Permit refuses otherwise.

### What `check_permission` evaluates

`check_permission` sends the acting user, the action and the resource type (or one instance, by
key) in the configured tenant to `PERMIT_PDP_URL`, with an empty context. It returns
`allowed: true` or `false`. On the cloud PDP, RBAC and ReBAC policies are evaluated; a permission
that only an ABAC policy grants comes back false. To evaluate ABAC policies, run a container PDP
and set `PERMIT_PDP_URL` to it.

## Embedding the tools

The package exports `create_server`, `PermitTools`, `Settings`, `TOOL_NAMES`, the resolvers
`bound_user` and `access_token_subject`, the `IdentityResolver` protocol, and the errors
`ConfigError` and `IdentityError`. The server runs on `MCPServer` from mcp 2. The
[API reference](https://permitio.github.io/permit-mcp/) documents each of them, and lists every
tool's input and output schemas.

### A server for one user

`create_server()` builds a complete server. Without `identity=`, every call acts as
`settings.user` (`PERMIT_MCP_USER`).

```python
from permit_mcp import Settings, create_server

settings = Settings.from_env(resource="documents")  # arguments win over the environment
server = create_server(settings, exclude_tools={"approve_access_request"})
server.run("stdio")
```

### A server over HTTP with authentication

`create_server(settings, auth=..., token_verifier=...)` passes the MCP SDK's authentication
settings and your `TokenVerifier` to `MCPServer`. The server then answers HTTP 401 to a request
without a bearer token your verifier accepts, before any tool runs or anything is sent to Permit.
Without `identity=`, each call acts as the `subject` of the `AccessToken` your verifier returned
(`access_token_subject()`), so that subject must be the caller's Permit user key, and
`settings.user` is not used. An explicit `identity=` replaces the token's subject for every
caller: pass `identity=bound_user(...)` with a verifier only when every authenticated caller
should act as one shared Permit user.

```python
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from permit_mcp import Settings, create_server


class MyTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify the token with your authorization server, or return None."""
        ...  # subject: the caller's Permit user key; resource: the URL it was issued for


server = create_server(
    Settings.from_env(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl("https://auth.example.com"),
        resource_server_url=AnyHttpUrl("https://mcp.example.com/mcp"),
        validate_token_resource=True,
    ),
    token_verifier=MyTokenVerifier(),
    exclude_tools={"approve_access_request", "deny_access_request"},
)
server.run("streamable-http")
```

`MCPServer` raises `ValueError` for `auth` without `token_verifier`, and for `token_verifier`
without `auth`.

### Tools on your own server

`PermitTools(settings, identity).register(server, exclude=...)` adds the tools to a server you
build. It returns the names it registered. The tools share one HTTP session to Permit; close it
with `aclose()` when your server shuts down.

```python
import contextlib
from collections.abc import AsyncIterator

from mcp.server.mcpserver import MCPServer

from permit_mcp import PermitTools, Settings, bound_user

tools = PermitTools(Settings.from_env(), bound_user("alice"))


@contextlib.asynccontextmanager
async def lifespan(_server: MCPServer[None]) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await tools.aclose()


server = MCPServer("my-app", lifespan=lifespan)
tools.register(server, exclude={"approve_access_request", "deny_access_request"})
server.run("stdio")
```

Over streamable HTTP the lifespan runs once per process. Some transports (SSE, in-memory clients)
enter it once per connection; `create_server()` counts open connections for that reason, and
closes the session when the last one ends. A call made after `aclose()` opens a new session.

### Your own identity resolver

An identity resolver is an async function that takes the tool call's `Context` and returns the
caller's Permit user key. Raise `IdentityError` when the caller cannot be identified. Any other
exception, or a value that is not a non-empty string, also fails the call before anything is sent
to Permit.

```python
from typing import Any

from mcp.server.mcpserver import Context

from permit_mcp import IdentityError, Settings, create_server


async def signed_in_user(ctx: Context[Any, Any]) -> str | None:
    """Return the Permit user key of the caller, from your application's session."""
    ...


async def resolve(ctx: Context[Any, Any], /) -> str:
    user_key = await signed_in_user(ctx)
    if user_key is None:
        raise IdentityError("no signed-in user")
    return user_key


settings = Settings.from_env()
server = create_server(settings, identity=resolve)
server.run("stdio")
```

`settings.user` is not used when you pass a resolver or a `token_verifier`.

## Errors

- **Configuration.** `Settings` and `create_server()` raise `ConfigError`, with a message that
  names the setting, its variable and the fix. The `permit-mcp` command prints it to stderr as
  `permit-mcp: configuration error: ...` and exits with status 2. Messages never contain the API
  key or the value of a URL. When a 0.1 variable such as `RESOURCE_KEY` is set, the server logs
  one warning that names its replacement.
- **Bad arguments in code.** `register()` and `create_server()` raise `ValueError` for an excluded
  tool name that does not exist. `create_server()` passes on the `ValueError` of `MCPServer` for
  `auth` without `token_verifier`, or `token_verifier` without `auth`. `Settings.from_env()`
  raises `TypeError` for an unknown setting.
- **Tool calls.** A failed call returns a tool error to the model:
  - `The caller could not be identified, so nothing was sent to Permit: ...` when the resolver
    fails;
  - `Permit API call to <operation> returned HTTP <status>: <body>`, or `PDP call to ...`, when
    Permit answers with an error; the body is redacted and cut to 2000 characters;
  - `Permit API call to <operation> failed: <reason>` when Permit is unreachable, does not answer
    within 30 seconds, or refuses the Elements login of the acting user;
  - a 404 from the PDP says to set `PERMIT_PDP_URL` to a PDP rather than the Permit API;
  - an organization- or project-level API key fails with a message that the server needs an
    environment-level key.

  Arguments that fail validation, such as a request ID that is not a UUID, and IDs or keys that
  would change the request path (empty, `.`, `..`, or containing `/` or `\`) are refused before
  anything is sent. A redirect is an error and is not followed.

## Best practices

The examples use the Permit SDK (`permit`), a separate package.

Give users names when you sync or create them in Permit. Permit puts each requesting user's
email and name on the items the list tools return, which makes it easier to tell who filed a
request:

```python
from permit import Permit

permit = Permit(token="permit_key_...")


async def sync_user(user_id: str, first_name: str) -> None:
    await permit.api.sync_user({"key": user_id, "first_name": first_name})
```

With a ReBAC model, if a resource instance's key is not its name, store the name as an attribute
when you create the instance, with any other information that identifies it.
`list_resource_instances` returns the attributes, so you and the model can tell instances apart:

```python
from permit import Permit

permit = Permit(token="permit_key_...")


async def create_document(document_id: str, title: str) -> None:
    await permit.api.resource_instances.create({
        "resource": "documents",
        "key": document_id,
        "tenant": "default",
        "attributes": {"name": title},
    })
```

## Links

- [Access Request MCP on docs.permit.io](https://docs.permit.io/ai-security/access-request-mcp/overview)
- [API reference](https://permitio.github.io/permit-mcp/)
- [Example: a family food-ordering chat](https://github.com/permitio/permit-mcp/tree/main/examples/food-ordering-system)
- [Upgrade guide from 0.1](https://github.com/permitio/permit-mcp/blob/main/docs/upgrade-to-1.0.md)
- [Changelog](https://github.com/permitio/permit-mcp/blob/main/CHANGELOG.md)
- [Contributing](https://github.com/permitio/permit-mcp/blob/main/CONTRIBUTING.md)
- [Security policy](https://github.com/permitio/permit-mcp/blob/main/SECURITY.md)

## License

MIT. See [LICENSE](https://github.com/permitio/permit-mcp/blob/main/LICENSE).
