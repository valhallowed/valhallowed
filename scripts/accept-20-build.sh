#!/usr/bin/env bash
# Acceptance check: the install runs the commit under test. Every image the
# compose file names carries that commit as its revision, and the web map
# serves it. See accept.sh for the ACCEPT_ variables.
set -euo pipefail

for image in $("$(dirname "$0")/install-images.sh" "$ACCEPT_INSTALL/compose.yaml"); do
  revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")
  if [[ $revision != "$ACCEPT_COMMIT" ]]; then
    echo "accept: $image is from build '$revision', not $ACCEPT_COMMIT" >&2
    exit 1
  fi
done
got=$(curl -fsS --max-time 5 "http://127.0.0.1:$ACCEPT_WEB_MAP_PORT/build")
if [[ $got != "$ACCEPT_COMMIT" ]]; then
  echo "accept: the web map serves build '$got', not $ACCEPT_COMMIT" >&2
  exit 1
fi
echo "accept: every image is from build $ACCEPT_COMMIT"
