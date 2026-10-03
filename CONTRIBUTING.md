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
| `package` | `uv build --no-sources`, a check of the wheel and sdist contents and of the wheel's metadata against `pyproject.toml` (`check_dist.py`, as the release runs it), and the `permit-mcp` command from the installed wheel, which must exit 2 without configuration |
| `audit` | Trivy on the runtime dependency trees (newest and lowest) and the dev tree; fails on a fixable HIGH or CRITICAL advisory. In the run `release.yml` calls, the dev tree is reported but does not fail it (the `gate-dev-tree` input) |
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
in the `prek` job. A job that needs more than `contents: read` also needs that permission
granted by the `ci` job in `release.yml` (see [Releases](#releases)).

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

`.github/workflows/release.yml` publishes to PyPI when a maintainer publishes a GitHub release.
It holds no PyPI token: PyPI accepts the upload through trusted publishing, from this
repository, that workflow file and the `pypi` environment only. Each job needs the one before
it, so a failure stops the release before anything is uploaded.

| Job | What it does |
| --- | --- |
| `tag` | Fails a pre-release, a tag other than the whole string `v` + the version in `pyproject.toml` (such as `v1.2.3`), and a tag whose commit is not on `main` |
| `CI` | Runs `ci.yml` in full on the tagged commit, with `gate-dev-tree: false`: a dev-tree advisory is reported but does not hold up a release |
| `build` | `uv build --no-sources` once, with uv pinned by version and checksum and no cache, then `check_dist.py`: the files ship the package and nothing else, and the wheel declares the version, `requires-python` and dependencies of `pyproject.toml` |
| `scan` | `audit-deps.sh`; fails on a fixable HIGH or CRITICAL advisory in the runtime trees (newest and lowest versions) |
| `publish` | Waits for a reviewer to approve the `pypi` environment, checks that the `dist` artifact `build` uploaded holds exactly the tag's wheel and sdist, and uploads them with PEP 740 attestations |

Running the workflow by hand (Actions, Release, Run workflow) is a dry run: every job but
`publish`, which runs on release events only. In a called run `github.workflow` is the
caller's name, so `ci.yml`'s live-spec drift check and Slack notification, which run when it is
`CI`, do not run in a release or a dry run.

`release.yml` calls `ci.yml`, and GitHub refuses the whole release run when a `ci.yml` job asks
for a permission the release's `ci` job does not grant. `test_release.py` fails when one is
missing. The uv version in `release.yml` is the one `uv.lock` pins; change them together.

### Cutting a release

1. In a pull request, set the version with `uv version X.Y.Z` (it updates `uv.lock` too), and
   change the `CHANGELOG.md` heading from `X.Y.Z (unreleased)` to `X.Y.Z (YYYY-MM-DD)`, the
   release day. Merge it.
2. Optionally, dry-run the release workflow on `main`.
3. Create a GitHub release from `main` with a new tag `vX.Y.Z`, the `CHANGELOG.md` section as
   its notes, and publish it as a full release. A draft starts nothing, and `tag` fails a
   pre-release.
4. When `tag`, `CI`, `build` and `scan` have passed, a reviewer approves the `pypi` deployment
   in the run. Rejecting it ends the release with nothing uploaded.

A published tag is not moved. When a job before `publish` fails for a reason a re-run fixes,
re-run it from the Actions page. When the release itself is wrong, fix it on `main` and release
the next patch version. Do the same when `publish` fails part-way, after uploading one of the
two files: PyPI never accepts a file name twice, even after the file is deleted.

### Repository setup

Done once by a repository owner, outside this repository:

- **PyPI trusted publisher.** permit-mcp is not on PyPI yet, so add a pending publisher in the
  PyPI account's Publishing settings: project `permit-mcp`, owner `permitio`, repository
  `permit-mcp`, workflow `release.yml`, environment `pypi`. A pending publisher does not
  reserve the name: anyone can register `permit-mcp` until the first upload, so cut 1.0.0
  soon after registering it. The first upload creates the project with this as its trusted
  publisher, and makes the account that registered it the project's sole owner. Then add the
  project to the Permit.io organization on PyPI (or add the other maintainers as owners), and
  keep 2FA required on every owner account; uploads come from trusted publishing only, so no
  API token is needed. Renaming the repository, the workflow file or the environment stops
  releases until the publisher is changed to match.
- **The `pypi` environment** (Settings, Environments): required reviewers (the maintainers),
  with self-review prevented and administrator bypass off; deployment branches and tags set to
  selected ones, with the one tag rule `v*`. It holds no secrets.
- **A release-tag ruleset** (Settings, Rules, Rulesets, new tag ruleset): active, targeting
  `v*`, restricting creations, updates and deletions, with the maintainers on the bypass list.
  Only they can create, move or delete a version tag.
