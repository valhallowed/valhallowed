#!/usr/bin/env bash
# Fail unless the images CI builds (docker-bake.hcl's default group) are exactly
# the images a release ships (install/compose.yaml's x-vhd block). Promotion
# tags only what the install lists, so an image in one list and not the other
# leaves every candidate unpromotable, or ships an image no install runs. Also
# fail unless Dependabot's docker updates watch every images/ directory with a
# Dockerfile, and unless each image's NOTICE names the versions its Dockerfile
# pins as ARGs.
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
listed=$(scripts/install-images.sh "$rendered" | sort)
if [[ -z $built || $built != "$listed" ]]; then
  echo "check-images: CI builds and the install lists different images:" >&2
  diff <(echo "$built") <(echo "$listed") | sed -n 's/^</  built, not listed:/p; s/^>/  listed, not built:/p' >&2 || true
  exit 1
fi

images=$(find images -mindepth 2 -maxdepth 2 -name Dockerfile -printf '/%h\n' | sort)
# The docker entries' directory or directories, whichever key each uses.
watched=$(awk '/^  - package-ecosystem:/ { docker = ($3 == "docker") }
  docker && /^ +(directory: |- )\/images\// { print $NF }' .github/dependabot.yml | sort)
if [[ $images != "$watched" ]]; then
  echo "check-images: Dependabot watches different images/ directories than have a Dockerfile:" >&2
  diff <(echo "$images") <(echo "$watched") | sed -n 's/^</  not watched:/p; s/^>/  no Dockerfile:/p' >&2 || true
  exit 1
fi
for dockerfile in images/*/Dockerfile; do
  notice=${dockerfile%Dockerfile}NOTICE
  [[ -f $notice ]] || continue
  while IFS= read -r version; do
    if ! grep -qF " $version" "$notice"; then
      echo "check-images: $notice does not name $version, which $dockerfile pins" >&2
      exit 1
    fi
  done < <(sed -n 's/^ARG [A-Z_]*_VERSION=//p' "$dockerfile")
done
echo "check-images: CI builds exactly the images the install lists, Dependabot watches every Dockerfile, and every NOTICE names its pinned versions"
