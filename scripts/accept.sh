#!/usr/bin/env bash
# Acceptance: install from a rendered compose file the way a server manager
# does, with vhd from the control plane's image, and check that the install
# runs the expected commit and that its world passes world truth: the runtime
# downloads the newest dedicated server and the world loads with the plugin.
# Pulls from the registry by default, so an image that was never pushed, or is
# not public, cannot pass. VHD_PULL=never accepts a local build. The install
# uses ports away from the defaults, so it never meets a real install on the
# same host.
set -euo pipefail

[[ $# -eq 2 ]] || { echo "usage: $0 COMPOSE_FILE COMMIT" >&2; exit 2; }
compose_file=$1 expected=$2
root=$(cd "$(dirname "$0")/.." && pwd)
pull=${VHD_PULL:-always}
project=vhd-accept
world_port=32456 web_map_port=38456
install=$(mktemp -d)
cli=(docker compose -p "$project" -f "$install/compose.yaml")

# Whatever a run left, a failed first apply included: everything the project labels.
label="label=com.docker.compose.project=$project"
trap '"${cli[@]}" --profile tools down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker ps -aq --filter "$label" | xargs -r docker rm --force >/dev/null 2>&1 || true
  docker volume ls -q --filter "$label" | xargs -r docker volume rm --force >/dev/null 2>&1 || true
  docker network ls -q --filter "$label" | xargs -r docker network rm >/dev/null 2>&1 || true
  rm -rf "$install"' EXIT

cp "$compose_file" "$install/compose.yaml"
printf 'schema = 1\n\n[world.Meadowfold]\nport = %d\n\n[web_map]\nport = %d\n' \
  "$world_port" "$web_map_port" > "$install/manifest.toml"
chmod 755 "$install"
chmod 644 "$install"/*

# Compose reports its own progress on stderr; vhd's answer is stdout.
expected_validate=$(printf '%s\n' "world Meadowfold: udp $world_port-$((world_port + 2)), password unset" \
  "web map: tcp $web_map_port")
if ! got=$("${cli[@]}" run --rm --pull "$pull" vhd validate) || [[ $got != "$expected_validate" ]]; then
  echo "accept: vhd validate did not reconcile the manifest:" >&2
  echo "$got" >&2
  exit 1
fi
echo "accept: vhd validate reconciled the manifest"

"${cli[@]}" run --rm vhd apply
echo "accept: vhd apply started the install"

for image in $("$root/scripts/install-images.sh" "$install/compose.yaml"); do
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
# World truth: running means the plugin reported the loaded world, so the dedicated
# server, BepInEx, doorstop and the plugin all loaded. Held fails at once.
status='' state='' seen=''
deadline=$((SECONDS + 20 * 60))
while ((SECONDS < deadline)); do
  status=$("${cli[@]}" run --rm vhd status 2>/dev/null || true)
  state=$(sed -n 's/^Meadowfold: \([a-z]*\).*/\1/p' <<< "$status")
  if [[ -n $state && $state != "$seen" ]]; then
    echo "accept: vhd status shows the world $state after ${SECONDS}s"
    seen=$state
  fi
  [[ $state == running || $state == held ]] && break
  sleep 5
done
if [[ $state != running ]]; then
  echo "accept: the world never passed world truth:" >&2
  echo "$status" >&2
  docker logs --tail 40 "$world" >&2 || true
  exit 1
fi
