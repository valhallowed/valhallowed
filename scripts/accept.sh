#!/usr/bin/env bash
# Acceptance: install from a rendered compose file the way a server manager
# does, with vhd from the control plane's image, and wait until the plugin
# reports its loaded world: the runtime downloads the newest dedicated server,
# and the server, BepInEx, UnityDoorstop and the plugin all load. Then run
# every check, scripts/accept-NN-*.sh in name order; the first to fail stops
# the run. A new check is a new file there. On a clean host the install pulls
# from the registry by default, so an image that was never pushed, or is not
# public, cannot pass. VHD_PULL=never accepts a local build. The install uses
# ports away from the defaults, so it never meets a real install on the same
# host.
#
# A check is a standalone executable. It prints "accept: <why>" and
# diagnostics to stderr and exits non-zero on failure. It gets these exported
# variables: ACCEPT_PROJECT (compose project), ACCEPT_INSTALL (the directory
# holding compose.yaml and manifest.toml), ACCEPT_COMMIT (the commit under
# test), ACCEPT_WORLD_NAME, ACCEPT_WORLD_PORT, ACCEPT_WEB_MAP_PORT (the
# manifest's), ACCEPT_WORLD, ACCEPT_DATABASE, ACCEPT_INTAKE,
# ACCEPT_WEB_MAP (container names or ids) and ACCEPT_STATUS (the last vhd
# status output).
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

"${cli[@]}" run --rm --pull "$pull" vhd apply
applied=$SECONDS
echo "accept: vhd apply started the install"

world=$(docker ps --filter "$label" --format '{{.Names}}' | grep -- '-world-' || true)
if [[ $(wc -l <<< "$world") -ne 1 || -z $world ]]; then
  echo "accept: expected one world container, found: ${world:-none}" >&2
  exit 1
fi
control=$(docker ps -q --filter "$label" --filter label=com.docker.compose.service=control-plane)
if [[ -z $control ]]; then
  echo "accept: no control plane container is running" >&2
  exit 1
fi
declare -A services
for service in database intake web-map; do
  services[$service]=$(docker ps -q --filter "$label" --filter "label=com.docker.compose.service=$service")
  if [[ -z ${services[$service]} ]]; then
    echo "accept: no $service container is running" >&2
    exit 1
  fi
done

# Running means the plugin reported the loaded world. Held and unverified fail at
# once, and so does a world or control plane container that stops or restarts:
# with one generation neither has a reason to exit.
status='' state='' seen='' outcome=''
while ((SECONDS - applied < wait_s)); do
  for container in "$world" "$control" "${services[@]}"; do
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
  # The checks run only once the world runs, so a wrong build shows here too.
  for image in $("$root/scripts/install-images.sh" "$install/compose.yaml" || true); do
    echo "accept: $image is from build '$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image" 2>/dev/null || true)'" >&2
  done
  exit 1
fi

export ACCEPT_PROJECT=$project ACCEPT_INSTALL=$install ACCEPT_COMMIT=$expected
export ACCEPT_WORLD_NAME=$world_name ACCEPT_WORLD_PORT=$world_port ACCEPT_WEB_MAP_PORT=$web_map_port
export ACCEPT_WORLD=$world ACCEPT_STATUS=$status
export ACCEPT_DATABASE=${services[database]} ACCEPT_INTAKE=${services[intake]} ACCEPT_WEB_MAP=${services[web-map]}

checks=("$root"/scripts/accept-[0-9][0-9]-*.sh)
[[ -e ${checks[0]} ]] || { echo "accept: no checks found" >&2; exit 1; }
for check in "${checks[@]}"; do
  "$check" || { echo "accept: ${check##*/} failed" >&2; exit 1; }
done
