#!/usr/bin/env bash
# Acceptance check: a clean stop of the world saves it, and its runtime backs
# that save up before the container exits. The world's container stops within
# its stop grace with exit code 0, its backups folder holds exactly one backup
# more than before, the newest, and vhd status gives the world that many
# backups and the newest one's age, from its name. The world stays stopped, so
# a check after this one finds no game running. See accept.sh for the ACCEPT_
# variables.
set -euo pipefail
cli=(docker compose -p "$ACCEPT_PROJECT" -f "$ACCEPT_INSTALL/compose.yaml")
image=$(docker inspect --format '{{.Config.Image}}' "$ACCEPT_WORLD")

# The world's backups, by the runtime's own names for them, oldest first.
backups() {
  "$@" sh -c 'ls -1 /backups' | grep -E '^[0-9]{8}T[0-9]{6}Z$' | sort || true
}
before=$(backups docker exec "$ACCEPT_WORLD")

# docker stop waits the container's stop grace, which Compose sets from the
# rendered stop_grace_period, before it kills the runtime.
docker stop "$ACCEPT_WORLD" >/dev/null
read -r run code oom < <(docker inspect --format '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}' "$ACCEPT_WORLD")
if [[ $run != exited || $code != 0 || $oom != false ]]; then
  echo "accept: $ACCEPT_WORLD did not stop cleanly: $run, exit code $code, out of memory $oom" >&2
  docker logs --tail 40 "$ACCEPT_WORLD" >&2 || true
  exit 1
fi

# The stopped container's own mounts, read as the runtime's user.
after=$(backups docker run --rm --label "com.docker.compose.project=$ACCEPT_PROJECT" --network none \
  --cap-drop ALL --volumes-from "$ACCEPT_WORLD" --entrypoint '' "$image")
new=$(comm -13 <(echo "$before") <(echo "$after") | sed '/^$/d')
newest=$(tail -n 1 <<< "$after")
if [[ $(wc -l <<< "$new") -ne 1 || -z $new || $new != "$newest" ]]; then
  echo "accept: the clean stop left backups '$(paste -sd ' ' <<< "$after")' where '$(paste -sd ' ' <<< "$before")' were; expected one more, the newest" >&2
  docker logs --tail 40 "$ACCEPT_WORLD" >&2 || true
  exit 1
fi
count=$(wc -l <<< "$after")
echo "accept: the clean stop backed up the world as $newest"

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
