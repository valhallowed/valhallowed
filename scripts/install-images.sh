#!/usr/bin/env bash
# Print the images an install's compose file lists in its x-vhd block, one per
# line, or with NAME only that one: the single reading of the list that
# promotion, acceptance, releases and the checks share.
set -euo pipefail

[[ $# -eq 1 || $# -eq 2 ]] || { echo "usage: $0 COMPOSE_FILE [NAME]" >&2; exit 2; }
docker compose -f "$1" config --format json | jq -r --arg name "${2:-}" \
  'if $name == "" then (."x-vhd".images // {} | .[]) else (."x-vhd".images[$name] // empty) end'
