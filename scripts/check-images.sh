#!/usr/bin/env bash
# Fail unless the images CI builds (docker-bake.hcl's default group) are exactly
# the images a release ships (install/compose.yaml's x-vhd block). Promotion
# tags only what the install lists, so an image in one list and not the other
# leaves every candidate unpromotable, or ships an image no install runs. Also
# fail unless Dependabot watches every images/ directory with a Dockerfile.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

rendered=$(mktemp --suffix=.yaml)
scripts/render-install.sh check "$rendered"

errors=$(mktemp)
trap 'rm -f "$rendered" "$errors"' EXIT
if ! printed=$(TAGS=check docker buildx bake --print 2>"$errors"); then
  echo "check-images: docker buildx bake --print failed:" >&2
  cat "$errors" >&2
  exit 1
fi
built=$(jq -r '.target[] | .tags // [] | .[]' <<< "$printed" | sort)
listed=$(docker compose -f "$rendered" config --format json | jq -r '."x-vhd".images // {} | .[]' | sort)
if [[ -z $built || $built != "$listed" ]]; then
  echo "check-images: CI builds and the install lists different images:" >&2
  diff <(echo "$built") <(echo "$listed") | sed -n 's/^</  built, not listed:/p; s/^>/  listed, not built:/p' >&2 || true
  exit 1
fi

images=$(find images -mindepth 2 -maxdepth 2 -name Dockerfile -printf '/%h\n' | sort)
watched=$(grep -oE '^ +- /images/[a-z0-9-]+$' .github/dependabot.yml | sed 's/^ *- //' | sort)
if [[ $images != "$watched" ]]; then
  echo "check-images: Dependabot watches different images/ directories than have a Dockerfile:" >&2
  diff <(echo "$images") <(echo "$watched") | sed -n 's/^</  not watched:/p; s/^>/  no Dockerfile:/p' >&2 || true
  exit 1
fi
echo "check-images: CI builds exactly the images the install lists, and Dependabot watches every Dockerfile"
