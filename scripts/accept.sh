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
install=$(mktemp -d)
cli=(docker compose -p vhd-accept-install -f "$install/compose.yaml")

trap '"${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  "${cli[@]}" --profile tools down --volumes >/dev/null 2>&1 || true
  docker rm --force "$runtime" >/dev/null 2>&1 || true
  rm -rf "$generation" "$install"' EXIT

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
installing=false
for _ in $(seq 60); do
  if docker logs "$runtime" 2>&1 | grep -qF '"generation": 1, "state": "installing"'; then
    installing=true
    break
  fi
  sleep 1
done
if [[ $installing == false ]]; then
  echo "accept: $image did not report installing generation 1 within 60 s:" >&2
  docker logs "$runtime" 2>&1 | tail -n 20 >&2
  exit 1
fi
echo "accept: $image validated generation 1 and is installing"

# vhd, from the control plane image, run the way a server manager runs it: from
# an install folder holding the release's compose file and a manifest.
cp "$compose_file" "$install/compose.yaml"
cp "$root/schema/examples/valid/manifest-one-world.toml" "$install/manifest.toml"
chmod 755 "$install"
chmod 644 "$install"/*
expected_world='world Meadowfold: udp 2456-2458, password unset'
if ! got=$("${cli[@]}" run --rm --pull "$pull" vhd validate 2>&1) || ! grep -qxF "$expected_world" <<< "$got"; then
  echo "accept: vhd validate did not reconcile the example manifest:" >&2
  echo "$got" >&2
  exit 1
fi
echo "accept: vhd validate reconciled the example manifest"
