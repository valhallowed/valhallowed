#!/usr/bin/env bash
# Run the data layer's tests inside the data-layer image's test stage, against
# the PostgreSQL and psycopg the image ships, so Docker is the only dependency.
# scripts/check-python.sh data runs the same tests where PostgreSQL is absent,
# and skips those that need it.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

docker buildx bake data-layer-test
docker run --rm --network none vhd-data-layer-test:local
echo "check-data-layer: the data layer passes against its own PostgreSQL"
