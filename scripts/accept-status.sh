#!/usr/bin/env bash
# Acceptance helper, not a check: print the control plane's GET /status of an
# install, as one JSON document held to contracts/control.status.schema.json,
# read through vhd's own client. vhd status shows less than the document holds,
# such as a world's object count and the restores.
set -euo pipefail

[[ $# -eq 2 ]] || { echo "usage: $0 PROJECT INSTALL" >&2; exit 2; }
# -P, so nothing in the working directory is imported.
timeout 60 docker compose -p "$1" -f "$2/compose.yaml" run --rm --entrypoint python3 vhd -P -c '
import json
from vhd_control import client, layout
from vhd_control.schemas import Schemas
print(json.dumps(client.status(layout.CONTROL_SOCKET, Schemas(layout.SCHEMAS))))'
