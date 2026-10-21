#!/usr/bin/env bash
# Fail when a file in the repo carries something that must not be public:
# private addresses, internal host names, personal paths, email addresses,
# private links, or words the glossary retires. CI runs it on every push and
# pull request; run it before committing.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

self=scripts/check-public.sh
mapfile -t files < <(git ls-files --cached --others --exclude-standard | grep -vxF "$self")

# Each entry: what it catches, a pipe, then an extended regex.
checks=(
  'private IPv4 address|\b(10(\.[0-9]{1,3}){3}|192\.168(\.[0-9]{1,3}){2}|172\.(1[6-9]|2[0-9]|3[01])(\.[0-9]{1,3}){2}|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])(\.[0-9]{1,3}){2})\b'
  'internal host name|\b[A-Za-z0-9-]+\.(local|lan|internal|home\.arpa)\b'
  'personal path|(/home/|/Users/)[A-Za-z]'
  'email address|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
  'private link|claude\.ai/'
)

failed=0
report() {
  echo "check-public: $1:" >&2
  local line
  while IFS= read -r line; do echo "  $line"; done <<< "$2" >&2
  failed=1
}

for check in "${checks[@]}"; do
  what=${check%%|*} re=${check#*|}
  hits=$(grep -nHIoE -- "$re" "${files[@]}" || true)
  if [[ $what == 'email address' ]]; then
    hits=$(grep -v '@users\.noreply\.github\.com' <<< "$hits" || true)
  fi
  if [[ -n $hits ]]; then
    report "$what" "$hits"
  fi
done

# Docs and agent instructions use glossary terms; the glossary itself lists
# the retired words, so it is exempt.
mapfile -t prose < <(printf '%s\n' "${files[@]}" | grep -E '^(docs/.*\.(md|html)|[A-Z]+\.md)$' | grep -vxF docs/glossary.md)
if (( ${#prose[@]} )); then
  hits=$(grep -nHIoiwE 'sidecar|basemap|write path' "${prose[@]}" || true)
  if [[ -n $hits ]]; then
    report "retired word (see docs/glossary.md)" "$hits"
  fi
fi

exit "$failed"
