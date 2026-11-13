#!/usr/bin/env bash
# Acceptance check: the data layer's services run as VHD's unprivileged user
# with every capability dropped, on the install network. Only the web map
# publishes a port, the manifest's. See accept.sh for the ACCEPT_ variables.
set -euo pipefail

for entry in "database:$ACCEPT_DATABASE" "intake:$ACCEPT_INTAKE" "web-map:$ACCEPT_WEB_MAP"; do
  service=${entry%%:*} container=${entry#*:}
  expected_ports='{}'
  [[ $service != web-map ]] || expected_ports="{\"8456/tcp\":[{\"HostIp\":\"\",\"HostPort\":\"$ACCEPT_WEB_MAP_PORT\"}]}"
  read -r user caps ports < <(docker inspect \
    --format '{{.Config.User}} {{json .HostConfig.CapDrop}} {{json .HostConfig.PortBindings}}' "$container")
  if [[ $user != vhd || $caps != '["ALL"]' || $ports != "$expected_ports" ]]; then
    echo "accept: $service runs as '$user' dropping $caps publishing $ports; expected vhd dropping [\"ALL\"] publishing $expected_ports" >&2
    exit 1
  fi
done
