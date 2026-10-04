#!/usr/bin/env bash
#
# release.yml's verify job: checks the release as a consumer gets it from PyPI,
# once publish has uploaded it. Runs from the repository root with the release
# dependency group's environment on PATH (uv run --only-group release), which
# brings pypi-attestations.
#
# Usage: verify-release.sh DIST
#
# DIST is the dist artifact that build uploaded and scan admitted. TAG is the
# release tag (vX.Y.Z) and REPOSITORY the repository as owner/name.
#
#   1. Waits until PyPI's JSON API lists exactly the tag's wheel and sdist,
#      asking every POLL_SECONDS for at most WAIT_SECONDS.
#   2. Downloads both files, and compares each one's SHA-256 with DIST's file
#      and with the digest PyPI lists.
#   3. Downloads the provenance PyPI holds for each file (its Integrity API),
#      checks that every attestation bundle's publisher is GitHub, REPOSITORY,
#      release.yml and the pypi environment, and verifies them with
#      `pypi-attestations verify pypi`: the Sigstore certificate is for that
#      repository and workflow, the signature holds, and the attestation's
#      subject is the downloaded file.
#   4. Installs the downloaded wheel in a fresh environment and runs permit-mcp
#      with an empty environment, which must exit 2 with a configuration error.
#
# Exits 0 when all of that holds. Exits 1 on a mismatch: PyPI lists other files,
# a digest differs, a file has no attestation or one from another publisher, an
# attestation does not verify, or permit-mcp does not exit 2. Exits 2 when PyPI
# never served the version in time, DIST is not the tag's two files, a download
# failed, or a tool did not run. pypi-attestations exits 1 in every case; its
# output tells a failed verification ("Verification failed") from a failure to
# run, such as fetching Sigstore's trust root.
set -euo pipefail

PROJECT=permit-mcp
WORKFLOW=release.yml
ENVIRONMENT=pypi

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

# fetch URL OUT: writes the body of a GET of URL to OUT and prints the HTTP
# status. Fails when no response came, after curl's retries.
fetch() {
  curl --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --max-time 60 --retry 3 --output "$2" --write-out '%{http_code}' "$1"
}

sha256() {
  sha256sum "$1" | cut -d ' ' -f 1
}

require RUNNER_TEMP TAG REPOSITORY WAIT_SECONDS POLL_SECONDS
dist=${1:?usage: verify-release.sh DIST}
version=${TAG#v}
wheel=permit_mcp-${version}-py3-none-any.whl
sdist=permit_mcp-${version}.tar.gz
expected=$(printf '%s\n' "$wheel" "$sdist" | LC_ALL=C sort)
work=$RUNNER_TEMP/verify
result=0

shopt -s nullglob dotglob
built=("$dist"/*)
found=$(printf '%s\n' "${built[@]#"$dist"/}" | LC_ALL=C sort)
if [[ $found != "$expected" ]]; then
  echo "::error title=Verify::${dist} holds ${found//$'\n'/, }; expected" \
    "${expected//$'\n'/, }."
  exit 2
fi
if ! pypi-attestations --version; then
  echo "::error title=Verify::pypi-attestations did not run."
  exit 2
fi
mkdir -p "$work/pypi"

# 1. Wait for PyPI. The JSON API can list the version before its second file.
deadline=$((SECONDS + WAIT_SECONDS))
served=false
while :; do
  status=$(fetch "https://pypi.org/pypi/$PROJECT/$version/json" "$work/release.json") ||
    status="no response"
  listed=""
  if [[ $status == 200 ]]; then
    served=true
    listed=$(jq -r '.urls[].filename' "$work/release.json" | LC_ALL=C sort) || listed=""
    if [[ $listed == "$expected" ]]; then
      break
    fi
  fi
  if ((SECONDS >= deadline)); then
    if $served; then
      echo "::error title=Verify::After ${WAIT_SECONDS}s PyPI lists ${listed//$'\n'/, } for" \
        "${PROJECT} ${version}; the release built ${expected//$'\n'/, }."
      exit 1
    fi
    echo "::error title=Verify::PyPI did not serve ${PROJECT} ${version} within" \
      "${WAIT_SECONDS}s (last answer: ${status})."
    exit 2
  fi
  sleep "$POLL_SECONDS"
done
echo "PyPI lists ${expected//$'\n'/ and }."

# 2. Download each file and compare it with the one build made.
while IFS=$'\t' read -r name url digest; do
  status=$(fetch "$url" "$work/pypi/$name") || status="no response"
  if [[ $status != 200 ]]; then
    echo "::error title=Verify::Could not download ${name} from PyPI (${status})."
    exit 2
  fi
  downloaded=$(sha256 "$work/pypi/$name")
  made=$(sha256 "$dist/$name")
  if [[ $downloaded == "$made" && $digest == "$made" ]]; then
    echo "${name}: PyPI serves the file build made (SHA-256 ${made})."
  else
    echo "::error title=Verify::${name} differs: PyPI serves SHA-256 ${downloaded} and lists" \
      "${digest}; build made ${made}."
    result=1
  fi
done < <(jq -r '.urls[] | [.filename, .url, .digests.sha256] | @tsv' "$work/release.json")

# 3. Verify each file's attestations against this repository and workflow.
# Every bundle holds an attestation and names this repository's release.yml and the pypi
# environment as its publisher. The $ names are jq's arguments, not the shell's.
# shellcheck disable=SC2016
from_this_workflow='(.attestation_bundles | length > 0) and all(.attestation_bundles[];
  (.publisher | {kind, repository, workflow, environment})
    == {kind: "GitHub", repository: $repo, workflow: $workflow, environment: $environment}
  and (.attestations | length > 0))'
for name in "$wheel" "$sdist"; do
  provenance=$work/$name.provenance.json
  status=$(fetch "https://pypi.org/integrity/$PROJECT/$version/$name/provenance" \
    "$provenance") || status="no response"
  case $status in
    200) ;;
    404)
      echo "::error title=Verify::PyPI holds no attestations for ${name}."
      result=1
      continue
      ;;
    *)
      echo "::error title=Verify::Could not download the provenance of ${name} (${status})."
      exit 2
      ;;
  esac
  if ! jq -e --arg repo "$REPOSITORY" --arg workflow "$WORKFLOW" \
    --arg environment "$ENVIRONMENT" "$from_this_workflow" "$provenance" >/dev/null; then
    echo "::error title=Verify::The attestations of ${name} are not all from GitHub," \
      "${REPOSITORY}, ${WORKFLOW} and the ${ENVIRONMENT} environment:" \
      "$(jq -c '[.attestation_bundles[]?.publisher]' "$provenance" 2>&1)."
    result=1
    continue
  fi
  verify=0
  output=$(pypi-attestations verify pypi --repository "https://github.com/$REPOSITORY" \
    --provenance-file "$provenance" "$work/pypi/$name" 2>&1) || verify=$?
  echo "$output"
  if [[ $verify -ne 0 ]]; then
    if ! grep -q 'Verification failed' <<<"$output"; then
      echo "::error title=Verify::pypi-attestations did not verify ${name}; see the log."
      exit 2
    fi
    echo "::error title=Verify::The attestations of ${name} do not verify."
    result=1
  fi
done

# 4. Install the wheel PyPI serves, as a consumer does, and run it unconfigured.
venv=$work/venv
if ! uv venv --quiet "$venv" ||
  ! uv pip install --quiet --python "$venv/bin/python" --exclude-newer false \
    "$work/pypi/$wheel"; then
  echo "::error title=Verify::Could not install ${wheel} from PyPI in a fresh environment."
  exit 2
fi
status=0
env -i "$venv/bin/permit-mcp" </dev/null 2>"$work/smoke.txt" || status=$?
cat "$work/smoke.txt"
if [[ $status -ne 2 ]] || ! grep -q 'configuration error' "$work/smoke.txt"; then
  echo "::error title=Verify::permit-mcp from PyPI's wheel exited ${status} with no" \
    "configuration; expected 2 and a configuration error."
  result=1
fi

if [[ $result -eq 0 ]]; then
  echo "Verified ${PROJECT} ${version} on PyPI: the files build made, attested by" \
    "${REPOSITORY}'s ${WORKFLOW}, and the wheel runs."
fi
exit "$result"
