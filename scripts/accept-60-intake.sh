#!/usr/bin/env bash
# Acceptance check: a synthetic telemetry batch, sent from the world's own
# container, which intake attributes to the world by its address, is found in
# the database. See accept.sh for the ACCEPT_ variables.
set -euo pipefail

session=$(cat /proc/sys/kernel/random/uuid)
answer=$(docker exec -i "$ACCEPT_WORLD" python3 - "$session" <<'PY'
import json, sys, urllib.error, urllib.request
at = "2026-01-01T00:00:00Z"
player = {"id": "Steam_accept"}
events = [
    {"kind": "player.join", "at": at, "player": {**player, "name": "Accept"}},
    {"kind": "player.position", "at": at, "player": player, "x": 1.5, "y": 30.0, "z": -2.5},
]
body = json.dumps({"contract": 1, "session": sys.argv[1], "sent_at": at, "events": events}).encode()
request = urllib.request.Request("http://intake:9458/events", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        print(response.status, response.read().decode())
except urllib.error.HTTPError as error:
    print(error.code, error.read().decode())
PY
)
if [[ $answer != '202 {"accepted": 2, "dropped": 0}' ]]; then
  echo "accept: intake answered the world's batch with: $answer" >&2
  docker logs --tail 40 "$ACCEPT_INTAKE" >&2 || true
  exit 1
fi
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
