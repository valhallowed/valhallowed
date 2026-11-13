#!/usr/bin/env bash
# Acceptance check: the plugin's own session reaches the database. The plugin
# starts one when the world loads and sends session.start until intake accepts
# it, so it arrives within a few of the sender's 5 s rounds. Only the plugin
# sends session.start. See accept.sh for the ACCEPT_ variables.
set -euo pipefail

started=''
for _ in $(seq 12); do
  started=$(docker exec "$ACCEPT_DATABASE" psql -h /run/postgresql -d vhd -tAc \
    "SELECT s.id FROM session s JOIN world w ON w.id = s.world_id
     WHERE w.name = '$ACCEPT_WORLD_NAME' AND s.started_at IS NOT NULL ORDER BY s.received_at DESC LIMIT 1")
  [[ -n $started ]] && break
  sleep 5
done
if [[ -z $started ]]; then
  echo "accept: the database holds no session the plugin started for $ACCEPT_WORLD_NAME" >&2
  docker logs "$ACCEPT_WORLD" 2>&1 | grep -F ':Valhallowed]' | tail -20 >&2 || true
  exit 1
fi
echo "accept: the plugin's session $started reached the database through intake"
