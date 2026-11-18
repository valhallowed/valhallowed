#!/usr/bin/env bash
# Acceptance check: the world runs as VHD's unprivileged user with every
# capability dropped, and owns and can write its backup folder. See accept.sh
# for the ACCEPT_ variables.
set -euo pipefail

read -r user caps < <(docker inspect --format '{{.Config.User}} {{json .HostConfig.CapDrop}}' "$ACCEPT_WORLD")
if [[ $user != vhd || $caps != '["ALL"]' ]]; then
  echo "accept: $ACCEPT_WORLD runs as '$user' dropping $caps; expected vhd dropping [\"ALL\"]" >&2
  exit 1
fi
if ! docker exec "$ACCEPT_WORLD" sh -c 'test -w /backups && test "$(stat -c %U /backups)" = vhd'; then
  echo "accept: $ACCEPT_WORLD cannot write /backups as its own folder:" >&2
  docker exec "$ACCEPT_WORLD" ls -ld /backups >&2 || true
  exit 1
fi
