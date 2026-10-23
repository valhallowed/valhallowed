#!/usr/bin/env bash
# Promote images already in the registry to a version tag. Nothing is rebuilt:
#   vX.Y.Z-rc.N  candidate  promotes the commit channel's sha-<commit> images,
#                           and is refused unless ci's accept job passed on
#                           that commit
#   vX.Y.Z       release    promotes the newest candidate on the same commit,
#                           and moves latest
# The images are the install's x-vhd block, which check-images.sh holds equal to
# what CI builds. With --check, only checks that TAG can be promoted and prints
# version=, prerelease=, commit= and source= lines for the workflow. Without it,
# tags exactly the digests COMPOSE_FILE pins: the file acceptance just passed,
# every version tag first and latest after, each retried, so a registry failure
# never leaves latest on a version some image lacks, and a re-run finishes the
# job. Reading ci's result needs GITHUB_TOKEN and GITHUB_REPOSITORY.
set -euo pipefail

usage() { echo "usage: $0 --check TAG | $0 TAG COMPOSE_FILE" >&2; exit 2; }
if [[ $# -eq 2 && $1 == --check ]]; then
  check=true tag=$2
elif [[ $# -eq 2 ]]; then
  check=false tag=$1 compose_file=$(realpath -e "$2")
else
  usage
fi
cd "$(git rev-parse --show-toplevel)"
if [[ $tag =~ ^v([0-9]+\.[0-9]+\.[0-9]+)-rc\.[0-9]+$ ]]; then
  prerelease=true
elif [[ $tag =~ ^v([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
  prerelease=false
else
  echo "promote: $tag is neither vX.Y.Z nor vX.Y.Z-rc.N" >&2
  exit 2
fi
base=${BASH_REMATCH[1]}
version=${tag#v}
commit=$(git rev-parse "$tag^{commit}")

github() {
  curl -fsS --max-time 20 --retry 3 \
    -H "Authorization: Bearer ${GITHUB_TOKEN:?promote: GITHUB_TOKEN is required}" \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/${GITHUB_REPOSITORY:?promote: GITHUB_REPOSITORY is required}/$1"
}

# The images a compose file lists in its x-vhd block, one per line.
listed() {
  docker compose -f "$1" config --format json | jq -r '."x-vhd".images // {} | .[]'
}

scripts/check-images.sh >&2

if [[ $prerelease == true ]]; then
  source=sha-${commit::7}
  runs=$(github "actions/workflows/ci.yml/runs?head_sha=$commit&per_page=100" | jq -r '.workflow_runs[].id')
  accepted=false
  for run in $runs; do
    jobs=$(github "actions/runs/$run/jobs?per_page=100")
    if jq -e 'any(.jobs[]; .name == "accept" and .conclusion == "success")' <<< "$jobs" >/dev/null; then
      accepted=true
      break
    fi
  done
  if [[ $accepted == false ]]; then
    echo "promote: ci's accept job has not passed on ${commit::7}; a candidate needs it" >&2
    exit 1
  fi
else
  rc=$(git tag --points-at "$commit" | grep -E "^v${base//./\\.}-rc\.[0-9]+$" | sort -V | tail -n1 || true)
  if [[ -z $rc ]]; then
    echo "promote: no candidate tag for $version on ${commit::7}; tag and soak v$version-rc.N first" >&2
    exit 1
  fi
  source=${rc#v}
fi

if [[ $check == true ]]; then
  rendered=$(mktemp --suffix=.yaml)
  trap 'rm -f "$rendered"' EXIT
  scripts/render-install.sh "$version" "$rendered"
  mapfile -t refs < <(listed "$rendered")
  for ref in "${refs[@]}"; do
    repo=${ref%:*}
    if ! timeout 60 docker buildx imagetools inspect "$repo:$source" >/dev/null 2>&1; then
      echo "promote: $repo:$source is not in the registry; wait for ci on main to publish it" >&2
      exit 1
    fi
  done
  printf 'version=%s\nprerelease=%s\ncommit=%s\nsource=%s\n' "$version" "$prerelease" "$commit" "$source"
  exit 0
fi

# Every image must be pinned for this version before anything is tagged, so a
# refusal never leaves a partial promotion behind.
mapfile -t pinned < <(listed "$compose_file")
if [[ ${#pinned[@]} -eq 0 ]]; then
  echo "promote: $compose_file lists no images in x-vhd" >&2
  exit 1
fi
for value in "${pinned[@]}"; do
  ref=${value%@*} digest=${value##*@}
  if [[ $ref != *:"$version" || ! $digest =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "promote: $compose_file pins '$value', not a $version image by digest; only accepted images are promoted" >&2
    exit 1
  fi
done

# Tag a digest in the registry. Tagging the same digest again changes nothing,
# so a failure is retried with backoff before giving up.
tag() {
  local name=$1 from=$2 attempt
  for attempt in 1 2 3 4; do
    if timeout 120 docker buildx imagetools create --tag "$name" "$from" >&2; then
      return 0
    fi
    if (( attempt < 4 )); then
      sleep $(( attempt * attempt * 5 + RANDOM % 5 ))
    fi
  done
  echo "promote: could not tag $name; run the release workflow for $tag again to finish" >&2
  return 1
}

for value in "${pinned[@]}"; do
  ref=${value%@*} digest=${value##*@}
  tag "$ref" "${ref%:*}@$digest"
done
if [[ $prerelease == false ]]; then
  for value in "${pinned[@]}"; do
    ref=${value%@*} digest=${value##*@}
    tag "${ref%:*}:latest" "${ref%:*}@$digest"
  done
fi
