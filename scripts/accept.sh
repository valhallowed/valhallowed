#!/usr/bin/env bash
# Acceptance: install from a rendered compose file and check that it runs the
# expected commit. Pulls from the registry by default, so an image that was never
# pushed, or is not public, cannot pass. VHD_PULL=never accepts a local build.
set -euo pipefail

[[ $# -eq 2 ]] || { echo "usage: $0 COMPOSE_FILE COMMIT" >&2; exit 2; }
compose_file=$1 expected=$2
compose=(docker compose -p vhd-accept -f "$compose_file")

trap '"${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true' EXIT
"${compose[@]}" up --detach --pull "${VHD_PULL:-always}" --wait --wait-timeout 60

got=$(curl -fsS --max-time 5 http://127.0.0.1:8456/build)
if [[ $got != "$expected" ]]; then
  echo "accept: expected build $expected, got $got" >&2
  exit 1
fi
echo "accept: $compose_file installed and running build $got"
