#!/usr/bin/env bash
# Acceptance check: the backup the clean stop took holds the save's files as
# its backup.json lists them. The backup.json names the world and its newest
# complete save, the one whose _main.<n>.ok the game wrote last, by number and
# by that .ok's modification time; the backup's folder holds the backup.json
# and exactly the files it lists, each of its size and SHA-256; and each is the
# save folder's file of that name. Runs after accept-95, which stopped the
# world, on the stopped container's own mounts, as the runtime's user. See
# accept.sh for the ACCEPT_ variables.
set -euo pipefail
image=$(docker inspect --format '{{.Config.Image}}' "$ACCEPT_WORLD")
# -I and the working directory /, so nothing in the world's data volume is imported.
docker run --rm -i --label "com.docker.compose.project=$ACCEPT_PROJECT" --network none \
  --cap-drop ALL --security-opt no-new-privileges:true --volumes-from "$ACCEPT_WORLD" -w / \
  --entrypoint python3 "$image" -I - "$ACCEPT_WORLD_NAME" <<'PY'
import hashlib, json, re, sys
from pathlib import Path

world = sys.argv[1]
backups = Path("/backups")
saves = Path("/data/saves/worlds_local") / world


def fail(why):
    print(f"accept: {why}", file=sys.stderr)
    sys.exit(1)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check():
    names = sorted(p.name for p in backups.iterdir() if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", p.name))
    if not names:
        fail(f"{backups} holds no backup")
    backup = backups / names[-1]
    record = json.loads((backup / "backup.json").read_text())

    # Save numbers wrap and repeat, so the newest is the .ok the game wrote last, as the runtime
    # picks it.
    written = [
        (p.stat().st_mtime_ns, int(m[1])) for p in saves.iterdir() if (m := re.fullmatch(r"_main\.([0-9]+)\.ok", p.name))
    ]
    if not written:
        fail(f"the world's save folder holds no complete save: {sorted(p.name for p in saves.iterdir())}")
    written_ns, number = max(written)
    named = (record.get("world"), record.get("save"), record.get("written_ns"))
    if named != (world, number, written_ns):
        fail(f"{backup.name}'s backup.json names world, save and written_ns {named}, not {(world, number, written_ns)}")

    files = record.get("files") or []
    listed = {entry["name"] for entry in files}
    held = {p.name for p in backup.iterdir()} - {"backup.json"}
    if not files or len(listed) != len(files) or held != listed:
        fail(f"{backup.name} holds {sorted(held)} where its backup.json lists {sorted(e['name'] for e in files)}")
    for entry in files:
        copy, original = backup / entry["name"], saves / entry["name"]
        if copy.stat().st_size != entry["size"] or sha256(copy) != entry["sha256"]:
            fail(f"{backup.name}/{entry['name']} is not the size and SHA-256 its backup.json gives")
        if not original.is_file() or sha256(original) != entry["sha256"]:
            fail(f"{backup.name}/{entry['name']} differs from the save folder's file of that name")
    print(f"accept: {backup.name} holds save {number}'s {len(files)} file(s), each as its backup.json lists it")


try:
    check()
except (OSError, ValueError, KeyError, TypeError) as error:
    fail(f"cannot check the newest backup against the save: {type(error).__name__}: {error}")
PY
