#!/usr/bin/env bash
#
# Reports on and gates on the Trivy reports audit-deps.sh wrote to AUDIT_DIR, for
# the trees in AUDIT_TREES (space separated), with format_audit.py, run on the
# requires-python floor. The audit job of ci.yml, the scan job of release.yml
# and ci.yml's notify job run it from the repository root.
#
# Usage: audit-report.sh summary CONTEXT | gate | slack
#
#   summary CONTEXT  Appends the job summary to GITHUB_STEP_SUMMARY; CONTEXT says
#       what was scanned.
#   gate  Annotates each blocking advisory, then exits 0 when no fixable HIGH or
#       CRITICAL advisory is found, 1 when one is, and 2 when a report is
#       missing, unreadable, scanned nothing or scanned other than its tree.
#       With GATE_DEV_TREE false, only the runtime-* trees are annotated and
#       gated; the others are in the summary only.
#   slack  Writes the Slack message as the step output `text` to GITHUB_OUTPUT,
#       with REPO, RUN_URL, and the results of CI and the two e2e jobs
#       (CI_RESULT, E2E_RESULT, E2E_PDP_LATEST_RESULT).
set -euo pipefail

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

format_audit() {
  uv run --no-project --python 3.11 python .github/scripts/format_audit.py \
    --dir "$AUDIT_DIR" "$@"
}

summary() {
  require GITHUB_STEP_SUMMARY
  format_audit "${trees[@]}" --context "${1:?usage: audit-report.sh summary CONTEXT}" \
    >>"$GITHUB_STEP_SUMMARY"
}

gate() {
  local gated=() ungated=() tree status=0
  if [[ ${GATE_DEV_TREE:-true} == false ]]; then
    for tree in "${trees[@]}"; do
      if [[ $tree == runtime-* ]]; then gated+=("$tree"); else ungated+=("$tree"); fi
    done
    echo "${ungated[*]}: reported in the summary but not gated (gate-dev-tree: false)."
    trees=("${gated[@]}")
  fi
  format_audit "${trees[@]}" --annotations
  format_audit "${trees[@]}" --gate || status=$?
  case $status in
    0) echo "No fixable HIGH or CRITICAL advisory." ;;
    1) echo "::error title=Dependency audit::Fixable HIGH or CRITICAL advisories; see the job" \
      "summary." ;;
    *)
      echo "::error title=Dependency audit::The audit did not complete; see the job summary."
      exit 2
      ;;
  esac
  exit "$status"
}

slack() {
  require GITHUB_OUTPUT REPO RUN_URL CI_RESULT E2E_RESULT E2E_PDP_LATEST_RESULT
  local delimiter
  delimiter="EOF_$(openssl rand -hex 16)"
  {
    echo "text<<${delimiter}"
    format_audit "${trees[@]}" --slack --repo "$REPO" --run-url "$RUN_URL" \
      --ci-result "$CI_RESULT" --e2e-result "$E2E_RESULT" \
      --e2e-pdp-latest-result "$E2E_PDP_LATEST_RESULT"
    echo "${delimiter}"
  } >>"$GITHUB_OUTPUT"
}

case ${1:-} in
  summary | gate | slack)
    require AUDIT_DIR AUDIT_TREES
    read -ra trees <<<"$AUDIT_TREES"
    "$@"
    ;;
  *)
    echo "usage: audit-report.sh summary CONTEXT | gate | slack" >&2
    exit 2
    ;;
esac
