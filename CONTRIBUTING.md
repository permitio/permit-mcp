# Contributing

Thanks for helping. Open an issue to discuss a larger change before you start. Report
vulnerabilities as [SECURITY.md](SECURITY.md) describes, not in an issue. Everyone who takes part
follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Set up

You need [uv](https://docs.astral.sh/uv/) 0.12.19 or newer on `PATH`. `pyproject.toml` requires
it, and the hooks run `uv run --locked` and `uv lock --check` with the `uv` they find on `PATH`.
uv installs the Python the project needs (3.11 to 3.14).

```shell
uv sync --locked                 # the project and the dev tools, as uv.lock pins them
uv run prek install              # run the hooks on every commit
uv run prek run --all-files      # every hook on every file
uv run pytest                    # the offline tests
```

prek, ruff, mypy, typos and pytest come from `uv.lock`, so you run the versions CI runs. The hooks
are in `.pre-commit-config.yaml`: file checks, ruff (lint and format), mypy (strict), typos, a
`uv.lock` drift check, actionlint, zizmor and shellcheck.

The tests of the CI scripts (`.github/scripts/test_*.py`) also need
[mikefarah yq](https://github.com/mikefarah/yq) v4 on `PATH`:

```shell
uv run --locked --only-dev pytest -c .github/scripts/pytest.ini -q .github/scripts
```

## Tests

- The offline tests run against local mock servers of the Permit API and PDP. They never contact
  Permit, and need no API key.
- A test must not skip: CI fails a run in which any test skipped or no test ran.
- `tests/test_tools_wire.py` pins the exact requests each tool sends. A new tool needs a case
  there; `test_every_tool_has_a_case` fails until it has one.
- `tests/test_docs.py` checks the README's tools and configuration tables against the code, and
  type-checks every Python block in the README and the upgrade guide with mypy. When you add a
  tool, change a tool's first sentence, or add or change a setting, update the README. A block
  that shows 0.1 code is marked with `<!-- docs-check: skip, 0.1 code -->` on the line before it.

## What CI runs

The `CI` check passes only when every job below succeeded. `dependency-review` runs on pull
requests only.

| Job | What it runs |
| --- | --- |
| `prek` | `uv run --locked --only-dev prek run --all-files`, with a count of the hooks that passed, and a check that `CI` needs every job |
| `tests` | `python -m pytest -q -W error` on Python 3.11 to 3.14, installed from the ranges in `pyproject.toml` at their newest (`highest`) and lowest (`lowest-direct`) versions, and on 3.13 from `uv.lock` (`uv sync --locked`); then a check that no test skipped. The `uv.lock` leg also runs the API coverage report (below) |
| `package` | `uv build --no-sources`, a check of the wheel and sdist contents, and the `permit-mcp` command from the installed wheel, which must exit 2 without configuration |
| `audit` | Trivy on the runtime dependency trees (newest and lowest) and the dev tree; fails on a fixable HIGH or CRITICAL advisory |
| `audit-scripts` | `uv run --locked --only-dev pytest -c .github/scripts/pytest.ini -q .github/scripts` |
| `dependency-review` | GitHub's dependency review; fails a pull request that adds a dependency or action with a HIGH or CRITICAL advisory |
| `workflow-hardening` | actionlint, and zizmor with its online audits |
| `gitleaks` | gitleaks on the whole history |

A scheduled run each Monday also posts the audit result to Slack.

### API coverage report

`.github/scripts/api_coverage.py report` lists which Permit API and PDP operations the offline
tests send. With `PERMIT_MCP_API_RECORD=<file>` set, the tests write one JSON line per request
(method, origin, path, status and test id; no query, header or body), and beside it
`<file stem>.origins.json`, which maps the origin of each test server to the APIs it serves. A
test that sends to a server of its own declares it with `tests.api_record.note_origin`. The
report matches each request that got a response against the in-scope inventories of its
origin's APIs in `.github/api-specs/`, and reads the scope and the reasons from
`.github/scripts/api_coverage_allowlist.json`:

- In scope are the operations the server calls, plus every operation of the Access Requests
  (EAP) and Operation Approval (EAP) tags. Every other operation is out of scope, for the one
  reason in `out_of_scope`.
- An in-scope operation no test sends needs an entry in `operations`, with status `untested`
  (the server calls it) or `missing` (no tool does), and a reason.
- A request that matches no in-scope operation of its API, such as
  `POST /v2/auth/elements_login_as`, which the public spec does not list, needs an entry in
  `sdk_only` with its API and a reason.

A request that got no response (refused, timed out) counts for nothing. The report fails on an
in-scope operation no test sends and no entry explains, on a request no in-scope operation or
`sdk_only` entry explains, and on a stale entry. To run it locally:

```shell
PERMIT_MCP_API_RECORD=/tmp/api-record.jsonl uv run pytest
python .github/scripts/api_coverage.py report \
  --spec control-plane=.github/api-specs/control-plane.json \
  --spec pdp=.github/api-specs/pdp.json \
  --allowlist .github/scripts/api_coverage_allowlist.json \
  --record /tmp/api-record.jsonl --origins /tmp/api-record.origins.json
```

When a tool starts calling an operation outside the scope, add it to the scope in the allowlist
and refresh the snapshot; until then the report fails on the test that sends it. When a tool
starts calling an operation that is already in scope, delete its `missing` entry; otherwise the
report flags the entry as stale.

#### Refreshing the snapshots

The inventories hold only the in-scope operations, each with its method, path template,
operationId, tags, deprecated flag, parameters (name, in, required) and request body schema.
Pull requests read only the committed inventories. The weekly and manual runs download the live
control-plane spec, write its in-scope inventory, fail on any in-scope operation that was added,
removed or changed in any of those fields, and upload the new inventory as the
`control-plane-spec` artifact. When the change is expected, refresh the snapshot, update the
allowlist to match, and commit both:

```shell
curl --fail --silent --show-error --output /tmp/openapi.json https://api.permit.io/v2/openapi.json
python .github/scripts/api_coverage.py snapshot control-plane /tmp/openapi.json \
  --allowlist .github/scripts/api_coverage_allowlist.json \
  --source https://api.permit.io/v2/openapi.json
```

`snapshot` refuses a spec with fewer operations than its minimum, and writes
`.github/api-specs/control-plane.json` and `control-plane.source.json` (the URL and the date).
Rerun it whenever the scope in the allowlist changes. The PDP inventory (`pdp.json`) holds
`POST /allowed`, taken from the PDP's source at the commit its `.source.json` names; no CI run
downloads it. To refresh it, write the PDP's `/openapi.json` to a file and run `snapshot pdp` on
it with that source.

### Adding a CI job

Add every new job to the `needs` of the `CI` job, and set `EXPECTED_JOBS` in its "Check the
needed jobs" step to the number of jobs in `needs`. The `prek` job fails when a job is missing
from `needs` or `EXPECTED_JOBS` is wrong. When you add or remove a hook, change `EXPECTED_HOOKS`
in the `prek` job.

## Dependencies

`[tool.uv]` sets `exclude-newer = "7 days"`: `uv lock` ignores releases younger than 7 days, and
Dependabot waits as long. To take a security fix that is younger than that, admit that one
package under `[tool.uv]`:

```toml
[tool.uv]
exclude-newer-package = { <package> = false }
```

Then run `uv lock --upgrade-package <package>` and commit both `pyproject.toml` and `uv.lock`.
Remove the entry once the release is older than 7 days.

## Pull requests

- Keep one logical change per commit, with an imperative subject of at most 72 characters.
- Run `uv run prek run --all-files` and `uv run pytest` before you push.
- Describe what the change does. Add a line to `CHANGELOG.md` for a change users see.
- This repository is public: no internal links, hostnames or customer names in code, commits or
  pull requests.

## Releases

A maintainer publishes a GitHub release.
