#!/usr/bin/env bash
# Acceptance helper, not a check: make INSTALL an install folder as a server
# manager does, holding the release's compose file and a manifest that names
# one world and the web map's port, readable by the containers that mount it.
set -euo pipefail

[[ $# -eq 5 ]] || { echo "usage: $0 COMPOSE_FILE INSTALL WORLD_NAME WORLD_PORT WEB_MAP_PORT" >&2; exit 2; }
compose_file=$1 install=$2

cp "$compose_file" "$install/compose.yaml"
printf 'schema = 1\n\n[world.%s]\nport = %d\n\n[web_map]\nport = %d\n' "$3" "$4" "$5" > "$install/manifest.toml"
chmod 755 "$install"
chmod 644 "$install/compose.yaml" "$install/manifest.toml"
