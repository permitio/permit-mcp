#!/usr/bin/env bash
#
# Scan this package's dependencies for known vulnerabilities with Trivy.
#
# Usage: audit-deps.sh <output-dir>
#
# Writes four dependency trees to <output-dir>, each as a directory holding a
# file named requirements.txt, and one Trivy JSON report per tree:
#
#   runtime-ceiling/  + trivy-runtime-ceiling.json
#       [project].dependencies alone, newest resolution: what a fresh
#       `pip install permit-mcp` gets today.
#   runtime-floor/    + trivy-runtime-floor.json
#       [project].dependencies alone, lowest-direct: the lowest versions the
#       published ranges permit, which a consumer can still end up with.
#   dev-ceiling/      + trivy-dev-ceiling.json
#       [project].dependencies plus the dev group, newest resolution. Test and
#       lint tooling only; it never ships to a user.
#   docs-ceiling/     + trivy-docs-ceiling.json
#       [project].dependencies plus the docs group, newest resolution. What
#       builds the API reference site in CI and the Pages workflow; it never
#       ships to a user.
#
# The trees are compiled from pyproject.toml, not exported from uv.lock: the
# lock pins this repository's own environment, while consumers resolve the
# published ranges.
#
# Exits 1 when a tree does not resolve or resolves to almost nothing, when the
# runtime-floor tree is not at the declared floors (check_floors.py), and when
# Trivy fails. Findings do not fail this script: format_audit.py --gate makes
# that decision from the reports, so the summary and the gate cannot disagree.
# The Python scripts run on the floor Python through `uv run --no-project`.
set -euo pipefail

OUT="${1:?usage: audit-deps.sh <output-dir>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# The floor of requires-python. A consumer on the lowest supported Python can
# get the lowest versions, and an advisory that affects a higher floor almost
# always affects the lower one too.
PYTHON_VERSION="${AUDIT_PYTHON_VERSION:-3.11}"

# The runtime tree is about 35 packages. Fewer than this means the compile
# produced an empty or truncated file, which Trivy would scan as clean.
MIN_PACKAGES=10

# The trees compiled, in order, for the Trivy loop below.
TREES=()

compile_tree() {
  local name="$1" resolution="$2"
  shift 2
  mkdir -p "${OUT}/${name}"
  # --no-sources: the published build ignores [tool.uv.sources]. --exclude-newer
  # false: the publish-age cooldown in [tool.uv] applies to this repository's
  # uv.lock only; consumers resolve against the index as it is today.
  local args=(
    --no-sources
    --exclude-newer false
    --python-version "${PYTHON_VERSION}"
    --quiet
    -o "${OUT}/${name}/requirements.txt"
  )
  if [[ -n ${resolution} ]]; then
    args+=(--resolution "${resolution}")
  fi
  uv pip compile "$@" "${args[@]}"

  local count
  count=$(grep -c '^[^#[:space:]].*==' "${OUT}/${name}/requirements.txt" || true)
  if [[ ${count:-0} -lt ${MIN_PACKAGES} ]]; then
    echo "::error title=Dependency resolution failed::Tree '${name}' resolved only" \
      "${count:-0} packages (expected at least ${MIN_PACKAGES}). Refusing to scan it."
    exit 1
  fi
  echo "${name}: ${count} packages"
  TREES+=("${name}")
}

echo "::group::Resolving dependency trees (Python ${PYTHON_VERSION})"
# lowest-direct, not lowest: the declared ranges go to their floor while the
# transitive dependencies resolve normally. Plain `lowest` would pull every
# transitive dependency back to its first release.
# The runtime trees are compiled without the dev group, so a dev tool cannot
# raise a runtime floor and hide what a consumer can install.
compile_tree runtime-ceiling "" "${REPO_ROOT}/pyproject.toml"
compile_tree runtime-floor "lowest-direct" "${REPO_ROOT}/pyproject.toml"
# lowest-direct moves past a floor it cannot install without saying so.
uv run --no-project --python "${PYTHON_VERSION}" python \
  "${REPO_ROOT}/.github/scripts/check_floors.py" "${REPO_ROOT}/pyproject.toml" \
  "${OUT}/runtime-floor/requirements.txt"
compile_tree dev-ceiling "" "${REPO_ROOT}/pyproject.toml" \
  --group "${REPO_ROOT}/pyproject.toml:dev"
compile_tree docs-ceiling "" "${REPO_ROOT}/pyproject.toml" \
  --group "${REPO_ROOT}/pyproject.toml:docs"

# The package count cannot tell a group's tree from a runtime one: a --group
# that matched nothing would scan the runtime tree again.
if ! grep -q '^pytest==' "${OUT}/dev-ceiling/requirements.txt"; then
  echo "::error title=Dependency resolution failed::Tree 'dev-ceiling' has no pytest," \
    "so the dev group was not resolved."
  exit 1
fi
if ! grep -q '^zensical==' "${OUT}/docs-ceiling/requirements.txt"; then
  echo "::error title=Dependency resolution failed::Tree 'docs-ceiling' has no zensical," \
    "so the docs group was not resolved."
  exit 1
fi
echo "::endgroup::"

# No --exit-code: the reports are written whatever they hold. Trivy reads
# trivy.yaml and .trivyignore from the working directory, which a pull request
# controls, and either could lower the severities or drop advisories without a
# trace: --config /dev/null and --ignorefile /dev/null read neither, and every
# severity is asked for. --list-all-pkgs lists the packages scanned, which
# format_audit.py counts against the tree.
for tree in "${TREES[@]}"; do
  echo "::group::Trivy scan (${tree})"
  trivy fs \
    --config /dev/null \
    --scanners vuln \
    --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL \
    --list-all-pkgs \
    --format json \
    --ignorefile /dev/null \
    --output "${OUT}/trivy-${tree}.json" \
    --quiet \
    "${OUT}/${tree}"
  echo "::endgroup::"
done
