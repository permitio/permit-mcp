#!/usr/bin/env bash
#
# Download a release tarball, check it against a pinned SHA-256 and extract one
# executable from it.
#
# Usage: fetch-binary.sh URL SHA256 NAME DEST
#
# Writes DEST/NAME, and adds DEST to GITHUB_PATH when it is set, so the job's later
# steps find NAME on PATH. Exits 1 when the download fails, the checksum differs or
# the tarball holds no executable NAME, and 2 on a wrong number of arguments.
# Nothing is extracted from a tarball whose checksum differs.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: fetch-binary.sh URL SHA256 NAME DEST" >&2
  exit 2
fi
url=$1 sha256=$2 name=$3 dest=$4

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
archive="$work/archive.tar.gz"

if ! curl -fsSL --retry 3 --retry-connrefused -o "$archive" "$url"; then
  echo "::error title=Download::Could not download ${url}."
  exit 1
fi
actual=$(sha256sum "$archive" | cut -d ' ' -f 1)
if [[ $actual != "$sha256" ]]; then
  echo "::error title=Download::${url} has SHA-256 ${actual}, not the pinned ${sha256}."
  exit 1
fi
mkdir -p "$dest"
if ! tar -xzf "$archive" -C "$work" "$name" || [[ ! -f $work/$name || ! -x $work/$name ]]; then
  echo "::error title=Download::${url} holds no executable ${name}."
  exit 1
fi
mv "$work/$name" "$dest/$name"
if [[ -n ${GITHUB_PATH:-} ]]; then
  echo "$dest" >>"$GITHUB_PATH"
fi
echo "Installed ${name} from ${url} at ${dest}/${name}."
