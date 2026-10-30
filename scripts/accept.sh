#!/usr/bin/env bash
# Acceptance: install from a rendered compose file the way a server manager
# does, with vhd from the control plane's image, and check that the install
# runs the expected commit and that the plugin reports its loaded world: the
# runtime downloads the newest dedicated server, and the server, BepInEx,
# UnityDoorstop and the plugin all load. On a clean host it pulls from the
# registry by default, so an image that was never pushed, or is not public,
# cannot pass. VHD_PULL=never accepts a local build. The install uses ports
# away from the defaults, so it never meets a real install on the same host.
set -euo pipefail

[[ $# -eq 2 ]] || { echo "usage: $0 COMPOSE_FILE COMMIT" >&2; exit 2; }
compose_file=$1 expected=$2
root=$(cd "$(dirname "$0")/.." && pwd)
pull=${VHD_PULL:-always}
project=vhd-accept
world_name=Meadowfold world_port=32456 web_map_port=38456
# The wait covers the world's download plus the runtime's load timeout (900 s),
# after which a world the plugin never reported is unverified. Every caller's
# job timeout leaves room for it.
wait_s=1200
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
printf 'schema = 1\n\n[world.%s]\nport = %d\n\n[web_map]\nport = %d\n' \
  "$world_name" "$world_port" "$web_map_port" > "$install/manifest.toml"
chmod 755 "$install"
chmod 644 "$install"/*

# Compose reports its own progress on stderr; vhd's answer is stdout.
expected_validate=$(printf '%s\n' "world $world_name: udp $world_port-$((world_port + 2)), password unset" \
  "web map: tcp $web_map_port")
if ! got=$("${cli[@]}" run --rm --pull "$pull" vhd validate) || [[ $got != "$expected_validate" ]]; then
  echo "accept: vhd validate did not reconcile the manifest:" >&2
  echo "$got" >&2
  exit 1
fi
echo "accept: vhd validate reconciled the manifest"

"${cli[@]}" run --rm vhd apply
applied=$SECONDS
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

# The world runs as VHD's unprivileged user with every capability dropped.
world=$(docker ps --filter "$label" --format '{{.Names}}' | grep -- '-world-' || true)
if [[ $(wc -l <<< "$world") -ne 1 || -z $world ]]; then
  echo "accept: expected one world container, found: ${world:-none}" >&2
  exit 1
fi
read -r user caps < <(docker inspect --format '{{.Config.User}} {{json .HostConfig.CapDrop}}' "$world")
if [[ $user != vhd || $caps != '["ALL"]' ]]; then
  echo "accept: $world runs as '$user' dropping $caps; expected vhd dropping [\"ALL\"]" >&2
  exit 1
fi

# Running means the plugin reported the loaded world. Held and unverified fail at
# once, and so does a world or control plane container that stops or restarts:
# with one generation neither has a reason to exit.
control=$(docker ps -q --filter "$label" --filter label=com.docker.compose.service=control-plane)
if [[ -z $control ]]; then
  echo "accept: no control plane container is running" >&2
  exit 1
fi
status='' state='' seen='' outcome=''
while ((SECONDS - applied < wait_s)); do
  for container in "$world" "$control"; do
    read -r run restarts < <(docker inspect --format '{{.State.Status}} {{.RestartCount}}' "$container" 2>/dev/null || echo gone 0)
    if [[ $run != running || $restarts != 0 ]]; then
      outcome="container $container is $run after $restarts restart(s)"
      break 2
    fi
  done
  status=$(timeout 60 "${cli[@]}" run --rm vhd status 2>"$install/status.err") || true
  state=$(sed -n "s/^$world_name: \([a-z]*\).*/\1/p" <<< "$status")
  if [[ -n $state && $state != "$seen" ]]; then
    echo "accept: vhd status shows the world $state $((SECONDS - applied))s after vhd apply"
    seen=$state
  fi
  case $state in
    running) break ;;
    held | unverified) outcome="it is $state"; break ;;
  esac
  sleep 5
done
if [[ $state != running ]]; then
  echo "accept: the plugin never reported the world: ${outcome:-it is ${state:-unreported} after ${wait_s}s}" >&2
  echo "$status" >&2
  cat "$install/status.err" >&2 2>/dev/null || true
  docker logs --tail 40 "$world" >&2 || true
  [[ -z $control ]] || docker logs --tail 20 "$control" >&2 || true
  exit 1
fi
