#!/usr/bin/env bash
# Acceptance: install from a rendered compose file the way a server manager
# does, with vhd from the control plane's image, and check that the install
# runs the expected commit. Pulls from the registry by default, so an image that
# was never pushed, or is not public, cannot pass. VHD_PULL=never accepts a
# local build. The world's game download that follows is not waited for.
set -euo pipefail

[[ $# -eq 2 ]] || { echo "usage: $0 COMPOSE_FILE COMMIT" >&2; exit 2; }
compose_file=$1 expected=$2
root=$(cd "$(dirname "$0")/.." && pwd)
pull=${VHD_PULL:-always}
project=vhd-accept
install=$(mktemp -d)
cli=(docker compose -p "$project" -f "$install/compose.yaml")

trap 'if [[ -f $install/.vhd/compose.json ]]; then
    docker compose -p "$project" -f "$install/.vhd/compose.json" down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
  "${cli[@]}" --profile tools down --volumes >/dev/null 2>&1 || true
  rm -rf "$install"' EXIT

cp "$compose_file" "$install/compose.yaml"
cp "$root/schema/examples/valid/manifest-one-world.toml" "$install/manifest.toml"
chmod 755 "$install"
chmod 644 "$install"/*
images=$("${cli[@]}" config --format json | jq -r '."x-vhd".images')
web_map_port=$(jq -r '.properties.web_map.properties.port.default' "$root/schema/manifest.schema.json")

# Compose reports its own progress on stderr; vhd's answer is stdout.
expected_validate=$(printf '%s\n' 'world Meadowfold: udp 2456-2458, password unset' "web map: tcp $web_map_port")
if ! got=$("${cli[@]}" run --rm --pull "$pull" vhd validate) || [[ $got != "$expected_validate" ]]; then
  echo "accept: vhd validate did not reconcile the example manifest:" >&2
  echo "$got" >&2
  exit 1
fi
echo "accept: vhd validate reconciled the example manifest"

"${cli[@]}" run --rm vhd apply
echo "accept: vhd apply started the install"

for name in $(jq -r 'keys[]' <<< "$images"); do
  image=$(jq -r --arg name "$name" '.[$name]' <<< "$images")
  revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")
  if [[ $revision != "$expected" ]]; then
    echo "accept: $image is from build '$revision', not $expected" >&2
    exit 1
  fi
done
got=$(curl -fsS --max-time 5 "http://127.0.0.1:$web_map_port/build")
if [[ $got != "$expected" ]]; then
  echo "accept: the skeleton serves build '$got', not $expected" >&2
  exit 1
fi
echo "accept: every image is from build $expected"

# The world: its own user, no capabilities, and its generation delivered, its
# status reaching the control plane from its own container.
world=$(docker ps --filter "label=com.docker.compose.project=$project" --format '{{.Names}}' | grep -- '-world-' || true)
if [[ $(wc -l <<< "$world") -ne 1 || -z $world ]]; then
  echo "accept: expected one world container, found: ${world:-none}" >&2
  exit 1
fi
read -r user caps < <(docker inspect --format '{{.Config.User}} {{json .HostConfig.CapDrop}}' "$world")
if [[ $user != vhd || $caps != '["ALL"]' ]]; then
  echo "accept: $world runs as '$user' dropping $caps; expected vhd dropping [\"ALL\"]" >&2
  exit 1
fi
status=
for _ in $(seq 30); do
  status=$("${cli[@]}" run --rm vhd status 2>/dev/null || true)
  if grep -qx 'Meadowfold: installing' <<< "$status"; then
    echo "accept: vhd status shows the world installing"
    exit 0
  fi
  sleep 3
done
echo "accept: vhd status never showed the world installing:" >&2
echo "$status" >&2
docker logs --tail 20 "$world" >&2 || true
exit 1
