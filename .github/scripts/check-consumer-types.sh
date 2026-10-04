#!/usr/bin/env bash
#
# Type-checks a host's use of the public API, as a consumer of the package sees
# it: FIXTURE (tests/consumer/consumer.py by default) against the wheel in DIST.
# In a temporary directory outside the checkout, it installs the wheel and its
# dependencies in a fresh Python 3.11 environment, and runs the mypy on PATH
# there with --python-executable set to that environment, so mypy finds the
# installed wheel and never src/ or an editable install. The configuration is
# strict, with warn_unused_ignores and the error codes pyproject.toml enables,
# deprecated among them. A misuse in the fixture is marked
# "# type: ignore[<code>]", so one that starts to type-check fails the check as
# an unused ignore.
#
# Usage: check-consumer-types.sh DIST [FIXTURE]
#
# Run it with mypy on PATH, such as through `uv run --locked --only-dev`. Exits 0
# when the fixture type-checks, 1 when mypy reports an error, and 2 when the
# check did not run: DIST holds other than one wheel, FIXTURE is missing, mypy
# or uv is not on PATH, the install failed, or mypy did not finish.
set -euo pipefail

dist=${1:?usage: check-consumer-types.sh DIST [FIXTURE]}
fixture=${2:-tests/consumer/consumer.py}

did_not_run() {
  echo "::error title=Consumer types::did not run: $*"
  exit 2
}

shopt -s nullglob
wheels=("$dist"/*.whl)
shopt -u nullglob
if [[ ${#wheels[@]} -ne 1 ]]; then
  did_not_run "expected one wheel in $dist, found ${#wheels[@]}"
fi
[[ -f $fixture ]] || did_not_run "no fixture at $fixture"
mypy=$(command -v mypy) || did_not_run "mypy is not on PATH; run this with uv run --locked --only-dev"
command -v uv >/dev/null || did_not_run "uv is not on PATH"

work=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/consumer-types.XXXXXX")
trap 'rm -rf "$work"' EXIT
cp "$fixture" "$work/consumer.py"
# The codes pyproject.toml's [tool.mypy] enables; test_check_consumer_types.py
# fails when the two lists differ.
cat >"$work/mypy.ini" <<'EOF'
[mypy]
python_version = 3.11
strict = True
warn_unused_ignores = True
warn_unreachable = True
enable_error_code =
    deprecated,
    exhaustive-match,
    ignore-without-code,
    mutable-override,
    possibly-undefined,
    redundant-expr,
    redundant-self,
    truthy-bool,
    truthy-iterable,
    unimported-reveal,
    unused-awaitable,
EOF

uv venv --quiet --python 3.11 "$work/venv" || did_not_run "uv could not create the environment"
uv pip install --quiet --python "$work/venv/bin/python" --exclude-newer false "${wheels[0]}" ||
  did_not_run "uv could not install ${wheels[0]}"

status=0
(cd "$work" && "$mypy" --config-file mypy.ini --python-executable "$work/venv/bin/python" \
  consumer.py) >"$work/mypy.txt" 2>&1 || status=$?
sed "s|^consumer\.py:|$fixture:|" "$work/mypy.txt"
case $status in
  0) echo "The public API type-checks as $fixture uses it." ;;
  1)
    echo "::error title=Consumer types::$fixture does not type-check against the wheel. An" \
      "error is a host's line the API no longer accepts; an unused \"type: ignore\" is a" \
      "misuse it now accepts. When the change is intended, update $fixture (CONTRIBUTING.md)."
    exit 1
    ;;
  *) did_not_run "mypy exited $status" ;;
esac
