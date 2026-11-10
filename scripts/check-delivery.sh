#!/usr/bin/env bash
# Check one pull request against the rules of AGENTS.md § Delivering that a
# machine can read: it closes an issue; it shares no file, docs apart, with an
# older open pull request; it is not a draft; and, once it is ready, its Retro
# section names the reviews and the findings (or says "No findings"), and a
# second reader where the pull request changes a contract, a schema, the
# control plane, the game runtime, an image, the install or the pipeline. A
# Dependabot pull request is exempt: it is not a slice.
# It reads the pull request through the GitHub API and never the working tree,
# so delivery.yml can run it from main while the pull request's own code stays
# unread. Everything the API returns is data: quoted, never evaluated, and a
# file name loses its control characters before it is printed.
#   usage: GH_REPO=owner/name scripts/check-delivery.sh PULL_REQUEST_NUMBER
# Prints one "delivery: ..." line per failed rule and exits 1 if any failed. A
# usage error or an unreadable API answer exits 2.
set -euo pipefail

usage() { echo "usage: GH_REPO=owner/name $0 PULL_REQUEST_NUMBER" >&2; exit 2; }
die() { echo "delivery: $*" >&2; exit 2; }
failed=0
fail() { printf 'delivery: %s\n' "$*"; failed=1; }

[[ $# -eq 1 ]] || usage
pr=$1
[[ $pr =~ ^[1-9][0-9]{0,8}$ ]] || die "the pull request number must be digits, without a leading zero"
[[ -n ${GH_REPO:-} ]] || die "GH_REPO is required, as owner/name"
[[ $GH_REPO =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "GH_REPO must be owner/name"

# Bounded, and cut off from stdin so a call inside a read loop cannot eat the loop's input.
gh_api() { timeout 60 gh api "$@" < /dev/null; }

# A rename changes both paths, so a file's previous name counts as changed too.
changed_files() {
  gh_api --paginate "repos/$GH_REPO/pulls/$1/files?per_page=100" \
    | jq -r '.[] | (.filename, (.previous_filename // empty)) | gsub("[[:cntrl:]]"; "?")'
}

# Drop <!-- ... --> comments, which may span lines: the template's own comment
# says "No findings", and that must not count as the author's answer.
strip_comments() {
  awk '
    {
      rest = $0
      out = ""
      while (rest != "") {
        if (in_comment) {
          i = index(rest, "-->")
          if (i == 0) break
          rest = substr(rest, i + 3)
          in_comment = 0
        } else {
          i = index(rest, "<!--")
          if (i == 0) { out = out rest; break }
          out = out substr(rest, 1, i - 1)
          rest = substr(rest, i + 4)
          in_comment = 1
        }
      }
      print out
    }'
}

pull=$(gh_api "repos/$GH_REPO/pulls/$pr") || die "cannot read pull request #$pr"

# R0: a dependency update is folded into a slice's branch or merged on its own.
author=$(jq -r '.user.login // ""' <<< "$pull")
if [[ $author == 'dependabot[bot]' ]]; then
  echo "delivery: a dependency update, not a slice"
  exit 0
fi

# R1: the pull request closes an issue, through its body or the sidebar.
# The $-names below are GraphQL variables, not shell ones.
# shellcheck disable=SC2016
closing_query='query($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) { pullRequest(number: $number) { closingIssuesReferences(first: 1) { totalCount } } } }'
closing=$(gh_api graphql -f query="$closing_query" -f owner="${GH_REPO%/*}" -f name="${GH_REPO#*/}" -F number="$pr" \
  | jq -e '.data.repository.pullRequest.closingIssuesReferences.totalCount | select(type == "number")') \
  || die "cannot read the issues pull request #$pr closes"
if (( closing < 1 )); then
  fail 'closes no issue: its body needs "Closes #n"'
fi

# R2: no file shared with an older open pull request, docs apart.
files=$(changed_files "$pr") || die "cannot list the files of pull request #$pr"
older=$(gh_api --paginate "repos/$GH_REPO/pulls?state=open&per_page=100" \
  | jq -r --argjson pr "$pr" '.[] | select(.number < $pr and (.user.login // "") != "dependabot[bot]") | .number') \
  || die "cannot list the open pull requests"
while IFS= read -r other; do
  [[ -n $other ]] || continue
  [[ $other =~ ^[0-9]+$ ]] || die "an open pull request has no number"
  theirs=$(changed_files "$other") || die "cannot list the files of pull request #$other"
  shared=$(awk '
    FILENAME == ARGV[1] { mine[$0]; next }
    ($0 in mine) && $0 !~ /^docs\// && $0 !~ /^[^\/]+\.md$/
  ' <(printf '%s\n' "$files") <(printf '%s\n' "$theirs"))
  while IFS= read -r file; do
    [[ -z $file ]] || fail "$file is also changed by older open pull request #$other"
  done <<< "$shared"
done <<< "$older"

# R3 and R4 read the Retro section of a pull request that is ready. A draft fails
# meanwhile: were delivery green on its head commit, a pull request marked ready
# and armed for auto-merge in one step could merge before the run that reads it.
draft=$(jq -r '.draft // false' <<< "$pull")
if [[ $draft == true ]]; then
  fail "draft: mark it ready once its Retro section is written"
else
  body=$(jq -r '.body // ""' <<< "$pull" | tr -d '\r' | strip_comments)
  retro=$(awk '
    in_retro && /^## / { exit }
    in_retro { print }
    /^## Retro[ \t]*$/ { in_retro = 1 }
  ' <<< "$body")
  has() { grep -Eq -- "$1" <<< "$retro"; }

  if ! grep -Eq '^## Retro[[:space:]]*$' <<< "$body"; then
    fail 'the body has no "## Retro" section'
  else
    has '^[[:space:]]*Reviews:[[:space:]]*[^[:space:]]' || fail 'the Retro section'\''s "Reviews:" line is empty'
    # A table row counts when it holds more than pipes, dashes and colons, and is
    # not the template's header.
    rows=$(awk '
      { s = $0; gsub(/[ \t]/, "", s) }
      s !~ /^[|]/ { next }
      s ~ /^[|][-:|]*$/ || s == "|Finding|Fixedin|" { next }
      { n++ }
      END { print n + 0 }
    ' <<< "$retro")
    if (( rows == 0 )) && ! has '^[[:space:]]*No findings[[:space:]]*$'; then
      fail 'the Retro section has neither a findings row nor "No findings"'
    fi
  fi

  protected=$(grep -m1 -E '^((contracts|schema|control|runtime|images|install|scripts|\.github)/|(Makefile|docker-bake\.hcl)$)' <<< "$files" || true)
  if [[ -n $protected ]] && ! has '^[[:space:]]*Second reader:[[:space:]]*[^[:space:]]'; then
    fail "the Retro section names no second reader, due since the pull request changes $protected"
  fi
fi

if (( failed )); then
  exit 1
fi
echo "delivery: ok"
