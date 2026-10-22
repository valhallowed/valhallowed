#!/usr/bin/env bash
# Print the build id of the dedicated server's public branch, as Steam reports it
# now. The plugin and the game runtime image are built against it (GAME_BUILD),
# so a build reuses the downloaded assemblies exactly while the game is unchanged.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

docker buildx bake steamcmd >&2
info=$(timeout 300 docker run --rm vhd-steamcmd:local \
  /steamcmd/steamcmd.sh +login anonymous +app_info_update 1 +app_info_print 896660 +quit)
build=$(awk '/"branches"/ { branches = 1 } branches && /"public"/ { public = 1 }
  public && /"buildid"/ { gsub(/"/, "", $2); print $2; exit }' <<< "$info")
if [[ ! $build =~ ^[0-9]+$ ]]; then
  echo "game-build: Steam reported no public build for the dedicated server" >&2
  exit 1
fi
echo "$build"
