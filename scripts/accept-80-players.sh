#!/usr/bin/env bash
# Acceptance check: a synthetic player's join and position, sent through intake
# in the world's newest session, come back from the web map's GET /api/players,
# the only live player of the only world, in an answer that passes
# contracts/web-map.players.schema.json. The web map serves an answer for up to
# 2 s, so the check reads until the player shows. See accept.sh for the ACCEPT_
# variables.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
"$root/scripts/accept-player.sh" Steam_accept_map Mapwalker 4.5 31.0 -6.5 >/dev/null

expected=$(jq -cn --arg world "$ACCEPT_WORLD_NAME" \
  '{contract: 1, worlds: [{name: $world, players: [{name: "Mapwalker", x: 4.5, y: 31.0, z: -6.5}]}]}')
answer='' found=''
for _ in $(seq 15); do
  if answer=$(curl -fsS --max-time 5 "http://127.0.0.1:$ACCEPT_WEB_MAP_PORT/api/players") &&
    jq -e --argjson want "$expected" '. == $want' <<< "$answer" >/dev/null 2>&1; then
    found=1
    break
  fi
  sleep 2
done
if [[ -z $found ]]; then
  echo "accept: the web map answered GET /api/players with '$answer', not $expected" >&2
  docker logs --tail 40 "$ACCEPT_WEB_MAP" >&2 || true
  exit 1
fi

# The contract is checked as scripts/check-schema.sh checks its examples.
if ! out=$(docker run --rm -i -v "$root:/repo:ro" -w /repo "$("$root/scripts/tool-image.sh" python)" sh -c '
  pip install --quiet --disable-pip-version-check --root-user-action=ignore --no-deps -r scripts/requirements-check-schema.txt &&
  check-jsonschema --schemafile contracts/web-map.players.schema.json -' <<< "$answer" 2>&1); then
  echo "accept: the web map's answer to GET /api/players breaks its contract:" >&2
  echo "$out" >&2
  exit 1
fi
echo "accept: a synthetic player sent through intake came back from the web map's GET /api/players"
