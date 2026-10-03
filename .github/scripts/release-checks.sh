#!/usr/bin/env bash
#
# The checks that hold a release and its site to a published vMAJOR.MINOR.PATCH
# release tag, run from the repository root. The tag reaches them through the
# environment, and is printed quoted when it is no version tag.
#
# Usage: release-checks.sh tag | files DIST | ref
#
#   tag  release.yml's first job. On a release EVENT, fails a PRERELEASE other
#       than false, a TAG that is not the whole string v + a version, a TAG
#       other than v + the version in pyproject.toml, and a tagged commit (HEAD)
#       that is not on origin/main. On any other event, a dry run, it only
#       reports the version. Exits 1 on any of those, and 2 when the version or
#       origin/main cannot be read.
#   files DIST  release.yml's publish job: DIST holds exactly the wheel and the
#       sdist of TAG's version, and nothing else. Exits 1 otherwise.
#   ref  pages.yml's build job: the run's ref (REF_TYPE, TAG) is a version tag
#       of a published release that is neither a draft nor a pre-release, as
#       `gh release view` shows it. Exits 1 otherwise.
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

version_tag='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'

tag() {
  require EVENT TAG PRERELEASE
  local version status=0
  if ! version=$(uv version --short --frozen) || [[ -z $version ]]; then
    echo "::error title=Release tag::Could not read the version from pyproject.toml."
    exit 2
  fi
  if [[ $EVENT != release ]]; then
    echo "::notice title=Dry run::Building and scanning ${version}; nothing is published."
    exit 0
  fi
  if [[ $PRERELEASE != false ]]; then
    echo "::error title=Release tag::The release is marked as a pre-release; PyPI would" \
      "publish ${version} as final. Publish it as a full release."
    exit 1
  fi
  if [[ ! $TAG =~ $version_tag ]]; then
    echo "::error title=Release tag::The tag $(printf '%q' "$TAG") is not a version tag such" \
      "as v1.2.3."
    exit 1
  fi
  if [[ ${TAG#v} != "$version" ]]; then
    echo "::error title=Release tag::The tag is $TAG, but pyproject.toml's version is" \
      "${version}. Bump the version, or tag v${version}."
    exit 1
  fi
  git merge-base --is-ancestor HEAD origin/main || status=$?
  case $status in
    0) echo "Releasing ${version} from ${TAG}, on main." ;;
    1)
      echo "::error title=Release tag::$TAG is not on main. Tag a commit of main."
      exit 1
      ;;
    *)
      echo "::error title=Release tag::Could not read origin/main."
      exit 2
      ;;
  esac
}

files() {
  require TAG
  local dist=${1:?usage: release-checks.sh files DIST} found expected version=${TAG#v}
  shopt -s nullglob dotglob
  local paths=("$dist"/*)
  found=$(printf '%s\n' "${paths[@]#"$dist"/}" | LC_ALL=C sort)
  expected=$(printf '%s\n' "permit_mcp-${version}-py3-none-any.whl" \
    "permit_mcp-${version}.tar.gz" | LC_ALL=C sort)
  if [[ $found != "$expected" ]]; then
    echo "::error title=Publish::${dist} holds ${found//$'\n'/, }; expected" \
      "${expected//$'\n'/, }."
    exit 1
  fi
  echo "Uploading ${found//$'\n'/ and }."
}

ref() {
  require REF_TYPE TAG GH_TOKEN GH_REPO
  local quoted release state
  printf -v quoted '%q' "$TAG"
  if [[ $REF_TYPE != tag ]]; then
    echo "::error title=Pages::The site deploys from a release tag only; this run is on the" \
      "${REF_TYPE} ${quoted}. Run it on a tag such as v1.2.3."
    exit 1
  fi
  if [[ ! $TAG =~ $version_tag ]]; then
    echo "::error title=Pages::The tag $quoted is not a version tag such as v1.2.3."
    exit 1
  fi
  if ! release=$(gh release view "$TAG" --json isPrerelease,isDraft); then
    echo "::error title=Pages::No published release has the tag $TAG."
    exit 1
  fi
  state=$(jq -r '"draft=\(.isDraft) prerelease=\(.isPrerelease)"' <<<"$release")
  if [[ $state != "draft=false prerelease=false" ]]; then
    echo "::error title=Pages::The release $TAG is not a published full release (${state})."
    exit 1
  fi
  echo "Deploying the site of the published release ${TAG}."
}

case ${1:-} in
  tag | files | ref) "$@" ;;
  *)
    echo "usage: release-checks.sh tag | files DIST | ref" >&2
    exit 2
    ;;
esac
