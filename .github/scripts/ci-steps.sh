#!/usr/bin/env bash
#
# The steps of ci.yml that hold more than one command, one subcommand each, run
# from the repository root. Inputs come from the environment the step sets;
# RUNNER_TEMP holds what one step leaves for the next.
#
# Usage: ci-steps.sh SUBCOMMAND [ARGS]
#
#   needs [WORKFLOW]  The prek job's check of ci.yml (the default WORKFLOW): CI
#       needs every job but itself and notify, once; ADVISORY_JOBS in CI's "Check
#       the needed jobs" step names jobs CI needs, once each; and its
#       EXPECTED_JOBS is the number of jobs CI needs. Exits 1 when any of that is
#       wrong, and 2 when no job is read.
#   hooks  Runs every prek hook. Exits 1 when a hook fails, and 2 when other than
#       EXPECTED_HOOKS hooks passed: a hook skipped, added or removed.
#   install  Resolves the runtime dependencies alone at RESOLUTION
#       (lowest-direct or highest), so the dev group cannot raise a floor, as a
#       consumer resolves them (--no-sources, no publish-age cooldown), into
#       RUNNER_TEMP/runtime.txt; checks a lowest-direct resolution is at the
#       floors (check_floors.py); installs the package and the dev group under
#       it; and checks the installed runtime versions are the resolved ones.
#       Exits 1 when they are not, and 2 when the resolution is empty.
#   coverage  The API coverage report of RUNNER_TEMP/api-record.jsonl. In CI's
#       own scheduled and manual runs (WORKFLOW is CI, EVENT schedule or
#       workflow_dispatch) it first writes the live control-plane spec's
#       in-scope inventory to RUNNER_TEMP/api-specs, and reports against it with
#       the committed one as the baseline. Exits 1 on a finding, and 2 when the
#       report did not run.
#   surface  Lists the changes to tests/snapshots/surface.json against HEAD^1,
#       the base a pull request's merge commit merged (BASE_REF names it), in the
#       job summary. Exits 0 with a warning on a breaking change, and 2 when the
#       snapshots could not be compared.
#   smoke DIST  Installs DIST's wheel in a fresh environment and runs
#       permit-mcp with no configuration. Exits 1 unless it exits 2 with a
#       configuration error.
#   mutation  The mutation gate on the lines HEAD changes against HEAD^1. Exits 0
#       when the tests catch at least 80% of the mutants or no package line
#       changed, 1 when they catch fewer, and 2 when the gate did not run.
#   results  The CI job: reads the needed jobs' results from NEEDS (toJSON(needs))
#       and fails unless each succeeded, but for a PULL_REQUEST_JOBS job skipped
#       off a pull request, a SCHEDULED_JOBS job skipped outside CI's own
#       scheduled and manual runs, and an ADVISORY_JOBS job that did not succeed,
#       which is a warning. Exits 1 when a job did not succeed, and 2 when the
#       results of other than EXPECTED_JOBS jobs arrive.
#
# The stdlib scripts run on the requires-python floor through `uv run
# --no-project`, whatever Python the job has.
set -euo pipefail

SPEC_URL=https://api.permit.io/v2/openapi.json

# Exits 2, as a check that did not run, when a variable it reads is unset.
require() {
  local name
  for name in "$@"; do
    if [[ -z ${!name+set} ]]; then
      echo "::error title=${0##*/}::did not run: ${name} unset"
      exit 2
    fi
  done
}

run_script() {
  local script=$1
  shift
  uv run --no-project --python 3.11 python ".github/scripts/$script" "$@"
}

own_scheduled_run() {
  [[ $WORKFLOW == CI && ($EVENT == schedule || $EVENT == workflow_dispatch) ]]
}

needs() {
  local -x LC_ALL=C
  local file=${1:-.github/workflows/ci.yml} jobs needed expected advisory twice stray count
  local ci_env='.jobs.ci.steps[] | select(.name == "Check the needed jobs") | .env'
  if ! jobs=$(yq '.jobs | keys | .[] | select(. != "ci" and . != "notify")' "$file" |
    sort) || [[ -z $jobs ]]; then
    echo "::error title=CI needs::No jobs read from $file."
    exit 2
  fi
  needed=$(yq '.jobs.ci.needs[]' "$file" | sort)
  expected=$(yq "$ci_env | .EXPECTED_JOBS // \"\"" "$file")
  advisory=$(yq "$ci_env | .ADVISORY_JOBS // \"\"" "$file" | tr -s ' ' '\n' | grep . |
    sort || true)
  local status=0
  twice=$(uniq -d <<<"$needed")
  if [[ -n $twice ]]; then
    echo "::error title=CI needs::CI needs ${twice//$'\n'/, } more than once."
    status=1
  fi
  if [[ $jobs != "$(uniq <<<"$needed")" ]]; then
    echo "::error title=CI needs::CI's needs must list every job in $file but CI and" \
      "notify. Add a new job to CI's needs and change EXPECTED_JOBS."
    diff <(echo "$jobs") <(uniq <<<"$needed") || true
    status=1
  fi
  twice=$(uniq -d <<<"$advisory")
  if [[ -n $twice ]]; then
    echo "::error title=CI needs::ADVISORY_JOBS lists ${twice//$'\n'/, } twice."
    status=1
  fi
  stray=$(comm -23 <(uniq <<<"$advisory") <(uniq <<<"$needed") | grep . || true)
  if [[ -n $stray ]]; then
    echo "::error title=CI needs::ADVISORY_JOBS lists ${stray//$'\n'/, }, which CI does not need."
    status=1
  fi
  count=$(grep -c . <<<"$needed" || true)
  if [[ $expected != "$count" ]]; then
    echo "::error title=CI needs::EXPECTED_JOBS in the CI job is ${expected:-not set}, but CI" \
      "needs $count jobs."
    status=1
  fi
  if [[ $status -eq 0 ]]; then
    advisory=${advisory//$'\n'/, }
    echo "CI needs every job: ${needed//$'\n'/, }. Advisory: ${advisory:-none}."
  fi
  exit "$status"
}

hooks() {
  require RUNNER_TEMP EXPECTED_HOOKS
  local passed
  uv run --locked --only-dev prek run --all-files --show-diff-on-failure --color=never |
    tee "$RUNNER_TEMP/prek.txt"
  passed=$(grep -c 'Passed$' "$RUNNER_TEMP/prek.txt" || true)
  if [[ $passed -ne $EXPECTED_HOOKS ]]; then
    echo "::error title=prek::${passed} hooks passed, expected ${EXPECTED_HOOKS}."
    exit 2
  fi
}

install() {
  require RUNNER_TEMP RESOLUTION
  local -x LC_ALL=C
  local resolve=(--no-sources --exclude-newer false) runtime=$RUNNER_TEMP/runtime.txt
  local expected differ
  uv pip compile --quiet "${resolve[@]}" --python "$(command -v python)" \
    --resolution "$RESOLUTION" pyproject.toml -o "$runtime"
  expected=$(grep -E '^[a-z0-9]' "$runtime" | sort || true)
  if [[ -z $expected ]]; then
    echo "::error title=Install::The $RESOLUTION resolution is empty."
    exit 2
  fi
  # lowest-direct moves past a floor it cannot install without saying so, and the
  # leg would then test something above the floor.
  if [[ $RESOLUTION == lowest-direct ]]; then
    run_script check_floors.py pyproject.toml "$runtime"
  fi
  uv pip install "${resolve[@]}" . --group dev -c "$runtime"
  differ=$(comm -23 <(echo "$expected") <(uv pip freeze | sort))
  if [[ -n $differ ]]; then
    echo "::error title=Install::Not installed as resolved: ${differ//$'\n'/, }."
    exit 1
  fi
  echo "$(grep -c . <<<"$expected") runtime packages at the $RESOLUTION resolution."
}

coverage() {
  require RUNNER_TEMP EVENT WORKFLOW GITHUB_STEP_SUMMARY
  local committed=.github/api-specs/control-plane.json
  local control_plane=(--spec "control-plane=$committed")
  if own_scheduled_run; then
    if ! curl --fail --silent --show-error --proto '=https' --max-time 60 --retry 3 \
      --output "$RUNNER_TEMP/openapi.json" "$SPEC_URL"; then
      echo "::error title=API coverage::Could not download ${SPEC_URL}."
      exit 2
    fi
    run_script api_coverage.py snapshot control-plane "$RUNNER_TEMP/openapi.json" \
      --source "$SPEC_URL" --allowlist .github/scripts/api_coverage_allowlist.json \
      --out-dir "$RUNNER_TEMP/api-specs"
    control_plane=(--spec "control-plane=$RUNNER_TEMP/api-specs/control-plane.json"
      --baseline "control-plane=$committed")
  fi
  run_script api_coverage.py report "${control_plane[@]}" --spec pdp=.github/api-specs/pdp.json \
    --allowlist .github/scripts/api_coverage_allowlist.json \
    --record "$RUNNER_TEMP/api-record.jsonl" --origins "$RUNNER_TEMP/api-record.origins.json" \
    --summary "$GITHUB_STEP_SUMMARY"
}

surface() {
  require RUNNER_TEMP BASE_REF GITHUB_STEP_SUMMARY
  local snapshot=tests/snapshots/surface.json in_base report status=0
  echo "### Tool surface changes against $BASE_REF (HEAD^1)" >>"$GITHUB_STEP_SUMMARY"
  in_base=$(git ls-tree --name-only HEAD^1 -- "$snapshot")
  if [[ -z $in_base ]]; then
    echo "No base snapshot: $BASE_REF (HEAD^1) has no $snapshot to compare with." |
      tee -a "$GITHUB_STEP_SUMMARY"
    return
  fi
  git show "HEAD^1:$snapshot" >"$RUNNER_TEMP/base-surface.json"
  report=$(run_script surface_diff.py "$RUNNER_TEMP/base-surface.json" "$snapshot") || status=$?
  printf '%s\n' "$report"
  printf '%s\n' '```' "$report" '```' >>"$GITHUB_STEP_SUMMARY"
  case $status in
    0) ;;
    1) echo "::warning title=Breaking surface changes::See the job summary." ;;
    *)
      echo "::error title=Surface diff::Could not compare the snapshots."
      exit 2
      ;;
  esac
}

smoke() {
  require RUNNER_TEMP
  local dist=${1:?usage: ci-steps.sh smoke DIST} venv=$RUNNER_TEMP/smoke status=0
  uv venv --quiet "$venv"
  uv pip install --quiet --python "$venv/bin/python" --exclude-newer false "$dist"/*.whl
  env -i "$venv/bin/permit-mcp" </dev/null 2>"$RUNNER_TEMP/smoke.txt" || status=$?
  cat "$RUNNER_TEMP/smoke.txt"
  if [[ $status -ne 2 ]] || ! grep -q 'configuration error' "$RUNNER_TEMP/smoke.txt"; then
    echo "::error title=Console script::permit-mcp with no configuration exited $status;" \
      "expected 2 and a configuration error."
    exit 1
  fi
  echo "permit-mcp exits 2 on a missing configuration, as it should."
}

mutation() {
  local status=0
  uv run --locked python .github/scripts/mutation_gate.py --base HEAD^1 --threshold 80 \
    --workers 4 --memory-mib 3072 --max-minutes 25 || status=$?
  case $status in
    0) ;;
    1) echo "::error title=Mutation tests::The tests catch too few mutants of the changed lines;" \
      "see the job summary." ;;
    *)
      echo "::error title=Mutation tests::The mutation tests did not run; see the log."
      exit 2
      ;;
  esac
  exit "$status"
}

results() {
  require NEEDS EVENT WORKFLOW EXPECTED_JOBS ADVISORY_JOBS PULL_REQUEST_JOBS SCHEDULED_JOBS
  local results count failed job advisory skipped_in_own_run=""
  if ! results=$(jq -r 'to_entries[] | "\(.key) \(.value.result)"' <<<"$NEEDS"); then
    echo "::error title=CI::Could not read the job results."
    exit 2
  fi
  printf '%s\n' "$results"
  count=$(grep -c . <<<"$results" || true)
  if [[ $count -ne $EXPECTED_JOBS ]]; then
    echo "::error title=CI::${count} job results, expected ${EXPECTED_JOBS}."
    exit 2
  fi
  failed=$(grep -v ' success$' <<<"$results" || true)
  if [[ $EVENT != pull_request ]]; then
    for job in $PULL_REQUEST_JOBS; do
      if grep -qx "$job skipped" <<<"$failed"; then
        echo "::notice title=CI::$job runs on pull requests only; skipped on this $EVENT run."
      fi
      failed=$(grep -vx "$job skipped" <<<"$failed" || true)
    done
  fi
  for job in $SCHEDULED_JOBS; do
    if ! grep -qx "$job skipped" <<<"$failed"; then
      continue
    fi
    failed=$(grep -vx "$job skipped" <<<"$failed" || true)
    if own_scheduled_run; then
      echo "::error title=CI::$job was skipped on CI's own $EVENT run, which it must run in:" \
        "its if: no longer matches this run."
      skipped_in_own_run+=$'\n'"$job skipped"
    else
      echo "::notice title=CI::$job runs in CI's scheduled and manual runs only; skipped on" \
        "this $EVENT run."
    fi
  done
  for job in $ADVISORY_JOBS; do
    advisory=$(awk -v job="$job" '$1 == job' <<<"$failed")
    if [[ -n $advisory ]]; then
      echo "::warning title=CI::Advisory job did not succeed: ${advisory}"
    fi
    failed=$(awk -v job="$job" '$1 != job' <<<"$failed")
  done
  failed=$(grep . <<<"${failed}${skipped_in_own_run}" || true)
  if [[ -n $failed ]]; then
    echo "::error title=CI::Jobs that did not succeed: ${failed//$'\n'/, }"
    exit 1
  fi
  echo "All ${count} jobs passed."
}

case ${1:-} in
  needs | hooks | install | coverage | surface | smoke | mutation | results) "$@" ;;
  *)
    echo "usage: ci-steps.sh needs|hooks|install|coverage|surface|smoke|mutation|results" >&2
    exit 2
    ;;
esac
