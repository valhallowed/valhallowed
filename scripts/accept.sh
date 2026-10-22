#!/usr/bin/env bash
# Acceptance: install from a rendered compose file and check that it runs the
# expected commit. Pulls from the registry by default, so an image that was never
# pushed, or is not public, cannot pass. VHD_PULL=never accepts a local build.
set -euo pipefail

[[ $# -eq 2 ]] || { echo "usage: $0 COMPOSE_FILE COMMIT" >&2; exit 2; }
compose_file=$1 expected=$2
root=$(cd "$(dirname "$0")/.." && pwd)
compose=(docker compose -p vhd-accept -f "$compose_file")
pull=${VHD_PULL:-always}
runtime=vhd-accept-game-runtime
generation=$(mktemp -d)

trap '"${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker rm --force "$runtime" >/dev/null 2>&1 || true
  rm -rf "$generation"' EXIT

"${compose[@]}" up --detach --pull "$pull" --wait --wait-timeout 60

got=$(curl -fsS --max-time 5 http://127.0.0.1:8456/build)
if [[ $got != "$expected" ]]; then
  echo "accept: expected build $expected, got $got" >&2
  exit 1
fi
echo "accept: $compose_file installed and running build $got"

# The game runtime, started on its own with a generation and its secrets from
# the B1 examples: it runs as its own user, validates them and reports that it is
# installing. The game download that follows is not checked here.
image=$("${compose[@]}" config --format json | jq -r '."x-vhd".images["game-runtime"]')
cp "$root/contracts/examples/valid/game-runtime.generation-minimal.json" "$generation/generation.json"
cp "$root/contracts/examples/valid/game-runtime.secrets-no-password.json" "$generation/secrets.json"
chmod 755 "$generation"
chmod 644 "$generation"/*.json
docker run --detach --name "$runtime" --pull "$pull" -v "$generation:/generation:ro" "$image" >/dev/null

user=$(docker inspect --format '{{.Config.User}}' "$runtime")
revision=$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$runtime")
if [[ $user != vhd || $revision != "$expected" ]]; then
  echo "accept: $image runs as '$user' from build '$revision'; expected vhd from $expected" >&2
  exit 1
fi
for _ in $(seq 60); do
  if docker logs "$runtime" 2>&1 | grep -qF '"generation": 1, "state": "installing"'; then
    echo "accept: $image validated generation 1 and is installing"
    exit 0
  fi
  sleep 1
done
echo "accept: $image did not report installing generation 1 within 60 s:" >&2
docker logs "$runtime" 2>&1 | tail -n 20 >&2
exit 1
