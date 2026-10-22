#!/usr/bin/env bash
# Check the manifest schema: both schemas are valid JSON Schema, every setting
# says what it costs and what applies it, every valid example passes, and every
# invalid example fails with the error its "# expect:" line names. Validation
# runs in a pinned Python image, so Docker and jq are the only dependencies.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

image=python:3.13.17-slim@sha256:fe14fd57a4c0361110eaa0c284112b189750bcb43fb04bb5d1ee7b7744027ccf
checker=check-jsonschema==0.38.2

failed=0
for s in schema/*.schema.json; do
  bad=$(jq -r '
    [.. | objects | select(has("properties")) | .properties | to_entries[]
      | select(.key != "schema" and (.value | has("type")) and .value.type != "object")
      | select((.value | has("description") | not)
          or (.value["x-change-cost"] | IN("live", "service restart", "game-server restart") | not)
          or (.value["x-applied-by"] | IN("control plane", "vhd apply") | not))
      | .key] | unique | .[]' "$s")
  if [[ -n $bad ]]; then
    echo "check-schema: $s: a setting lacks description, x-change-cost or x-applied-by: $bad" >&2
    failed=1
  fi
done

docker run --rm -i -v "$PWD/schema:/schema:ro" "$image" sh -s "$checker" <<'EOF' || failed=1
set -eu
pip install --quiet --disable-pip-version-check --root-user-action=ignore "$1"
failed=0
check-jsonschema --check-metaschema /schema/*.schema.json >/dev/null || failed=1
for f in /schema/examples/valid/*.toml /schema/examples/invalid/*.toml; do
  name=${f#/schema/examples/}
  case ${f##*/} in
    manifest-*) schema=/schema/manifest.schema.json ;;
    secrets-*) schema=/schema/secrets.schema.json ;;
    *) echo "check-schema: $name: name must start with manifest- or secrets-" >&2; failed=1; continue ;;
  esac
  if out=$(check-jsonschema --schemafile "$schema" "$f" 2>&1); then ok=1; else ok=0; fi
  case $name in
    valid/*)
      if [ $ok = 0 ]; then echo "check-schema: $name fails:" >&2; echo "$out" >&2; failed=1; fi ;;
    invalid/*)
      expect=$(sed -n '1s/^# expect: //p' "$f")
      if [ -z "$expect" ]; then
        echo "check-schema: $name: first line must be '# expect: <error>'" >&2; failed=1
      elif [ $ok = 1 ]; then
        echo "check-schema: $name passes; it must fail with: $expect" >&2; failed=1
      elif ! printf '%s\n' "$out" | grep -qF -- "$expect"; then
        echo "check-schema: $name fails, but not with: $expect" >&2; echo "$out" >&2; failed=1
      fi ;;
  esac
done
exit $failed
EOF

if (( failed == 0 )); then
  echo "check-schema: schemas and examples agree"
fi
exit "$failed"
