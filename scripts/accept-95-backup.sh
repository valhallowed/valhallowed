#!/usr/bin/env bash
# Acceptance check: a clean stop of the world saves it, and its runtime backs
# that save up before the container exits. The world's container stops within
# its stop grace with exit code 0, and its newest backup is a new one, of a
# save the game completed after the stop began. vhd status gives the world as
# many backups as its folder holds and the newest one's age, from its name.
# The world stays stopped, so a check after this one finds no game running.
# See accept.sh for the ACCEPT_ variables.
set -euo pipefail
cli=(docker compose -p "$ACCEPT_PROJECT" -f "$ACCEPT_INSTALL/compose.yaml")
image=$(docker inspect --format '{{.Config.Image}}' "$ACCEPT_WORLD") ||
  { echo "accept: cannot read $ACCEPT_WORLD's image" >&2; exit 1; }
# Reads the stopped container's own mounts, as the runtime's user, locked down as it is.
helper=(docker run --rm --label "com.docker.compose.project=$ACCEPT_PROJECT" --network none
  --cap-drop ALL --security-opt no-new-privileges:true --volumes-from "$ACCEPT_WORLD" --entrypoint '' "$image")

# The world's backups, by the runtime's own names for them, oldest first.
backups() {
  local listed
  listed=$("$@" ls -1 /backups) || { echo "accept: cannot list $ACCEPT_WORLD's backups" >&2; exit 1; }
  grep -E '^[0-9]{8}T[0-9]{6}Z$' <<< "$listed" | sort || true
}
before=$(backups docker exec "$ACCEPT_WORLD")

# docker stop waits the container's stop grace, which Compose sets from the
# rendered stop_grace_period, before it kills the runtime.
stopped_ns=$(date +%s%N)
docker stop "$ACCEPT_WORLD" >/dev/null || { echo "accept: docker stop $ACCEPT_WORLD failed" >&2; exit 1; }
state=$(docker inspect --format '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}' "$ACCEPT_WORLD") ||
  { echo "accept: cannot inspect $ACCEPT_WORLD after its stop" >&2; exit 1; }
if [[ $state != 'exited 0 false' ]]; then
  echo "accept: $ACCEPT_WORLD did not stop cleanly: status, exit code and out of memory are '$state'" >&2
  docker logs --tail 40 "$ACCEPT_WORLD" >&2 || true
  exit 1
fi

after=$(backups "${helper[@]}")
newest=$(tail -n 1 <<< "$after")
if [[ -z $newest ]] || grep -qxF "$newest" <<< "$before"; then
  echo "accept: the clean stop left backups '$(paste -sd ' ' <<< "$after")' where '$(paste -sd ' ' <<< "$before")' were; expected a new one, the newest" >&2
  docker logs --tail 40 "$ACCEPT_WORLD" >&2 || true
  exit 1
fi
# A game killed at the runtime's stop timeout saves nothing, and the stop then
# backs up an older save: the stop's own save completed after it began.
# Python, not jq, which may read a nanosecond count as a float.
written_ns=$("${helper[@]}" python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["written_ns"])' \
  "/backups/$newest/backup.json") || { echo "accept: cannot read the written_ns of $newest's backup.json" >&2; exit 1; }
if [[ ! $written_ns =~ ^[0-9]+$ ]] || ((written_ns < stopped_ns)); then
  echo "accept: $newest's backup.json gives written_ns '$written_ns', not after the stop began at $stopped_ns: the stop saved nothing" >&2
  docker logs --tail 40 "$ACCEPT_WORLD" >&2 || true
  exit 1
fi
count=$(wc -l <<< "$after")
echo "accept: the clean stop saved the world, and its runtime backed it up as $newest"

# The runtime's last report before it exited names the backups. vhd status
# keeps showing it once the world goes quiet.
taken=$(date -u -d "${newest:0:4}-${newest:4:2}-${newest:6:2}T${newest:9:2}:${newest:11:2}:${newest:13:2}Z" +%s)
declare -A unit_s=([s]=1 [m]=60 [h]=3600 [d]=86400)
status='' line=''
for _ in $(seq 6); do
  from=$(date -u +%s)
  status=$(timeout 60 "${cli[@]}" run --rm vhd status 2>&1) || true
  to=$(date -u +%s)
  line=$(grep "^$ACCEPT_WORLD_NAME: " <<< "$status" || true)
  if [[ $line =~ ,\ ([0-9]+)\ backup\(s\),\ the\ newest\ ([0-9]+)([smhd])\ old$ && ${BASH_REMATCH[1]} == "$count" ]]; then
    # The age is whole units, rounded down, at a moment between from and to.
    size=${unit_s[${BASH_REMATCH[3]}]}
    age=$((BASH_REMATCH[2] * size))
    if ((age <= to - taken && age + size > from - taken)); then
      echo "accept: vhd status gives the world $count backup(s), the newest ${BASH_REMATCH[2]}${BASH_REMATCH[3]} old"
      exit 0
    fi
    echo "accept: vhd status gives the newest backup an age of ${BASH_REMATCH[2]}${BASH_REMATCH[3]}; $newest was $((from - taken))..$((to - taken)) s old" >&2
    exit 1
  fi
  sleep 5
done
echo "accept: vhd status does not give $ACCEPT_WORLD_NAME $count backup(s) and the newest one's age: '$line'" >&2
echo "$status" >&2
exit 1
