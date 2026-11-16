#!/usr/bin/env bash
# Acceptance check: a synthetic telemetry batch, sent from the world's own
# container, which intake attributes to the world by its address, is found in
# the database. See accept.sh for the ACCEPT_ variables.
set -euo pipefail

session=$("$(dirname "$0")/accept-player.sh" Steam_accept Accept 1.5 30.0 -2.5)
# psql binds :'session' from standard input, not from -c.
stored=$(docker exec -i "$ACCEPT_DATABASE" psql -h /run/postgresql -d vhd -tA -v ON_ERROR_STOP=1 \
  -v session="$session" <<'SQL'
SELECT w.name, p.x, p.z FROM position p JOIN session s ON s.id = p.session_id
JOIN world w ON w.id = s.world_id WHERE p.session_id = :'session';
SQL
)
if [[ $stored != "$ACCEPT_WORLD_NAME|1.5|-2.5" ]]; then
  echo "accept: the database holds '$stored' for the world's batch, not $ACCEPT_WORLD_NAME at 1.5, -2.5" >&2
  exit 1
fi
echo "accept: a batch from the world's container reached the database through intake"
