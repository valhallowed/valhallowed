#!/usr/bin/env bash
# Fail when a pinned download Dependabot cannot watch has a newer upstream
# release: the Docker CLI and Compose in the control plane's image, and the
# BepInEx pack in the game runtime's. Each version is an ARG in its Dockerfile;
# moving to a newer one changes that ARG and its checksums. The nightly game
# check runs this. GITHUB_TOKEN, when set, lifts GitHub's anonymous rate limit.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

version_re='^[0-9]+(\.[0-9]+)+$'
pinned_version() { sed -n "s/^ARG $1=//p" "$2"; }
get() { curl -fsS --max-time 30 --retry 3 "$@"; }
github() {
  if [[ -n ${GITHUB_TOKEN:-} ]]; then
    printf 'Authorization: Bearer %s\n' "$GITHUB_TOKEN" | get -H @- "https://api.github.com/$1"
  else
    get "https://api.github.com/$1"
  fi
}

failed=0
compare() {
  local what=$1 pinned=$2 latest=$3
  if [[ ! $pinned =~ $version_re || ! $latest =~ $version_re ]]; then
    echo "check-upstream: $what: cannot compare pinned '$pinned' with released '$latest'" >&2
    failed=1
  elif [[ $(printf '%s\n%s\n' "$pinned" "$latest" | sort -V | tail -n1) != "$pinned" ]]; then
    echo "check-upstream: $what $pinned is pinned; $latest is released" >&2
    failed=1
  else
    echo "check-upstream: $what $pinned is current"
  fi
}

compare "Docker CLI" "$(pinned_version DOCKER_VERSION images/control-plane/Dockerfile)" \
  "$(get https://download.docker.com/linux/static/stable/x86_64/ \
    | grep -oE 'docker-[0-9]+\.[0-9]+\.[0-9]+\.tgz' | sed 's/^docker-//; s/\.tgz$//' | sort -uV | tail -n1)"
compare "Compose" "$(pinned_version COMPOSE_VERSION images/control-plane/Dockerfile)" \
  "$(github repos/docker/compose/releases/latest | jq -r '.tag_name | ltrimstr("v")')"
compare "BepInEx pack" "$(pinned_version BEPINEX_PACK_VERSION images/game-runtime/Dockerfile)" \
  "$(get https://thunderstore.io/api/experimental/package/denikson/BepInExPack_Valheim/ | jq -r .latest.version_number)"
exit "$failed"
