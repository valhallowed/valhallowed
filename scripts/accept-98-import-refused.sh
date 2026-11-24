#!/usr/bin/env bash
# Acceptance check: a refused import leaves the world's save as it was, and
# says why. The save put in the install folder is the world's newest backup
# with one of its chunks missing, which the game runtime image's import action
# refuses only once it reads the save's .chunks file. vhd import must exit
# non-zero, its reason naming the missing chunk; the world's save folder must
# hold the same files, byte for byte; the world's backups folder must gain no
# backup an import took and keep no import's copy; and no new restore may be
# asked for. Runs after accept-97, on the running world, which an import never
# stops; the game saves again only 30 minutes after accept-97's load, so the
# world's save holds still meanwhile. See accept.sh for the ACCEPT_ variables.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
cli=(docker compose -p "$ACCEPT_PROJECT" -f "$ACCEPT_INSTALL/compose.yaml")
folder=refused-import

# What a refused import must leave as it was: each file of the world's save
# with its SHA-256, each backup an import took, and every import's copy, which
# vhd import's action names .import-<id> until it is whole. The world's own
# backups keep coming, since accept-97's restore backs its save up again, so
# its copies under way (.partial-) and a record renamed away are passed over.
# Anything in the save folder other than a file, such as a folder of the
# game's own, counts by name.
held() {
  timeout 60 docker exec -i "$ACCEPT_WORLD" python3 -I - "$ACCEPT_WORLD_NAME" <<'PY'
import hashlib, json, sys
from pathlib import Path

saves = Path("/data/saves/worlds_local") / sys.argv[1]
backups = Path("/backups")
for path in sorted(saves.iterdir()):
    if path.is_file() and not path.is_symlink():
        print("save", path.name, hashlib.sha256(path.read_bytes()).hexdigest())
    else:
        print("save", path.name, "no file")
for path in sorted(backups.iterdir()):
    if path.name.startswith(".import-"):
        print("copy", path.name)
    elif not path.name.startswith("."):
        try:
            record = json.loads((path / "backup.json").read_text())
        except FileNotFoundError:
            continue
        if record.get("source") == "import":
            print("imported", path.name)
PY
}
kept() {
  "$root/scripts/accept-status.sh" "$ACCEPT_PROJECT" "$ACCEPT_INSTALL" |
    jq -r --arg world "$ACCEPT_WORLD_NAME" '.worlds[$world].restore.id // "none"'
}

before=$(held) || { echo "accept: cannot read $ACCEPT_WORLD_NAME's save and backups" >&2; exit 1; }
kept_before=$(kept) || { echo "accept: cannot read the control plane's status" >&2; exit 1; }

# The newest backup's save, less one chunk, copied out as the runtime's user
# into a folder of the install folder's owner, readable by the image's user.
# shellcheck disable=SC2016 # expanded by the world container's shell
read -r backup chunk < <(timeout 60 docker exec "$ACCEPT_WORLD" sh -c \
  'b=$(ls -1 /backups | grep -E "^[0-9]{8}T[0-9]{6}Z$" | sort | tail -n 1) && c=$(ls -1 "/backups/$b" | grep "\.chunk$" | head -n 1) && echo "$b $c"') ||
  true
if [[ -z ${chunk:-} ]]; then
  echo "accept: $ACCEPT_WORLD_NAME's newest backup '${backup:-}' holds no chunk to leave out" >&2
  exit 1
fi
mkdir -p "$ACCEPT_INSTALL/$folder/$ACCEPT_WORLD_NAME"
timeout 60 docker exec "$ACCEPT_WORLD" tar -C "/backups/$backup" --exclude backup.json --exclude "$chunk" -cf - . |
  tar -C "$ACCEPT_INSTALL/$folder/$ACCEPT_WORLD_NAME" -xf - ||
  { echo "accept: cannot copy $backup out of $ACCEPT_WORLD" >&2; exit 1; }
chmod -R u+rwX,go+rX,go-w "$ACCEPT_INSTALL/$folder"

if said=$(timeout 300 "${cli[@]}" run --rm vhd import "$ACCEPT_WORLD_NAME" "$folder" 2>&1); then
  echo "accept: vhd import took $backup's save without $chunk: $said" >&2
  exit 1
fi
if [[ $said != *"vhd import: save "*" lacks $chunk, which its .chunks file names"* ]]; then
  echo "accept: vhd import refused $backup's save without $chunk, but not for the missing chunk: $said" >&2
  exit 1
fi
rm -rf "${ACCEPT_INSTALL:?}/$folder"

after=$(held) || { echo "accept: cannot read $ACCEPT_WORLD_NAME's save and backups" >&2; exit 1; }
if [[ $after != "$before" ]]; then
  echo "accept: the refused import changed $ACCEPT_WORLD_NAME's save or backups:" >&2
  diff <(echo "$before") <(echo "$after") >&2 || true
  exit 1
fi
# The control plane may drop accept-97's restore meanwhile, once its runtime reports it ended.
kept_after=$(kept) || { echo "accept: cannot read the control plane's status" >&2; exit 1; }
if [[ $kept_after != none && $kept_after != "$kept_before" ]]; then
  echo "accept: the refused import asked for restore $kept_after of $ACCEPT_WORLD_NAME" >&2
  exit 1
fi
echo "accept: vhd import refused a save without $chunk, saying so, and left the world's save and backups as they were"
