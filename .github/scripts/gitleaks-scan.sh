#!/usr/bin/env bash
#
# Scans the whole history of the checkout in the working directory with
# gitleaks, reading no configuration from it. The gitleaks job of ci.yml runs
# it.
#
# Usage: gitleaks-scan.sh GITLEAKS
#
# GITLEAKS is the gitleaks binary; EVENT is the event's name; RUNNER_TEMP holds
# the scan's files. A pull request controls the checkout, so gitleaks reads
# nothing from it: not its .gitleaks.toml (the default rules come from a config
# written here), not its gitleaks:allow comments, and not its .gitleaksignore.
# gitleaks reads a .gitleaksignore from the directory it scans whatever
# --gitleaks-ignore-path says, so it scans a bare mirror of the history, which
# has no working tree, with the ignore path an empty directory.
#
# The one exception is .github/gitleaks-accept.txt: exact fingerprints
# (commit:file:rule:line) of reviewed false positives, each with a reason. A
# fingerprint names one commit, so an entry can never hide a secret committed
# later, and every entry is printed as a notice. On a pull request the list is
# read from the base, HEAD^1 (the merge commit's first parent), so a pull
# request cannot accept a secret it commits itself; an entry it adds counts once
# it is merged. When the base has no list at all, as before the pull request
# that adds the file merges, the pull request's own list is read, with a
# warning. On other events the list is read from HEAD.
#
# gitleaks's own commit count depends on the git log format and can read 0, so
# the proof that it ran is the mirror's commit count and the bytes gitleaks
# reports. Exits 1 on a finding, and 2 when the checkout is shallow, the history
# is empty, a pull request's HEAD has no parent, nothing was scanned, or the
# accept list has a line that is not a fingerprint and a reason.
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

require RUNNER_TEMP EVENT
gitleaks=${1:?usage: gitleaks-scan.sh GITLEAKS}
accept=.github/gitleaks-accept.txt
accept_line='^([0-9a-f]{40}:[^:[:space:]]+:[a-z0-9-]+:[0-9]+)[[:space:]]+#[[:space:]]*[^[:space:]]'

if [[ $(git rev-parse --is-shallow-repository) != false ]]; then
  echo "::error title=gitleaks::The checkout is shallow, so the history is incomplete."
  exit 2
fi
history="$RUNNER_TEMP/history.git"
git clone --quiet --mirror . "$history"
commits=$(git -C "$history" rev-list --all --count)
if [[ ${commits:-0} -eq 0 ]]; then
  echo "::error title=gitleaks::The history holds no commit to scan."
  exit 2
fi
config="$RUNNER_TEMP/gitleaks.toml"
printf '[extend]\nuseDefault = true\n' >"$config"
ignores="$RUNNER_TEMP/gitleaks-ignores"
mkdir -p "$ignores"
: >"$ignores/.gitleaksignore"

source=HEAD
if [[ $EVENT == pull_request ]]; then
  if ! git rev-parse --verify --quiet 'HEAD^1^{commit}' >/dev/null; then
    echo "::error title=gitleaks::HEAD has no parent, so there is no base to read ${accept} from."
    exit 2
  fi
  if [[ -n $(git ls-tree --name-only HEAD^1 -- "$accept") ]]; then
    source=HEAD^1
  elif [[ -n $(git ls-tree --name-only HEAD -- "$accept") ]]; then
    echo "::warning title=gitleaks::The base (HEAD^1) has no ${accept}, so the pull request's" \
      "own list is read. Review every entry in it."
  fi
fi
if [[ -n $(git ls-tree --name-only "$source" -- "$accept") ]]; then
  echo "Reading ${accept} from ${source}."
  git show "${source}:${accept}" >"$RUNNER_TEMP/gitleaks-accept.txt"
  line_no=0
  while IFS= read -r line || [[ -n $line ]]; do
    line_no=$((line_no + 1))
    [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
    if [[ ! $line =~ $accept_line ]]; then
      echo "::error title=gitleaks::${source}:${accept}:${line_no} is not a fingerprint and a" \
        "reason."
      exit 2
    fi
    echo "${BASH_REMATCH[1]}" >>"$ignores/.gitleaksignore"
    echo "::notice title=gitleaks::Accepted as a false positive: ${line}"
  done <"$RUNNER_TEMP/gitleaks-accept.txt"
fi

status=0
"$gitleaks" git --config "$config" --gitleaks-ignore-path "$ignores" --ignore-gitleaks-allow \
  --redact --no-banner --no-color --verbose --exit-code 1 "$history" 2>&1 |
  tee "$RUNNER_TEMP/gitleaks.txt" || status=$?
bytes=$(sed -nE 's/.* scanned ~([0-9]+) bytes.*/\1/p' "$RUNNER_TEMP/gitleaks.txt" | tail -n 1)
if [[ ${bytes:-0} -eq 0 ]]; then
  echo "::error title=gitleaks::gitleaks scanned nothing in ${commits} commits."
  exit 2
fi
if [[ $status -ne 0 ]]; then
  echo "::error title=gitleaks::Secrets found in the history; see the log. Rotate each one;" \
    "removing it from the files does not remove it from history."
  exit 1
fi
echo "gitleaks scanned ${bytes} bytes of ${commits} commits and found no secret."
