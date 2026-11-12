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

# gh api, answered from fixtures: pull-N.json, files-N.json, commits-N.json,
# open.json and graphql-N.json. A missing fixture is a 404. A fixture holding
# several JSON values is a list on several pages: without --paginate, only the
# first page is served.
cat > "$tmp/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
[[ ${1:-} == api ]] || { echo "stub gh: only 'api' is stubbed: $*" >&2; exit 1; }
shift
endpoint='' number='' owner='' name='' paginate=false
while (( $# )); do
  case $1 in
    --paginate) paginate=true ;;
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
  "${prefix}pulls/"*"/commits?per_page=100")
    n=${endpoint#"${prefix}pulls/"}
    file=commits-${n%%/*}.json ;;
  "${prefix}pulls/"*) file=pull-${endpoint#"${prefix}pulls/"}.json ;;
  *) echo "stub gh: unexpected endpoint: $endpoint" >&2; exit 1 ;;
esac
[[ -f $FIXTURES/$file ]] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
if [[ $paginate == true ]]; then
  cat "$FIXTURES/$file"
else
  jq -n input "$FIXTURES/$file"
fi
STUB
chmod +x "$tmp/bin/gh"

# set_pull N FILTER [JQ_ARG...]: edit pull request N's fixture in place
set_pull() {
  local f=$fix/pull-$1.json
  [[ -f $f ]] || echo '{}' > "$f"
  jq "${@:3}" "$2" "$f" > "$f.new"
  mv "$f.new" "$f"
}
# pull N AUTHOR DRAFT BODY: the counts of files and commits an earlier files or commits set stay
pull() {
  local f=$fix/pull-$1.json old='{}'
  [[ ! -f $f ]] || old=$(< "$f")
  jq -n --argjson old "$old" --arg login "$2" --argjson draft "$3" --arg body "$4" \
    '$old + {user: {login: $login}, draft: $draft, body: $body}' > "$f"
}
# commits N LOGIN...: the authors of pull request N's commits, on one page, each signed and
# verified; an empty LOGIN is a commit no GitHub account owns
commits() {
  local n=$1
  shift
  jq -n '$ARGS.positional
    | map({author: (if . == "" then null else {login: .} end), commit: {verification: {verified: true}}})' \
    --args "$@" \
    > "$fix/commits-$n.json"
  set_pull "$n" ".commits = $#"
}
# files N PATH...: the changed files, on one page, and the file count of pull request N
files() {
  local n=$1
  shift
  jq -n '$ARGS.positional | map({filename: .})' --args "$@" > "$fix/files-$n.json"
  set_pull "$n" ".changed_files = $#"
}
# files_raw N: the pages of files given as JSON on stdin, served as they are, and the file count
# of pull request N
files_raw() {
  cat > "$fix/files-$1.json"
  set_pull "$1" ".changed_files = $(jq -s 'add | length' "$fix/files-$1.json")"
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
draft_fails='delivery: draft: mark it ready once its Retro section is written'
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

# R0: a dependency update is a Dependabot pull request whose every commit is Dependabot's
bot='dependabot[bot]'
not_slice='delivery: a dependency update, not a slice'
ready
pull 7 "$bot" false ''
commits 7 "$bot"
rm "$fix"/graphql-7.json "$fix"/files-7.json "$fix"/open.json
check 'a dependabot[bot] pull request of dependabot[bot] commits passes at R0, before anything else is read' 0 "$not_slice"

ready
pull 7 "$bot" false ''
commits 7 "$bot"
jq -s 'add' "$fix/commits-7.json" "$fix/commits-7.json" > "$fix/one-page.json"
jq '.[0:1], .[1:2]' "$fix/one-page.json" > "$fix/commits-7.json"
set_pull 7 '.commits = 2'
check 'dependabot[bot] commits on two pages pass at R0' 0 "$not_slice"

ready
pull 7 "$bot" false ''
commits 7 "$bot"
jq '.[0].commit.verification.verified = false' "$fix/commits-7.json" > "$fix/unsigned.json"
mv "$fix/unsigned.json" "$fix/commits-7.json"
check 'a dependabot[bot] commit GitHub has not verified: every rule applies' 1 "$no_retro"

ready
pull 7 "$bot" false ''
commits 7 "$bot" alice
check 'a commit by someone else on a dependabot[bot] pull request: every rule applies' 1 "$no_retro"

ready
pull 7 "$bot" false ''
closes 7 0
commits 7 "$bot" ''
check 'a commit no GitHub account owns on a dependabot[bot] pull request: every rule applies' 1 "$no_issue" "$no_retro"

ready
pull 7 "$bot" false ''
commits 7 "$bot"
set_pull 7 '.commits = 300'
check 'fewer commits listed than the pull request has: every rule applies' 1 "$no_retro"

ready
pull 7 "$bot" false ''
commits 7 "$bot"
set_pull 7 'del(.commits)'
check 'no commit count on the pull request: every rule applies' 1 "$no_retro"

ready
pull 7 alice false ''
commits 7 "$bot"
check 'dependabot[bot] commits on a pull request by someone else: every rule applies' 1 "$no_retro"

ready
pull 7 "$bot" false ''
check_error 'unreadable commits of a dependabot[bot] pull request' 2 'cannot list the commits of pull request #7' 7

# R1
ready
closes 7 0
check 'closes no issue' 1 "$no_issue"

ready
closes 7 2
check 'closes two issues' 0 "$ok"

# A draft: R1 and R2 report, R3 and R4 wait, and it fails until it is ready
ready
pull 7 alice true ''
closes 7 0
check 'a draft that closes no issue fails on both counts' 1 "$no_issue" "$draft_fails"

ready
pull 7 alice true "$(body 'Reviews:')"
files 7 scripts/check-delivery.sh
check 'a draft with an empty Retro fails, and R3 and R4 are skipped' 1 "$draft_fails"

ready
pull 7 alice true "$(body 'Reviews: the whole diff' 'No findings')"
check 'a draft with a filled Retro still fails' 1 "$draft_fails"

ready
pull 7 alice true ''
files 7 plugin/a.cs
files 3 plugin/a.cs
open_prs 3:bob 7:alice
check 'a draft that shares a file with an older pull request fails on both counts' 1 "$(shared plugin/a.cs 3)" "$draft_fails"

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
pull 7 alice false "$(body 'Reviews: the whole diff' 'No findings' '' '# Appendix' '' 'No findings')"
check 'a top-level heading ends the Retro section' 0 "$ok"

ready
pull 7 alice false "$(body 'Reviews: the whole diff' '' '# Appendix' '' 'No findings')"
check '"No findings" after a top-level heading does not count' 1 "$no_findings"

ready
retro 'Reviews: the whole diff' '### Notes' 'No findings'
check 'a deeper heading does not end the Retro section' 0 "$ok"

ready
retro 'Reviews: the whole diff' 'No findings.'
check '"No findings" with a final full stop' 0 "$ok"

# A findings row sits below the separator row
ready
retro 'Reviews: the whole diff' '' '| Found | In |' '|---|---|'
check 'a table with another header and no row' 1 "$no_findings"

ready
retro 'Reviews: the whole diff' '' '| Finding | Fixed in |' '| A name that misled | 3f2a9c1 |'
check 'a table row with no separator above it' 1 "$no_findings"

ready
retro 'Reviews: the whole diff' '' '| A name that misled | 3f2a9c1 |'
check 'a table row with no header' 1 "$no_findings"

ready
retro 'Reviews: the whole diff' '' '| Found | In |' '| :-- | --: |' '| A name that misled | 3f2a9c1 |'
check 'a table with a separator that has colons, and a row' 0 "$ok"

ready
retro 'Reviews: the whole diff' '' '| Finding | Fixed in |' '|---|---|' '| | |' '| A name that misled | 3f2a9c1 |'
check 'an empty row, then a real one' 0 "$ok"

ready
retro 'Reviews: the whole diff' '' '| Found | In |' '| | |' '| A name that misled | 3f2a9c1 |'
check 'a line of pipes and spaces is no separator' 1 "$no_findings"

ready
pull 7 alice false $'## Retro   \n\nReviews: the whole diff\n\nNo findings\n'
check 'a "## Retro" heading with trailing spaces' 0 "$ok"

# Fenced code is no answer
ready
pull 7 alice false $'Closes #1\n\n```\n## Retro\n\nReviews: the whole diff\n\nNo findings\n```\n'
check 'a "## Retro" inside a fence is no section' 1 "$no_retro"

ready
pull 7 alice false "$(printf '%s\n' '## Retro' '' 'Example:' '' '~~~' '## Retro' 'No findings' '~~~' '' 'Reviews: the whole diff')"
check '"No findings" inside a fence does not count' 1 "$no_findings"

ready
files 7 scripts/check-delivery.sh
retro 'Reviews: the whole diff' 'No findings' '' '```' 'Second reader: Ana' '```'
check 'a Second reader line inside a fence does not count' 1 "$(no_reader scripts/check-delivery.sh)"

ready
files 7 scripts/check-delivery.sh
retro 'Reviews: the whole diff' 'No findings' '' '~~~~' 'Second reader: Ana' '~~~~' '' 'Second reader: Ana'
check 'a Second reader line after a fence counts' 0 "$ok"

ready
files 7 scripts/check-delivery.sh
retro 'Reviews: the whole diff' 'No findings' '' '````' '```' 'Second reader: Ana' '````'
check 'a shorter fence line does not close a longer fence' 1 "$(no_reader scripts/check-delivery.sh)"

ready
retro 'Reviews: the whole diff' '```' 'No findings'
check 'a fence left open runs to the end' 1 "$no_findings"

ready
ticks='```'
retro 'Reviews: the whole diff' "Inline ${ticks}code${ticks} is no fence" 'No findings'
check 'three backticks inline open no fence' 0 "$ok"

ready
retro 'Reviews: the whole diff' '<!--' '```' '-->' 'No findings'
check 'a fence marker inside a comment opens no fence' 0 "$ok"

ready
retro 'Reviews: the whole diff' '```' '<!--' '```' 'No findings'
check 'a comment marker inside a fence opens no comment' 0 "$ok"

# Only ASCII text fills a line
nbsp=$'\xc2\xa0'
zwsp=$'\xe2\x80\x8b'
ready
retro "Reviews:$nbsp" 'No findings'
check 'a Reviews line holding a no-break space' 1 "$no_reviews"

ready
retro "Reviews: $zwsp" 'No findings'
check 'a Reviews line holding a zero-width space' 1 "$no_reviews"

ready
files 7 scripts/check-delivery.sh
retro 'Reviews: the whole diff' "Second reader:$nbsp" 'No findings'
check 'a Second reader line holding a no-break space' 1 "$(no_reader scripts/check-delivery.sh)"

ready
files 7 scripts/check-delivery.sh
retro 'Reviews: the whole diff' "Second reader: $zwsp" 'No findings'
check 'a Second reader line holding a zero-width space' 1 "$(no_reader scripts/check-delivery.sh)"

ready
files 7 scripts/check-delivery.sh
retro 'Reviews: 2 passes' 'Second reader: 4eyes' 'No findings'
check 'digits are text' 0 "$ok"

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

ready
jq -n '[{filename: "plugin/a.sh", previous_filename: "scripts/a.sh"}]' | files_raw 7
check 'a protected path renamed out of its directory' 1 "$(no_reader scripts/a.sh)"

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
pull 3 'dependabot[bot]' false ''
commits 3 'dependabot[bot]'
open_prs 3:'dependabot[bot]' 7:alice
check 'a file shared with an older dependency update' 0 "$ok"

ready
files 3 plugin/a.cs
pull 3 'dependabot[bot]' false ''
commits 3 'dependabot[bot]' bob
open_prs 3:'dependabot[bot]' 7:alice
check 'a file shared with an older dependabot[bot] pull request that is a slice' 1 "$(shared plugin/a.cs 3)"

ready
files 3 plugin/other.cs
open_prs 3:bob 7:alice
check 'an older open pull request with no file in common' 0 "$ok"

ready
files 7 plugin/a.cs
jq -n '[{filename: "plugin/new.cs", previous_filename: "plugin/a.cs"}]' | files_raw 3
open_prs 3:bob 7:alice
check 'a file an older pull request renames' 1 "$(shared plugin/a.cs 3)"

ready
{ jq -n '[{filename: "plugin/x.cs"}]'; jq -n '[{filename: "plugin/a.cs"}]'; } | files_raw 3
open_prs 3:bob 7:alice
check 'a shared file on the second page of files' 1 "$(shared plugin/a.cs 3)"

ready
{ jq -n '[{filename: "plugin/x.cs"}]'; jq -n '[{filename: "plugin/a.cs"}]'; } | files_raw 7
files 3 plugin/a.cs
open_prs 3:bob 7:alice
check 'a file of this pull request on its second page of files' 1 "$(shared plugin/a.cs 3)"

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
set_pull 3 '.changed_files = 1'
check_error 'unreadable files of an older pull request' 2 'cannot list the files of pull request #3' 7

ready
files 3 plugin/other.cs
rm "$fix/pull-3.json"
open_prs 3:bob 7:alice
check_error 'an unreadable older pull request' 2 'cannot read pull request #3' 7

# A listing short of the pull request's file count is files unseen
ready
set_pull 7 '.changed_files = 3001'
check_error 'this pull request lists fewer files than it has' 2 'pull request #7 is too large to check' 7

ready
files 3 plugin/other.cs
set_pull 3 '.changed_files = 3001'
open_prs 3:bob 7:alice
check_error 'an older pull request lists fewer files than it has' 2 'pull request #3 is too large to check' 7

ready
set_pull 7 'del(.changed_files)'
check_error 'this pull request has no file count' 2 'pull request #7 has no file count' 7

ready
files 3 plugin/other.cs
set_pull 3 'del(.changed_files)'
open_prs 3:bob 7:alice
check_error 'an older pull request has no file count' 2 'pull request #3 has no file count' 7

ready
files 3 plugin/other.cs
set_pull 3 '.changed_files = "many"'
open_prs 3:bob 7:alice
check_error 'an older pull request has a file count that is no number' 2 'pull request #3 has no file count' 7

ready
set_pull 7 '.changed_files = 0'
check 'a listing longer than the count is not short of it' 0 "$ok"

# The workflow runs the check from the branch a pull request targets, so whatever
# defines the check must need a maintainer's approval to change.
for path in /.github/workflows/ /scripts/check-delivery.sh /scripts/test-delivery.sh /.github/CODEOWNERS; do
  verdict "CODEOWNERS gives $path to a maintainer" \
    "$(awk -v path="$path" '$1 == path && $2 ~ /^@/ { found = 1 } END { print found ? "true" : "false" }' "$root/.github/CODEOWNERS")"
done

if (( failures )); then
  echo "test-delivery: $failures failed" >&2
  exit 1
fi
echo "test-delivery: all passed"
