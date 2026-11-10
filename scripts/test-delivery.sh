#!/usr/bin/env bash
# Test scripts/check-delivery.sh without a network. A stub gh, first on PATH,
# answers from fixtures this script writes to a temp directory, so each case
# builds the pull requests it needs and asserts the exit code and the exact
# "delivery:" lines. CI runs it on every pull request; run it after changing
# check-delivery.sh.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
script=$root/scripts/check-delivery.sh
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fix=$tmp/fixtures
mkdir -p "$tmp/bin" "$tmp/cwd" "$fix"
# The script reads the API and never the working tree: run it from an empty directory.
export PATH=$tmp/bin:$PATH FIXTURES=$fix

# gh api, answered from fixtures: pull-N.json, files-N.json, open.json and
# graphql-N.json. A missing fixture is a 404.
cat > "$tmp/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
[[ ${1:-} == api ]] || { echo "stub gh: only 'api' is stubbed: $*" >&2; exit 1; }
shift
endpoint='' number='' owner='' name=''
while (( $# )); do
  case $1 in
    --paginate) ;;
    -f) shift; case $1 in owner=*) owner=${1#owner=} ;; name=*) name=${1#name=} ;; esac ;;
    -F) shift; case $1 in number=*) number=${1#number=} ;; esac ;;
    *) endpoint=$1 ;;
  esac
  shift
done
prefix=repos/$GH_REPO/
case $endpoint in
  graphql)
    [[ $owner/$name == "$GH_REPO" ]] || { echo "stub gh: graphql asked for $owner/$name" >&2; exit 1; }
    file=graphql-$number.json ;;
  "${prefix}pulls?state=open&per_page=100") file=open.json ;;
  "${prefix}pulls/"*"/files?per_page=100")
    n=${endpoint#"${prefix}pulls/"}
    file=files-${n%%/*}.json ;;
  "${prefix}pulls/"*) file=pull-${endpoint#"${prefix}pulls/"}.json ;;
  *) echo "stub gh: unexpected endpoint: $endpoint" >&2; exit 1 ;;
esac
[[ -f $FIXTURES/$file ]] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
cat "$FIXTURES/$file"
STUB
chmod +x "$tmp/bin/gh"

# pull N AUTHOR DRAFT BODY
pull() {
  jq -n --arg login "$2" --argjson draft "$3" --arg body "$4" \
    '{user: {login: $login}, draft: $draft, body: $body}' > "$fix/pull-$1.json"
}
# files N PATH...: the changed files, on one page
files() {
  local n=$1
  shift
  jq -n '$ARGS.positional | map({filename: .})' --args "$@" > "$fix/files-$n.json"
}
# closes N COUNT
closes() {
  jq -n --argjson count "$2" \
    '{data: {repository: {pullRequest: {closingIssuesReferences: {totalCount: $count}}}}}' > "$fix/graphql-$1.json"
}
# open_prs NUMBER:LOGIN...
open_prs() {
  jq -n '[$ARGS.positional[] | split(":") | {number: (.[0] | tonumber), user: {login: .[1]}}]' \
    --args "$@" > "$fix/open.json"
}
# body LINE...: a body whose Retro section holds the lines given
body() {
  printf '## What this delivers\n\nCloses #1\n\n## Retro\n\n'
  printf '%s\n' "$@"
}
# retro LINE...: the body of pull request 7 becomes one with that Retro section
retro() { pull 7 alice false "$(body "$@")"; }

# ready: pull request 7, ready and by alice, passes every rule. Each case changes what it tests.
ready() {
  rm -f "$fix"/*
  retro 'Reviews: the whole diff, by the author' 'No findings'
  files 7 plugin/a.cs
  closes 7 1
  open_prs 7:alice
}

failures=0
# run [ARG...]: the script's stdout lands in $out, its stderr in $err, its exit code in $code.
# GH_REPO is example/repo unless $repo is set.
run() {
  code=0
  out=$(cd "$tmp/cwd" && GH_REPO=${repo-example/repo} "$script" "$@" 2> "$tmp/err") || code=$?
  err=$(< "$tmp/err")
}
# verdict NAME OK [DETAIL]
verdict() {
  if [[ $2 == true ]]; then
    printf 'ok   %s\n' "$1"
  else
    printf 'FAIL %s\n%s\n' "$1" "${3:-}"
    failures=$((failures + 1))
  fi
}
# check NAME CODE LINE...: run on pull request 7, expect that exit code and exactly those stdout lines
check() {
  local name=$1 want_code=$2 want
  shift 2
  want=$(printf '%s\n' "$@")
  run 7
  verdict "$name" "$([[ $code == "$want_code" && $out == "$want" ]] && echo true || echo false)" \
    "  want exit $want_code, got $code
  want: $want
  got:  $out
  stderr: $err"
}
# check_error NAME CODE TEXT [ARG...]: run with those arguments, expect that exit code, nothing on
# stdout, and TEXT on stderr
check_error() {
  local name=$1 want_code=$2 text=$3
  shift 3
  run "$@"
  verdict "$name" "$([[ $code == "$want_code" && -z $out && $err == *"$text"* ]] && echo true || echo false)" \
    "  want exit $want_code and stderr with '$text', got $code
  stdout: $out
  stderr: $err"
}

ok='delivery: ok'
no_issue='delivery: closes no issue: its body needs "Closes #n"'
no_reviews='delivery: the Retro section'\''s "Reviews:" line is empty'
no_findings='delivery: the Retro section has neither a findings row nor "No findings"'
no_retro='delivery: the body has no "## Retro" section'
# A command that would leave a mark if anything evaluated the text around it
evil="touch $tmp/pwned"
evil_text="\$($evil) \`$evil\`"
shared() { printf 'delivery: %s is also changed by older open pull request #%s' "$1" "$2"; }
no_reader() { printf 'delivery: the Retro section names no second reader, due since the pull request changes %s' "$1"; }

# The rules that pass
ready
check 'a ready pull request that closes an issue, with "No findings"' 0 "$ok"

ready
retro 'Reviews: the whole diff, by the author' '' '| Finding | Fixed in |' '|---|---|' '| A name that misled | 3f2a9c1 |'
check 'a findings table with a row' 0 "$ok"

ready
retro 'Reviews: the whole diff' '' '| Finding | Fixed in |' '|---|---|' '| A gap in a later slice | task in #12 |' '| A stale comment | 9b1c0de |'
check 'a findings table with two rows' 0 "$ok"

ready
retro 'Reviews: the whole diff' '   No findings   '
check '"No findings" with surrounding whitespace' 0 "$ok"

ready
retro 'Reviews: <!-- by whom --> the author, then Ana' 'No findings'
check 'text beside a comment on the Reviews line' 0 "$ok"

ready
pull 7 alice false "$(body 'Reviews: the whole diff' 'No findings' | sed 's/$/\r/')"
check 'a body with \r\n line endings' 0 "$ok"

ready
files 7 scripts/check-delivery.sh
pull 7 alice false "$(body 'Reviews: the whole diff' 'Second reader: Ana' 'No findings' | sed 's/$/\r/')"
check 'a body with \r\n line endings, and a protected path with a second reader' 0 "$ok"

# R0: a dependency update
ready
pull 7 'dependabot[bot]' false ''
rm "$fix"/graphql-7.json "$fix"/files-7.json "$fix"/open.json
check 'a dependabot[bot] author passes at R0, before anything else is read' 0 'delivery: a dependency update, not a slice'

# R1
ready
closes 7 0
check 'closes no issue' 1 "$no_issue"

ready
closes 7 2
check 'closes two issues' 0 "$ok"

ready
pull 7 alice true ''
closes 7 0
check 'a draft that closes no issue still fails' 1 "$no_issue" 'delivery: draft, so its Retro section is not checked yet'

# R3 and R4 on a draft
ready
pull 7 alice true "$(body 'Reviews:')"
files 7 scripts/check-delivery.sh
check 'a draft with an empty Retro passes, and R3 and R4 are skipped' 0 \
  'delivery: draft, so its Retro section is not checked yet' "$ok"

# R3 on a ready pull request
ready
pull 7 alice false "$(< "$root/.github/pull_request_template.md")"
check 'the untouched template fails: its comment saying "No findings" does not count' 1 "$no_reviews" "$no_findings"

ready
pull 7 alice false "$(< "$root/.github/pull_request_template.md")"
files 7 scripts/check-delivery.sh
check 'the untouched template, with a protected path, also lacks a second reader' 1 \
  "$no_reviews" "$no_findings" "$(no_reader scripts/check-delivery.sh)"

ready
pull 7 alice false ''
check 'an empty body has no Retro section' 1 "$no_retro"

ready
pull 7 alice false $'## What this delivers\n\nCloses #1\n'
check 'a body with no Retro section' 1 "$no_retro"

ready
retro 'Reviews:' 'No findings'
check 'an empty Reviews line' 1 "$no_reviews"

ready
retro 'Reviews: <!-- who -->' 'No findings'
check 'a Reviews line holding only a comment' 1 "$no_reviews"

ready
retro 'Reviews: the whole diff'
check 'no findings table and no "No findings"' 1 "$no_findings"

ready
retro 'Reviews: the whole diff' '' '| Finding | Fixed in |' '|---|---|'
check 'a findings table with a header and a separator only' 1 "$no_findings"

ready
retro 'Reviews: the whole diff' '' '| Finding | Fixed in |' '|---|---|' '| | |'
check 'a findings table whose only row is empty' 1 "$no_findings"

ready
retro 'Reviews: the whole diff' 'No findings yet'
check '"No findings" inside a longer line does not count' 1 "$no_findings"

ready
retro 'Reviews: the whole diff' '<!--' 'No findings' '-->'
check '"No findings" in a multi-line comment does not count' 1 "$no_findings"

ready
pull 7 alice false "$(body 'Reviews: the whole diff' '' '## Notes' '' 'No findings')"
check '"No findings" after the next heading does not count' 1 "$no_findings"

ready
retro 'Reviews: the whole diff' 'No findings' '' "Reviews: $evil_text"
check 'a body with shell syntax in it is data' 0 "$ok"
verdict 'a body with shell syntax in it ran nothing' "$([[ ! -e $tmp/pwned ]] && echo true || echo false)"

# R4
ready
files 7 scripts/check-delivery.sh
check 'a protected path with no second reader' 1 "$(no_reader scripts/check-delivery.sh)"

ready
files 7 scripts/check-delivery.sh
retro 'Reviews: the whole diff, and Ana' 'Second reader: Ana' 'No findings'
check 'a protected path with a second reader' 0 "$ok"

ready
files 7 scripts/check-delivery.sh
retro 'Reviews: the whole diff' 'Second reader: <!-- who read the whole diff -->' 'No findings'
check 'a protected path with an empty Second reader line' 1 "$(no_reader scripts/check-delivery.sh)"

for path in contracts/b1.md schema/manifest.json control/app.py runtime/run.py images/game/Dockerfile \
  install/compose.yaml scripts/a.sh .github/workflows/ci.yml Makefile docker-bake.hcl; do
  ready
  files 7 docs/guide.md "$path"
  check "protected: $path" 1 "$(no_reader "$path")"
done

for path in plugin/Makefile plugin/scripts/a.sh docs/makefile.md scripts-old/a.sh data/a.py web-map/a.js; do
  ready
  files 7 "$path"
  check "not protected: $path" 0 "$ok"
done

# R2
ready
files 7 plugin/a.cs plugin/b.cs
files 3 plugin/b.cs plugin/c.cs
open_prs 3:bob 7:alice
check 'a file shared with an older open pull request' 1 "$(shared plugin/b.cs 3)"

ready
files 9 plugin/a.cs
open_prs 7:alice 9:bob
check 'a file shared only with a newer pull request' 0 "$ok"

for path in docs/glossary.md docs/diagrams/pipeline.html README.md; do
  ready
  files 7 "$path"
  files 3 "$path"
  open_prs 3:bob 7:alice
  check "a shared doc: $path" 0 "$ok"
done

ready
files 7 plugin/README.md
files 3 plugin/README.md
open_prs 3:bob 7:alice
check 'a shared *.md file below the root is not a doc' 1 "$(shared plugin/README.md 3)"

ready
files 3 plugin/a.cs
open_prs 3:'dependabot[bot]' 7:alice
check 'a file shared with an older dependabot[bot] pull request' 0 "$ok"

ready
files 3 plugin/other.cs
open_prs 3:bob 7:alice
check 'an older open pull request with no file in common' 0 "$ok"

ready
files 7 plugin/a.cs
jq -n '[{filename: "plugin/new.cs", previous_filename: "plugin/a.cs"}]' > "$fix/files-3.json"
open_prs 3:bob 7:alice
check 'a file an older pull request renames' 1 "$(shared plugin/a.cs 3)"

ready
{ jq -n '[{filename: "plugin/x.cs"}]'; jq -n '[{filename: "plugin/a.cs"}]'; } > "$fix/files-3.json"
open_prs 3:bob 7:alice
check 'a shared file on the second page of files' 1 "$(shared plugin/a.cs 3)"

ready
files 7 plugin/a.cs plugin/b.cs
files 3 plugin/a.cs
files 5 plugin/b.cs
open_prs 3:bob 5:carol 7:alice
check 'files shared with two older pull requests' 1 "$(shared plugin/a.cs 3)" "$(shared plugin/b.cs 5)"

ready
files 7 "plugin/we ird*\$($evil).cs"
files 3 "plugin/we ird*\$($evil).cs"
open_prs 3:bob 7:alice
check 'a file name with shell syntax is data' 1 "$(shared "plugin/we ird*\$($evil).cs" 3)"
verdict 'a file name with shell syntax ran nothing' "$([[ ! -e $tmp/pwned ]] && echo true || echo false)"

ready
files 7 $'plugin/a\nb.cs'
files 3 $'plugin/a\nb.cs'
open_prs 3:bob 7:alice
check 'a file name with a newline stays on one line' 1 "$(shared 'plugin/a?b.cs' 3)"

# Every rule is checked
ready
closes 7 0
retro 'Reviews:'
files 7 scripts/check-delivery.sh plugin/a.cs
files 3 plugin/a.cs
open_prs 3:bob 7:alice
check 'every failed rule is printed, in order' 1 "$no_issue" "$(shared plugin/a.cs 3)" "$no_reviews" "$no_findings" \
  "$(no_reader scripts/check-delivery.sh)"

# Usage and API errors
ready
check_error 'a missing argument' 2 'usage:'
check_error 'a non-numeric argument' 2 'must be digits' abc
check_error 'a number with a leading zero' 2 'must be digits' 07
check_error 'two arguments' 2 'usage:' 7 8
repo='' check_error 'a missing GH_REPO' 2 'GH_REPO is required' 7
repo='not a repo' check_error 'a malformed GH_REPO' 2 'GH_REPO must be owner/name' 7

ready
rm "$fix/pull-7.json"
check_error 'an unreadable pull request' 2 'cannot read pull request #7' 7

ready
rm "$fix/graphql-7.json"
check_error 'unreadable closing issues' 2 'cannot read the issues pull request #7 closes' 7

ready
open_prs 3:bob 7:alice
check_error 'unreadable files of an older pull request' 2 'cannot list the files of pull request #3' 7

if (( failures )); then
  echo "test-delivery: $failures failed" >&2
  exit 1
fi
echo "test-delivery: all passed"
