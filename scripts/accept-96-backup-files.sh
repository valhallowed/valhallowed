#!/usr/bin/env bash
# Acceptance check: the backup the clean stop took holds the save's files as
# its backup.json record lists them. The record names the world and the newest
# complete save in the world's save folder, the one whose _main.<n>.ok the game
# wrote last; the backup's folder holds the record and exactly the files it
# lists, each of its size and SHA-256; and each is the save folder's file of
# that name. Runs after accept-95, which stopped the world, on the stopped
# container's own mounts, as the runtime's user. See accept.sh for the ACCEPT_
# variables.
set -euo pipefail
image=$(docker inspect --format '{{.Config.Image}}' "$ACCEPT_WORLD")
docker run --rm -i --label "com.docker.compose.project=$ACCEPT_PROJECT" --network none \
  --cap-drop ALL --volumes-from "$ACCEPT_WORLD" --entrypoint python3 "$image" - "$ACCEPT_WORLD_NAME" <<'PY'
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


names = sorted(p.name for p in backups.iterdir() if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", p.name))
if not names:
    fail(f"{backups} holds no backup")
backup = backups / names[-1]
record = json.loads((backup / "backup.json").read_text())

oks = [p for p in saves.iterdir() if re.fullmatch(r"_main\.[0-9]+\.ok", p.name)]
if not oks:
    fail(f"the world's save folder holds no complete save: {sorted(p.name for p in saves.iterdir())}")
# Save numbers wrap, so the newest is the .ok the game wrote last.
newest = int(max(oks, key=lambda p: p.stat().st_mtime_ns).name.split(".")[1])
if record.get("world") != world or record.get("save") != newest:
    fail(f"{backup.name} records world {record.get('world')!r} save {record.get('save')!r}, not {world!r} save {newest}")

files = record.get("files") or []
listed = {entry["name"] for entry in files}
held = {p.name for p in backup.iterdir()} - {"backup.json"}
if not files or len(listed) != len(files) or held != listed:
    fail(f"{backup.name} holds {sorted(held)} where its record lists {sorted(e['name'] for e in files)}")
for entry in files:
    copy, original = backup / entry["name"], saves / entry["name"]
    if copy.stat().st_size != entry["size"] or sha256(copy) != entry["sha256"]:
        fail(f"{backup.name}/{entry['name']} is not the size and SHA-256 its record gives")
    if not original.is_file() or sha256(original) != entry["sha256"]:
        fail(f"{backup.name}/{entry['name']} differs from the save folder's file of that name")
print(f"accept: {backup.name} holds save {newest}'s {len(files)} file(s), each as its record lists it")
PY
