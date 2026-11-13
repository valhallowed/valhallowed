#!/usr/bin/env bash
# Acceptance check: vhd validate, run from the control plane's image against the
# installed compose file and manifest, reconciles the manifest: it reports the
# world's ports and password state and the web map's port as the manifest sets
# them. See accept.sh for the ACCEPT_ variables.
set -euo pipefail

cli=(docker compose -p "$ACCEPT_PROJECT" -f "$ACCEPT_INSTALL/compose.yaml")

# Compose reports its own progress on stderr; vhd's answer is stdout.
expected_validate=$(printf '%s\n' \
  "world $ACCEPT_WORLD_NAME: udp $ACCEPT_WORLD_PORT-$((ACCEPT_WORLD_PORT + 2)), password unset" \
  "web map: tcp $ACCEPT_WEB_MAP_PORT")
if ! got=$("${cli[@]}" run --rm vhd validate) || [[ $got != "$expected_validate" ]]; then
  echo "accept: vhd validate did not reconcile the manifest:" >&2
  echo "$got" >&2
  exit 1
fi
echo "accept: vhd validate reconciled the manifest"
