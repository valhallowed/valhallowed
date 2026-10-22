#!/usr/bin/env bash
# Promote images already in the registry to a version tag. Nothing is rebuilt:
#   vX.Y.Z-rc.N  candidate  promotes the commit channel's sha-<commit> images,
#                           and is refused unless ci's accept job passed on
#                           that commit
#   vX.Y.Z       release    promotes the newest candidate on the same commit,
#                           and moves latest
# Prints version=, prerelease= and commit= lines for the workflow.
# Reading ci's result needs GITHUB_TOKEN and GITHUB_REPOSITORY.
set -euo pipefail

[[ $# -eq 1 ]] || { echo "usage: $0 TAG" >&2; exit 2; }
tag=$1
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
for ref in "${refs[@]}"; do
  repo=${ref%:*}
  if ! docker buildx imagetools inspect "$repo:$source" >/dev/null 2>&1; then
    echo "promote: $repo:$source is not in the registry; wait for ci on main to publish it" >&2
    exit 1
  fi
  tags=(--tag "$ref")
  if [[ $prerelease == false ]]; then
    tags+=(--tag "$repo:latest")
  fi
  docker buildx imagetools create "${tags[@]}" "$repo:$source" >&2
done

printf 'version=%s\nprerelease=%s\ncommit=%s\n' "$version" "$prerelease" "$commit"
