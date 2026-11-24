#!/usr/bin/env bash
# Acceptance helper, not a check: remove an install the acceptance made, and
# whatever a run of it left, a failed first apply included: everything its
# Compose project labels, then its folder. The worlds' backup folders belong to
# root and the game runtime's user, so vhd, which runs as root, removes them
# before the install folder goes. It runs from an exit trap, so it never fails.
set -uo pipefail

[[ $# -eq 2 ]] || { echo "usage: $0 PROJECT INSTALL" >&2; exit 2; }
project=$1 install=$2
cli=(docker compose -p "$project" -f "$install/compose.yaml")
label="label=com.docker.compose.project=$project"

"${cli[@]}" run --rm --entrypoint rm vhd -rf /install/backups >/dev/null 2>&1 || true
"${cli[@]}" --profile tools down --volumes --remove-orphans >/dev/null 2>&1 || true
docker ps -aq --filter "$label" | xargs -r docker rm --force >/dev/null 2>&1 || true
docker volume ls -q --filter "$label" | xargs -r docker volume rm --force >/dev/null 2>&1 || true
docker network ls -q --filter "$label" | xargs -r docker network rm >/dev/null 2>&1 || true
rm -rf "$install"
exit 0
