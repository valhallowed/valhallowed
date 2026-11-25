#!/usr/bin/env bash
# Acceptance check: an exported world, imported into a world that has never
# started, loads with its objects. vhd export copies the world's newest backup
# into the install folder; the export goes into a second install, made the way
# accept.sh makes the first, with a world of the same save name, which the
# plugin reports as the loaded world's; vhd import keeps it as a backup there
# before that install's first vhd apply; and vhd apply starts the install. The
# second world's runtime must report the import's restore done and the world
# running, so world truth passed it, with the plugin's object count equal to
# the export's. The second install uses ports of its own, away from the first
# install's, and goes when the check ends. See accept.sh for the ACCEPT_
# variables.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
cli=(docker compose -p "$ACCEPT_PROJECT" -f "$ACCEPT_INSTALL/compose.yaml")
project=$ACCEPT_PROJECT-import
world_port=$((ACCEPT_WORLD_PORT + 10)) web_map_port=$((ACCEPT_WEB_MAP_PORT + 10))
# The new world downloads the dedicated server, about a minute and a half in
# CI, then loads the save in about half a minute. 7 minutes leaves room for a
# slow download; accept-85 counts the job's time.
wait_s=420

# The save, as vhd prints it: its number, then its object count.
objects_of() {
  [[ $1 =~ save\ [0-9]+\ \(([0-9]+)\ objects, ]] && echo "${BASH_REMATCH[1]}"
}

if ! exported=$(timeout 300 "${cli[@]}" run --rm vhd export "$ACCEPT_WORLD_NAME" 2>&1) ||
  [[ ! $exported =~ is\ in\ (exports/"$ACCEPT_WORLD_NAME"/[^/]+)/worlds_local/"$ACCEPT_WORLD_NAME", ]]; then
  echo "accept: vhd export $ACCEPT_WORLD_NAME did not export its newest backup: $exported" >&2
  exit 1
fi
from=$ACCEPT_INSTALL/${BASH_REMATCH[1]}
objects=$(objects_of "$exported") || { echo "accept: vhd export gave no object count: $exported" >&2; exit 1; }
echo "accept: vhd export copied $ACCEPT_WORLD_NAME's newest backup, $objects objects, into ${from#"$ACCEPT_INSTALL/"}"

install=$(mktemp -d)
trap '"$root/scripts/accept-teardown.sh" "$project" "$install"' EXIT
imported=(docker compose -p "$project" -f "$install/compose.yaml")
"$root/scripts/accept-install.sh" "$ACCEPT_INSTALL/compose.yaml" "$install" "$ACCEPT_WORLD_NAME" "$world_port" "$web_map_port"
# -p keeps each file's modification time, which the import takes as when the save was completed.
cp -Rp "$from/worlds_local" "$install/worlds_local"
chmod -R u+rwX,go+rX,go-w "$install/worlds_local"

if ! said=$(timeout 300 "${imported[@]}" run --rm vhd import "$ACCEPT_WORLD_NAME" worlds_local 2>&1) ||
  [[ ! $said =~ restore\ ([0-9a-f]{32})\ of\ it\ is\ asked\ for ]] ||
  [[ $said != *'has no container yet'* ]]; then
  echo "accept: vhd import did not keep the export for a world that never started: $said" >&2
  exit 1
fi
id=${BASH_REMATCH[1]}
if [[ $(objects_of "$said") != "$objects" ]]; then
  echo "accept: vhd import kept a save of other than the export's $objects objects: $said" >&2
  exit 1
fi
# vhd apply's own wait for the containers is 300 s.
if ! applied=$(timeout 360 "${imported[@]}" run --rm vhd apply 2>&1); then
  echo "accept: vhd apply did not start the second install:" >&2
  echo "$applied" >&2
  exit 1
fi

entry=$("$root/scripts/accept-restored.sh" "$project" "$install" "$ACCEPT_WORLD_NAME" "$id" "$wait_s") || exit 1
read -r state outcome counted < <(jq -r '[.state, .report.restore.outcome, (.report.objects // "none")] | @tsv' <<< "$entry")
if [[ $state != running || $outcome != "done" || $counted != "$objects" ]]; then
  echo "accept: the imported world is $state, its restore $outcome, with $counted objects; expected running, done and $objects" >&2
  echo "$entry" >&2
  exit 1
fi
echo "accept: the export, imported before the second install's first vhd apply, loaded there with its $objects objects, and world truth passed it"
