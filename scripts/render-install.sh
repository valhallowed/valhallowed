#!/usr/bin/env bash
# Render install/compose.yaml for one version. With --pin, every image in its
# x-vhd block is pinned to the digest the registry holds for that version, as
# release assets are, or for SOURCE: the channel a promotion copies, which holds
# the same images.
set -euo pipefail

usage() { echo "usage: $0 VERSION OUT [--pin [SOURCE]]" >&2; exit 2; }
[[ $# -eq 2 || ($# -ge 3 && $# -le 4 && $3 == --pin) ]] || usage
version=$1 out=$2 pin=${3:-} source=${4:-$1}
tag_re='^[0-9A-Za-z][0-9A-Za-z._-]*$'
[[ $version =~ $tag_re && $source =~ $tag_re ]] || usage

root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$(dirname "$out")"
sed "s/@VHD_VERSION@/$version/g" "$root/install/compose.yaml" > "$out"

if [[ $pin == --pin ]]; then
  mapfile -t refs < <(docker compose -f "$out" config --format json | jq -r '."x-vhd".images // {} | .[]')
  if [[ ${#refs[@]} -eq 0 ]]; then
    echo "render-install: $out lists no images in x-vhd" >&2
    exit 1
  fi
  for ref in "${refs[@]}"; do
    digest=$(timeout 60 docker buildx imagetools inspect "${ref%:*}:$source" --format '{{json .Manifest}}' | jq -r .digest)
    if [[ ! $digest =~ ^sha256:[0-9a-f]{64}$ ]]; then
      echo "render-install: no digest for ${ref%:*}:$source" >&2
      exit 1
    fi
    # The anchor line is the only place the ref is spelled out.
    sed -i "s| $ref\$| $ref@$digest|" "$out"
  done
fi
