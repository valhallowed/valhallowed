#!/usr/bin/env bash
# Acceptance check: restoring a backup brings the world back with that backup's
# object count, and world truth passes. Runs after accept-95, which left the
# world stopped with a new backup of its save. The check damages that save, as
# a lost file would, by removing one of its chunks; asks for the newest
# backup's restore with vhd restore; and starts the world with vhd apply, as a
# server manager does. The world's runtime must report the restore done and
# the world running, so world truth passed the restored save, with the
# plugin's object count equal to the backup record's. The world keeps running
# for the checks after this one. See accept.sh for the ACCEPT_ variables.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
cli=(docker compose -p "$ACCEPT_PROJECT" -f "$ACCEPT_INSTALL/compose.yaml")
image=$(docker inspect --format '{{.Config.Image}}' "$ACCEPT_WORLD") ||
  { echo "accept: cannot read $ACCEPT_WORLD's image" >&2; exit 1; }
# The world is already installed and loads from a save, so its restart takes a
# few seconds to swap the backup in and about a minute to load. 4 minutes
# leaves room for a game update at the start; accept-85 counts the job's time.
wait_s=240

# On the stopped container's own mounts, as the runtime's user, as accept-96
# reads them. -I and the working directory /, so nothing in the world's data
# volume is imported. Prints the newest backup's name, its object count and the
# chunk it removed from the save.
picked=$(docker run --rm -i --label "com.docker.compose.project=$ACCEPT_PROJECT" --network none \
  --cap-drop ALL --security-opt no-new-privileges:true --volumes-from "$ACCEPT_WORLD" -w / \
  --entrypoint python3 "$image" -I - "$ACCEPT_WORLD_NAME" <<'PY'
import json, re, sys
from pathlib import Path

world = sys.argv[1]
backups = Path("/backups")
saves = Path("/data/saves/worlds_local") / world


def fail(why):
    print(f"accept: {why}", file=sys.stderr)
    sys.exit(1)


try:
    names = sorted(p.name for p in backups.iterdir() if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", p.name))
    if not names:
        fail(f"{backups} holds no backup to restore")
    record = json.loads((backups / names[-1] / "backup.json").read_text())
    chunks = sorted(entry["name"] for entry in record["files"] if entry["name"].endswith(".chunk"))
    # An empty save's restore cannot be told from a world that loaded nothing.
    if record["objects"] < 1 or not chunks:
        fail(f"{names[-1]} holds {record['objects']} objects in {len(chunks)} chunk(s); a restore needs some to show")
    (saves / chunks[0]).unlink()
except (OSError, ValueError, KeyError, TypeError) as error:
    fail(f"cannot pick the newest backup and damage the save: {type(error).__name__}: {error}")
print(names[-1], record["objects"], chunks[0])
PY
) || exit 1
read -r backup objects chunk <<< "$picked"
echo "accept: removed $chunk from the stopped world's save; its newest backup, $backup, holds $objects objects"

if ! asked=$(timeout 60 "${cli[@]}" run --rm vhd restore "$ACCEPT_WORLD_NAME" "$backup" 2>&1) ||
  [[ ! $asked =~ restore\ ([0-9a-f]{32})\ of ]]; then
  echo "accept: vhd restore $ACCEPT_WORLD_NAME $backup did not ask for a restore: $asked" >&2
  exit 1
fi
id=${BASH_REMATCH[1]}
# vhd apply's own wait for the containers is 300 s.
if ! applied=$(timeout 360 "${cli[@]}" run --rm vhd apply 2>&1); then
  echo "accept: vhd apply did not start the world for restore $id:" >&2
  echo "$applied" >&2
  exit 1
fi

entry=$("$root/scripts/accept-restored.sh" "$ACCEPT_PROJECT" "$ACCEPT_INSTALL" "$ACCEPT_WORLD_NAME" "$id" "$wait_s") ||
  exit 1
read -r state outcome counted < <(jq -r '[.state, .report.restore.outcome, (.report.objects // "none")] | @tsv' <<< "$entry")
if [[ $state != running || $outcome != "done" || $counted != "$objects" ]]; then
  echo "accept: restore $id of $backup left the world $state, the restore $outcome and $counted objects; expected running, done and $objects" >&2
  echo "$entry" >&2
  docker logs --tail 40 "$ACCEPT_WORLD" >&2 || true
  exit 1
fi
# The checks after this one find the world as accept.sh named it.
if [[ $(docker inspect --format '{{.State.Status}}' "$ACCEPT_WORLD" 2>/dev/null) != running ]]; then
  echo "accept: the world runs again, but not in $ACCEPT_WORLD: vhd apply replaced its container" >&2
  exit 1
fi
echo "accept: restore $id brought back $backup's $objects objects, and world truth passed the world"
