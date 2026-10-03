# permit-mcp

An MCP server (mcp 2, `MCPServer`) for Permit.io access requests, operation approvals and
permission checks. Python 3.11 to 3.14, built with uv_build. Source in `src/permit_mcp/`, tests in
`tests/`.

## Commands

[CONTRIBUTING.md](CONTRIBUTING.md) has the setup and test commands, what CI runs, how to add a CI
job, and how to take a dependency release younger than the 7-day cooldown. In short:
`uv sync --locked`, `uv run prek run --all-files`, `uv run pytest`.

## Layout

- `config.py`: `Settings` and `ENV_VARS`, the environment variables `Settings.from_env` reads.
- `identity.py`: `IdentityResolver`, `bound_user`, `access_token_subject`.
- `tools.py`: `PermitTools`, `TOOL_NAMES`, the tool descriptions and argument schemas.
- `permit_api.py`: every HTTP request to the Permit API and the PDP. The only module that sends
  requests.
- `server.py`: `create_server` and the `permit-mcp` command (stdio).
- `_log.py`: `get_logger` and the redaction of secrets.

## Rules

- **No tool takes a user.** The acting user comes from the identity resolver bound in code
  (`bound_user(PERMIT_MCP_USER)`, `access_token_subject()`, or a host's resolver). Never add a
  user, requester or subject argument to a tool, or read one from tool input.
  `tests/test_identity.py` enforces this.
- **Public repository.** Code, comments, commits, pull requests and docs must not contain
  internal paths, hostnames, links or customer names. Linear ticket numbers (PER-12345) are fine.
- **Logging only through `permit_mcp._log.get_logger`.** It redacts the API key and Elements
  tokens. ruff bans `logging.getLogger` elsewhere. Register any new secret with `redact()` or
  `redact_recent()`.
- **Surface snapshot.** `tests/snapshots/surface.json` pins every tool's description, schemas
  and annotations, and the public API's signatures. After an intended change, run
  `UPDATE_SNAPSHOT=1 uv run pytest tests/test_surface.py` and commit the result. On a pull
  request CI labels each change BREAKING or non-breaking against the base branch.
- **Mutation gate.** On a pull request, mutmut mutates the changed lines of `src/permit_mcp`,
  and CI fails when the tests catch fewer than 80% of the mutants (see CONTRIBUTING.md).
- **Wire cases.** Every tool has a case in `CASES` in `tests/support.py` that pins the exact
  requests it sends and what it returns; `test_every_tool_has_a_case` in
  `tests/test_tools_wire.py` fails when a tool has none. A new tool also needs a row in the
  README tools table (`tests/test_docs.py`), and a new setting a row in the configuration table.
- **API coverage.** A tool that calls a Permit operation outside the scope of
  `.github/scripts/api_coverage_allowlist.json` needs it added to the scope and the snapshot
  refreshed. A tool that starts calling an in-scope operation needs that operation's `missing`
  entry deleted, or the report flags the entry as stale. A test that sends to a server of its
  own declares it with `tests.api_record.note_origin`. See CONTRIBUTING.md.
- **README and upgrade-guide Python blocks are type-checked** by `tests/test_docs.py`. Mark a
  block that shows 0.1 code with `<!-- docs-check: skip, 0.1 code -->` on the line before it.
- **API reference site.** A new export of `permit_mcp` needs a `:::` entry in
  `docs/reference/api.md` (`tests/test_docs_pages.py`). The CI `docs` job fails on any docs
  build warning, including a docstring Griffe cannot parse. See CONTRIBUTING.md.
- **New CI jobs** go into the `needs` of the `CI` job in `.github/workflows/ci.yml`, and
  `EXPECTED_JOBS` changes to match (see CONTRIBUTING.md). The `prek` job fails otherwise.
- **Offline tests never skip.** CI fails a run in which a test skipped or none ran. Do not add
  `pytest.skip`, `skipif` or `importorskip`.
- **Never test against the real Permit cloud except through the e2e suite with the owner's
  keys.** That suite is `tests/e2e`, marked `e2e`, deselected by default and run with
  `-m e2e` (CONTRIBUTING.md). Offline tests use the mock servers in `tests/conftest.py`, which
  also clears the Permit, proxy and netrc variables for every test.
- Zero warnings: pytest runs with `filterwarnings = ["error"]`, and mypy is strict.
- 100-character lines, absolute imports, Google-style docstrings.
- Docs describe what the code does now. Plain, factual wording.
