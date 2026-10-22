#!/usr/bin/env bash
# Fail unless the images CI builds (docker-bake.hcl's default group) are exactly
# the images a release ships (install/compose.yaml's x-vhd block). Promotion
# tags only what the install lists, so an image in one list and not the other
# leaves every candidate unpromotable, or ships an image no install runs.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

rendered=$(mktemp --suffix=.yaml)
trap 'rm -f "$rendered"' EXIT
scripts/render-install.sh check "$rendered"

built=$(TAGS=check docker buildx bake --print 2>/dev/null | jq -r '.target[].tags[]' | sort)
listed=$(docker compose -f "$rendered" config --format json | jq -r '."x-vhd".images // {} | .[]' | sort)
if [[ -z $built || $built != "$listed" ]]; then
  echo "check-images: CI builds and the install lists different images:" >&2
  diff <(echo "$built") <(echo "$listed") | sed -n 's/^</  built, not listed:/p; s/^>/  listed, not built:/p' >&2 || true
  exit 1
fi
echo "check-images: CI builds exactly the images the install lists"
