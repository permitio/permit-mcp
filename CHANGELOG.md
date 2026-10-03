# Changelog

## 1.0.0 (unreleased)

The first release on PyPI. To upgrade from the 0.1 server, follow the
[upgrade guide](docs/upgrade-to-1.0.md).

### Compatibility

- Python 3.11 to 3.14. Python 3.10 is no longer supported.
- Runs on mcp 2 (`MCPServer`); bug fix, could never work before.
  With mcp 2 installed, the 0.1 server did not import, because it used `mcp.server.fastmcp`.
- Runtime dependencies are aiohttp, mcp and pydantic. The Permit SDK, httpx, python-dotenv and
  aiosqlite are no longer dependencies.
- Installs a `permit-mcp` command: `uvx permit-mcp`.
- Published to PyPI by the release workflow with trusted publishing. Each file on PyPI carries
  a PEP 740 attestation signed by that workflow.
- An API reference site, <https://permitio.github.io/permit-mcp/>, published with each release:
  the embedding API from its docstrings, and every tool's input and output schemas.

### API

- The acting user is now bound in code; tools no longer take a user argument. The `permit-mcp`
  command acts as `PERMIT_MCP_USER`, a new setting it requires. A host passes an identity
  resolver instead.
- The environment variables are renamed to `PERMIT_*` names, and `PROJECT_ID` and `ENV_ID` are
  removed. The [upgrade guide](docs/upgrade-to-1.0.md#environment-variables) maps each 0.1 name
  to its 1.0 name. The 0.1 names are not read; when one is set, the server logs one warning that
  names its replacement.
- New setting `PERMIT_API_URL`, the base URL of the Permit API.
- The server no longer loads a `.env` file.
- New tools: `check_permission`, `cancel_access_request` and `cancel_operation_approval`.
- The access-request tools are registered only when `PERMIT_ACCESS_REQUEST_ELEMENT` is set, and
  the operation-approval tools only when `PERMIT_OPERATION_APPROVAL_ELEMENT` is set. At least one
  must be set.
- Tools return JSON objects. Create, approve, deny and cancel return `status` and the request as
  Permit returns it, instead of a sentence. The list tools return Permit's paginated object, and
  add `requesting_user` (key, email, first name and last name) to each item.
- Tool arguments are validated: `page` is at least 1, `per_page` is 1 to 100, `status` is one of
  pending, approved, denied or canceled. `list_resource_instances` returns 30 results per page by
  default, instead of 100.
- Embedding: `PermitServer(mcp, exclude_tools=...)` is replaced by `create_server(...)` and
  `PermitTools(settings, identity).register(server, exclude=...)`, with `aclose()` to close the
  HTTP session. Hosts use `MCPServer` instead of `FastMCP`. New exports: `Settings`,
  `ConfigError`, `IdentityResolver`, `IdentityError`, `bound_user`, `access_token_subject` and
  `TOOL_NAMES`. An unknown tool name in `exclude` raises `ValueError`.
- A configuration error stops the `permit-mcp` command with exit status 2 and a message that
  names the variable.
- The API key and Elements tokens are redacted from log records and error messages.

### Wire behaviour

- The project and environment come from the API key: the server asks the Permit API once, and
  refuses an organization- or project-level key.
- The server logs in to Elements itself (`POST /v2/auth/elements_login_as`) and reads the token
  from the `element_bearer_token` field. Permit answers a failed login with HTTP 200 and an
  `error` field; the tool now reports that reason instead of sending a request without a token.
- Access request and operation approval IDs must be UUIDs. Other values are refused before
  anything is sent.
- Redirects are not followed; a redirect is an error.
- A netrc file is never read. Proxies come from `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`, or
  the system settings.
- IDs and keys are escaped as single path segments. A value that would change the request path
  (empty, `.`, `..`, or containing `/` or `\`) is refused before anything is sent.
- Every request uses `PERMIT_API_URL` instead of a fixed `https://api.permit.io`.
- Requests time out after 30 seconds. Cookies are not stored. All calls share one HTTP session.
- The list tools look up each requesting user once, at most 5 at a time. A user Permit no longer
  has comes back as `requesting_user: null`.
