#!/usr/bin/env bash
# Acceptance: install from a rendered compose file the way a server manager
# does, with vhd from the control plane's image, and check that the install
# runs the expected commit and that the plugin reports its loaded world: the
# runtime downloads the newest dedicated server, and the server, BepInEx,
# UnityDoorstop and the plugin all load. Then a synthetic telemetry batch,
# sent from the world's container, must land in the database through intake,
# and so must the session the plugin started. On a clean host it pulls from the
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
  echo "accept: the web map serves build '$got', not $expected" >&2
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

# The data layer's services run as VHD's unprivileged user with every
# capability dropped, on the install network. Only the web map publishes a
# port, the manifest's.
declare -A services
for service in database intake web-map; do
  services[$service]=$(docker ps -q --filter "$label" --filter "label=com.docker.compose.service=$service")
  if [[ -z ${services[$service]} ]]; then
    echo "accept: no $service container is running" >&2
    exit 1
  fi
  expected_ports='{}'
  [[ $service != web-map ]] || expected_ports="{\"8456/tcp\":[{\"HostIp\":\"\",\"HostPort\":\"$web_map_port\"}]}"
  read -r user caps ports < <(docker inspect \
    --format '{{.Config.User}} {{json .HostConfig.CapDrop}} {{json .HostConfig.PortBindings}}' "${services[$service]}")
  if [[ $user != vhd || $caps != '["ALL"]' || $ports != "$expected_ports" ]]; then
    echo "accept: $service runs as '$user' dropping $caps publishing $ports; expected vhd dropping [\"ALL\"] publishing $expected_ports" >&2
    exit 1
  fi
done
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
  exit 1
fi

# The data layer, once the world runs: its services running, then a batch
# from the world's own container, which intake attributes to the world by its
# address, found in the database.
for service in database intake web-map; do
  if ! grep -q "^$service service: running" <<< "$status"; then
    echo "accept: vhd status does not show the $service service running:" >&2
    echo "$status" >&2
    docker logs --tail 40 "${services[$service]}" >&2 || true
    exit 1
  fi
done
session=$(cat /proc/sys/kernel/random/uuid)
answer=$(docker exec -i "$world" python3 - "$session" <<'PY'
import json, sys, urllib.error, urllib.request
at = "2026-01-01T00:00:00Z"
player = {"id": "Steam_accept"}
events = [
    {"kind": "player.join", "at": at, "player": {**player, "name": "Accept"}},
    {"kind": "player.position", "at": at, "player": player, "x": 1.5, "y": 30.0, "z": -2.5},
]
body = json.dumps({"contract": 1, "session": sys.argv[1], "sent_at": at, "events": events}).encode()
request = urllib.request.Request("http://intake:9458/events", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        print(response.status, response.read().decode())
except urllib.error.HTTPError as error:
    print(error.code, error.read().decode())
PY
)
if [[ $answer != '202 {"accepted": 2, "dropped": 0}' ]]; then
  echo "accept: intake answered the world's batch with: $answer" >&2
  docker logs --tail 40 "${services[intake]}" >&2 || true
  exit 1
fi
stored=$(docker exec "${services[database]}" psql -h /run/postgresql -d vhd -tAc \
  "SELECT w.name, p.x, p.z FROM position p JOIN session s ON s.id = p.session_id
   JOIN world w ON w.id = s.world_id WHERE p.session_id = '$session'")
if [[ $stored != "$world_name|1.5|-2.5" ]]; then
  echo "accept: the database holds '$stored' for the world's batch, not $world_name at 1.5, -2.5" >&2
  exit 1
fi
echo "accept: a batch from the world's container reached the database through intake"

# The plugin's own session: it starts one when the world loads and sends
# session.start until intake accepts it, so it reaches the database within a
# few of the sender's 5 s rounds. Only the plugin sends session.start.
started=''
for _ in $(seq 12); do
  started=$(docker exec "${services[database]}" psql -h /run/postgresql -d vhd -tAc \
    "SELECT s.id FROM session s JOIN world w ON w.id = s.world_id
     WHERE w.name = '$world_name' AND s.started_at IS NOT NULL ORDER BY s.received_at DESC LIMIT 1")
  [[ -n $started ]] && break
  sleep 5
done
if [[ -z $started ]]; then
  echo "accept: the database holds no session the plugin started for $world_name" >&2
  docker logs "$world" 2>&1 | grep -F ':Valhallowed]' | tail -20 >&2 || true
  exit 1
fi
echo "accept: the plugin's session $started reached the database through intake"

