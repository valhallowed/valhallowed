#!/usr/bin/env bash
# Promote images already in the registry to a version tag. Nothing is rebuilt:
#   vX.Y.Z-rc.N  candidate  promotes the commit channel's sha-<commit> images,
#                           and is refused unless ci's accept job passed on
#                           that commit
#   vX.Y.Z       release    promotes the newest candidate on the same commit,
#                           and moves latest
# With --check, only checks that TAG can be promoted and prints version=,
# prerelease=, commit= and source= lines for the workflow. Without it, tags
# exactly the digests COMPOSE_FILE pins: the file acceptance just passed.
# Reading ci's result needs GITHUB_TOKEN and GITHUB_REPOSITORY.
set -euo pipefail

usage() { echo "usage: $0 --check TAG | $0 TAG COMPOSE_FILE" >&2; exit 2; }
if [[ $# -eq 2 && $1 == --check ]]; then
  check=true tag=$2
elif [[ $# -eq 2 ]]; then
  check=false tag=$1 compose_file=$2
else
  usage
fi
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

mapfile -t refs < <(TAGS=$version docker buildx bake --print 2>/dev/null | jq -r '.target[].tags[]')
if [[ ${#refs[@]} -eq 0 ]]; then
  echo "promote: docker buildx bake --print lists no images" >&2
  exit 1
fi

if [[ $check == true ]]; then
  for ref in "${refs[@]}"; do
    repo=${ref%:*}
    if ! docker buildx imagetools inspect "$repo:$source" >/dev/null 2>&1; then
      echo "promote: $repo:$source is not in the registry; wait for ci on main to publish it" >&2
      exit 1
    fi
  done
  printf 'version=%s\nprerelease=%s\ncommit=%s\nsource=%s\n' "$version" "$prerelease" "$commit" "$source"
  exit 0
fi

for ref in "${refs[@]}"; do
  repo=${ref%:*}
  digest=$(awk -v ref="$ref" '$1 == "image:" && index($2, ref "@") == 1 { print substr($2, length(ref) + 2) }' "$compose_file")
  if [[ ! $digest =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "promote: $compose_file does not pin $ref; only accepted images are promoted" >&2
    exit 1
  fi
  tags=(--tag "$ref")
  if [[ $prerelease == false ]]; then
    tags+=(--tag "$repo:latest")
  fi
  docker buildx imagetools create "${tags[@]}" "$repo@$digest" >&2
done
