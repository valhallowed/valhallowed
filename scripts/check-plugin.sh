#!/usr/bin/env bash
# Check a plugin build's output: the plugin and the game build it was compiled
# against, and nothing else, so no game file leaves the build.
set -euo pipefail

[[ $# -eq 1 ]] || { echo "usage: $0 DIR" >&2; exit 2; }
dir=$1

expected=$(printf '%s\n' Valhallowed.Plugin.dll game-build | sort)
got=$(cd "$dir" && find . -mindepth 1 -printf '%P\n' | sort)
if [[ $got != "$expected" ]]; then
  printf 'check-plugin: %s holds:\n%s\nexpected exactly:\n%s\n' "$dir" "$got" "$expected" >&2
  exit 1
fi
if [[ $(head -c 2 "$dir/Valhallowed.Plugin.dll") != MZ ]]; then
  echo "check-plugin: $dir/Valhallowed.Plugin.dll is not a PE file" >&2
  exit 1
fi
build=$(<"$dir/game-build")
if [[ ! $build =~ ^[0-9]+$ ]]; then
  echo "check-plugin: $dir/game-build holds '$build', not a game build id" >&2
  exit 1
fi
echo "check-plugin: plugin built against game build $build"
