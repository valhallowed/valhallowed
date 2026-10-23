#!/usr/bin/env bash
# Gather what a candidate or release attaches into OUT: COMPOSE_FILE, the
# install that was accepted and promoted, as compose.yaml; both schemas; the
# plugin, taken out of that install's game runtime image, so the plugin shipped
# is the one acceptance ran and nothing is rebuilt (G12); and SHA256SUMS over
# them. Prints the dedicated server build the plugin was built against.
set -euo pipefail

[[ $# -eq 2 ]] || { echo "usage: $0 COMPOSE_FILE OUT" >&2; exit 2; }
compose_file=$(realpath -e "$1") out=$2
root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$out"
out=$(realpath "$out")
if [[ $compose_file != "$out/compose.yaml" ]]; then
  cp "$compose_file" "$out/compose.yaml"
fi
cp "$root/schema/manifest.schema.json" "$root/schema/secrets.schema.json" "$out/"

image=$(docker compose -f "$out/compose.yaml" config --format json | jq -r '."x-vhd".images["game-runtime"] // empty')
if [[ ! $image =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "release-assets: $compose_file pins no game runtime image by digest" >&2
  exit 1
fi
docker pull --quiet "$image" >/dev/null
build=$(docker image inspect --format '{{index .Config.Labels "com.valhallowed.game-build"}}' "$image")
if [[ ! $build =~ ^[0-9]+$ ]]; then
  echo "release-assets: $image names no game build" >&2
  exit 1
fi
container=$(docker create "$image")
trap 'docker rm --force "$container" >/dev/null' EXIT
docker cp --quiet "$container:/opt/vhd/plugin/Valhallowed.Plugin.dll" "$out/"

(cd "$out" && sha256sum compose.yaml manifest.schema.json secrets.schema.json Valhallowed.Plugin.dll > SHA256SUMS)
echo "$build"
