#!/usr/bin/env bash
# Acceptance helper, not a check: wait until a world's runtime reports how
# restore ID ended, and, for one done, that the world is past installing and
# loading, then print the world's entry of the control plane's status (see
# accept-status.sh). Fails, saying why, when WAIT_S passes first, or when the
# world's container stops or restarts while it waits.
set -euo pipefail

[[ $# -eq 5 ]] || { echo "usage: $0 PROJECT INSTALL WORLD ID WAIT_S" >&2; exit 2; }
project=$1 install=$2 world=$3 id=$4 wait_s=$5
root=$(cd "$(dirname "$0")/.." && pwd)

entry='' seen='' ended='' container='' started=$SECONDS
while ((SECONDS - started < wait_s)); do
  # An acceptance install runs one world, whose container's name holds -world-.
  container=$(docker ps -aq --filter "label=com.docker.compose.project=$project" --filter name=-world- | head -n 1)
  if [[ -n $container ]]; then
    read -r run restarts < <(docker inspect --format '{{.State.Status}} {{.RestartCount}}' "$container" 2>/dev/null || echo gone 0)
    if [[ $run != running || $restarts != 0 ]]; then
      echo "accept: $world's container is $run after $restarts restart(s), waiting on restore $id" >&2
      docker logs --tail 40 "$container" >&2 || true
      exit 1
    fi
  fi
  if status=$("$root/scripts/accept-status.sh" "$project" "$install" 2>/dev/null) &&
    entry=$(jq -ce --arg world "$world" '.worlds[$world]' <<< "$status" 2>/dev/null); then
    read -r state outcome < <(jq -r --arg id "$id" \
      '[.state, (if .report.restore.id == $id then .report.restore.outcome else "unacted" end)] | @tsv' <<< "$entry")
    if [[ "$state, restore $outcome" != "$seen" ]]; then
      seen="$state, restore $outcome"
      echo "accept: $world is $seen, $((SECONDS - started))s into the wait for restore $id" >&2
    fi
    case $outcome:$state in
      done:installing | done:loading | done:unknown) ;;
      done:* | failed:* | rolled-back:*) ended=1; break ;;
    esac
  fi
  sleep 5
done
if [[ -z $ended ]]; then
  echo "accept: $world did not end restore $id within ${wait_s}s; it is ${seen:-not in vhd status}" >&2
  [[ -z $container ]] || docker logs --tail 40 "$container" >&2 || true
  exit 1
fi
echo "$entry"
