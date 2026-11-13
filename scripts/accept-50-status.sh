#!/usr/bin/env bash
# Acceptance check: once the world runs, vhd status shows the data layer's
# services running. See accept.sh for the ACCEPT_ variables.
set -euo pipefail

for entry in "database:$ACCEPT_DATABASE" "intake:$ACCEPT_INTAKE" "web-map:$ACCEPT_WEB_MAP"; do
  service=${entry%%:*} container=${entry#*:}
  if ! grep -q "^$service service: running" <<< "$ACCEPT_STATUS"; then
    echo "accept: vhd status does not show the $service service running:" >&2
    echo "$ACCEPT_STATUS" >&2
    docker logs --tail 40 "$container" >&2 || true
    exit 1
  fi
done
