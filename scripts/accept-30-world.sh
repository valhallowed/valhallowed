#!/usr/bin/env bash
# Acceptance check: the world runs as VHD's unprivileged user with every
# capability dropped. See accept.sh for the ACCEPT_ variables.
set -euo pipefail

read -r user caps < <(docker inspect --format '{{.Config.User}} {{json .HostConfig.CapDrop}}' "$ACCEPT_WORLD")
if [[ $user != vhd || $caps != '["ALL"]' ]]; then
  echo "accept: $ACCEPT_WORLD runs as '$user' dropping $caps; expected vhd dropping [\"ALL\"]" >&2
  exit 1
fi
