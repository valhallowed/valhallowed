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
# The text rules below match ASCII only: a no-break or a zero-width space is not
# text, and must not make an empty line look filled.
export LC_ALL=C

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

# changed_files N PULL: the file names of pull request N, whose own object is PULL.
# The files API stops at 3000 files without an error. A listing short of the pull
# request's own count means files unseen, so the check stops rather than pass on
# part of the diff. A rename changes both paths, so a file's previous name counts
# as changed too.
changed_files() {
  local n=$1 expected list listed
  expected=$(jq -r '.changed_files // ""' <<< "$2")
  [[ $expected =~ ^[0-9]{1,9}$ ]] || die "pull request #$n has no file count"
  list=$(gh_api --paginate "repos/$GH_REPO/pulls/$n/files?per_page=100" | jq -s 'add // []') \
    || die "cannot list the files of pull request #$n"
  listed=$(jq 'length' <<< "$list")
  if (( listed < expected )); then
    die "pull request #$n is too large to check: GitHub lists $listed of its $expected files"
  fi
  jq -r '.[] | (.filename, (.previous_filename // empty)) | gsub("[[:cntrl:]]"; "?")' <<< "$list"
}

# Drop what is not the author's own answer: <!-- ... --> comments, which may span
# lines (the template's own comment says "No findings"), and fenced code blocks
# (a "## Retro" or "Second reader:" inside a fence is an example, not an answer).
# A comment or a fence still open at the end runs to it, as in Markdown. Each
# dropped line leaves an empty one, and the awk stays free of {n,m} intervals,
# which older awks lack.
strip_hidden() {
  awk '
    {
      indent = match($0, /^ */) ? RLENGTH : 0
      rest = substr($0, indent + 1)
      if (in_fence) {
        # A fence closes on its own character, at least as long, and nothing else.
        closer = rest
        sub(/[ \t]+$/, "", closer)
        if (indent <= 3 && closer ~ ("^" fence_char "+$") && length(closer) >= fence_len) in_fence = 0
        print ""
        next
      }
      # A backtick fence takes no backtick in its info string, so inline code is not one.
      if (!in_comment && indent <= 3 && match(rest, /^(```+|~~~+)/)) {
        fence_char = substr(rest, 1, 1)
        fence_len = RLENGTH
        if (fence_char != "`" || index(substr(rest, fence_len + 1), "`") == 0) {
          in_fence = 1
          print ""
          next
        }
      }
      line = $0
      out = ""
      while (line != "") {
        if (in_comment) {
          i = index(line, "-->")
          if (i == 0) break
          line = substr(line, i + 3)
          in_comment = 0
        } else {
          i = index(line, "<!--")
          if (i == 0) { out = out line; break }
          out = out substr(line, 1, i - 1)
          line = substr(line, i + 4)
          in_comment = 1
        }
      }
      print out
    }'
}

# The Retro section starts at this line, here and in the extraction below.
retro_heading='^## Retro[[:space:]]*$'

pull=$(gh_api "repos/$GH_REPO/pulls/$pr") || die "cannot read pull request #$pr"

# R0: a dependency update is a Dependabot pull request whose every commit is
# Dependabot's. A commit pushed onto its branch makes it a slice, so the rules
# apply. The API lists at most 250 commits, so a count short of the pull
# request's own leaves it a slice too, and so does a commit no GitHub account owns.
author=$(jq -r '.user.login // ""' <<< "$pull")
if [[ $author == 'dependabot[bot]' ]]; then
  bot_only=$(gh_api --paginate "repos/$GH_REPO/pulls/$pr/commits?per_page=100" \
    | jq -rs --argjson pull "$pull" \
      '[.[][] | .author.login // ""] as $logins | ($logins | length) == $pull.commits and all($logins[]; . == "dependabot[bot]")') \
    || die "cannot list the commits of pull request #$pr"
  if [[ $bot_only == true ]]; then
    echo "delivery: a dependency update, not a slice"
    exit 0
  fi
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
files=$(changed_files "$pr" "$pull") || exit 2
older=$(gh_api --paginate "repos/$GH_REPO/pulls?state=open&per_page=100" \
  | jq -r --argjson pr "$pr" '.[] | select(.number < $pr and (.user.login // "") != "dependabot[bot]") | .number') \
  || die "cannot list the open pull requests"
while IFS= read -r other; do
  [[ -n $other ]] || continue
  [[ $other =~ ^[0-9]+$ ]] || die "an open pull request has no number"
  # The list of open pull requests carries no file count, so read each one's own.
  other_pull=$(gh_api "repos/$GH_REPO/pulls/$other") || die "cannot read pull request #$other"
  theirs=$(changed_files "$other" "$other_pull") || exit 2
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
  body=$(jq -r '.body // ""' <<< "$pull" | tr -d '\r' | strip_hidden)
  # The section ends at the next heading of its own level or above.
  retro=$(awk -v heading="$retro_heading" '
    in_retro && /^##? / { exit }
    in_retro { print }
    $0 ~ heading { in_retro = 1 }
  ' <<< "$body")
  has() { grep -Eq -- "$1" <<< "$retro"; }

  if ! grep -Eq -- "$retro_heading" <<< "$body"; then
    fail 'the body has no "## Retro" section'
  else
    has '^[[:space:]]*Reviews:.*[[:alnum:]]' || fail 'the Retro section'\''s "Reviews:" line is empty'
    # A findings row is a table line below the separator row, the one made of
    # pipes, dashes and colons. A line with no text besides those is no row, and
    # a header with no separator under it opens no table.
    rows=$(awk '
      { s = $0; gsub(/[ \t]/, "", s) }
      s !~ /^[|]/ { next }
      s ~ /^[|][-:|]*-[-:|]*$/ { separator = 1; next }
      s ~ /^[|][-:|]*$/ { next }
      separator { n++ }
      END { print n + 0 }
    ' <<< "$retro")
    if (( rows == 0 )) && ! has '^[[:space:]]*No findings\.?[[:space:]]*$'; then
      fail 'the Retro section has neither a findings row nor "No findings"'
    fi
  fi

  protected=$(grep -m1 -E '^((contracts|schema|control|runtime|images|install|scripts|\.github)/|(Makefile|docker-bake\.hcl)$)' <<< "$files" || true)
  if [[ -n $protected ]] && ! has '^[[:space:]]*Second reader:.*[[:alnum:]]'; then
    fail "the Retro section names no second reader, due since the pull request changes $protected"
  fi
fi

if (( failed )); then
  exit 1
fi
echo "delivery: ok"
