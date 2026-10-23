#!/usr/bin/env bash
# Print the pinned image a check runs in, by its stage name in
# images/tools/Dockerfile: python, actionlint or shellcheck.
set -euo pipefail

[[ $# -eq 1 ]] || { echo "usage: $0 NAME" >&2; exit 2; }
root=$(cd "$(dirname "$0")/.." && pwd)
ref=$(awk -v name="$1" '$1 == "FROM" && $3 == "AS" && $4 == name { print $2 }' "$root/images/tools/Dockerfile")
if [[ -z $ref ]]; then
  echo "tool-image: images/tools/Dockerfile names no image $1" >&2
  exit 1
fi
echo "$ref"
