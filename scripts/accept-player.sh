#!/usr/bin/env bash
# Acceptance helper, not a check: a synthetic player joins and is seen at X Y Z,
# in a new session of the world's, sent as one telemetry batch from the world's
# own container, which intake attributes to the world by its address. Prints
# the session id. The web map reads only the world's newest session, so a check
# that reads the player back from it runs after accept-70, once the plugin's
# own session is stored. See accept.sh for the ACCEPT_ variables.
set -euo pipefail

[[ $# -eq 5 ]] || { echo "usage: $0 PLAYER_ID NAME X Y Z" >&2; exit 2; }

session=$(cat /proc/sys/kernel/random/uuid)
answer=$(docker exec -i "$ACCEPT_WORLD" python3 - "$session" "$@" <<'PY'
import json, sys, urllib.error, urllib.request
session, player_id, name, x, y, z = sys.argv[1:]
at = "2026-01-01T00:00:00Z"
player = {"id": player_id}
events = [
    {"kind": "player.join", "at": at, "player": {**player, "name": name}},
    {"kind": "player.position", "at": at, "player": player, "x": float(x), "y": float(y), "z": float(z)},
]
body = json.dumps({"contract": 1, "session": session, "sent_at": at, "events": events}).encode()
request = urllib.request.Request("http://intake:9458/events", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        print(response.status, response.read().decode())
except urllib.error.HTTPError as error:
    print(error.code, error.read().decode())
except OSError as error:
    print("unreached:", error)
PY
)
if [[ $answer != '202 {"accepted": 2, "dropped": 0}' ]]; then
  echo "accept: intake answered the world's batch with: $answer" >&2
  docker logs --tail 40 "$ACCEPT_INTAKE" >&2 || true
  exit 1
fi
echo "$session"
