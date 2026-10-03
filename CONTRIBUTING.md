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
| `tests` | `python -m pytest -q -W error` on Python 3.11 to 3.14, installed from the ranges in `pyproject.toml` at their newest (`highest`) and lowest (`lowest-direct`) versions, and on 3.13 from `uv.lock` (`uv sync --locked`); then a check that no test skipped |
| `package` | `uv build --no-sources`, a check of the wheel and sdist contents, and the `permit-mcp` command from the installed wheel, which must exit 2 without configuration |
| `audit` | Trivy on the runtime dependency trees (newest and lowest) and the dev tree; fails on a fixable HIGH or CRITICAL advisory |
| `audit-scripts` | `uv run --locked --only-dev pytest -c .github/scripts/pytest.ini -q .github/scripts` |
| `dependency-review` | GitHub's dependency review; fails a pull request that adds a dependency or action with a HIGH or CRITICAL advisory |
| `workflow-hardening` | actionlint, and zizmor with its online audits |
| `gitleaks` | gitleaks on the whole history |

A scheduled run each Monday also posts the audit result to Slack.

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
