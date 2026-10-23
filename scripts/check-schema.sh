#!/usr/bin/env bash
# Check the schemas: the manifest schema in schema/ and the boundary contracts
# in contracts/. Every schema is valid JSON Schema, every manifest setting says
# what it costs and what applies it, every valid example passes, and every
# invalid example fails with the error it names: a TOML example on its first
# line ("# expect: <error>"), a JSON example in a sibling <example>.expect.
# .github/dependabot.yml is checked against Dependabot's schema too, since
# GitHub reads it only after a merge.
# Validation runs in a pinned Python image with the packages pinned in
# scripts/requirements-check-schema.txt, so Docker and jq are the only
# dependencies.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

image=$(scripts/tool-image.sh python)

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

# An example names its schema by starting with the schema's stem and a hyphen:
# manifest-one-world.toml is checked against manifest.schema.json.
cases=()
for dir in schema contracts; do
  for example in "$dir"/examples/valid/* "$dir"/examples/invalid/*; do
    [[ $example == *.toml || $example == *.json ]] || continue
    name=${example##*/}
    schema=
    for candidate in "$dir"/*.schema.json; do
      stem=${candidate##*/}
      stem=${stem%.schema.json}
      if [[ $name == "$stem"-* && ${#candidate} -gt ${#schema} ]]; then
        schema=$candidate
      fi
    done
    if [[ -z $schema ]]; then
      echo "check-schema: $example names no schema in $dir/" >&2
      failed=1
      continue
    fi
    expect=
    if [[ $example == */invalid/* ]]; then
      if [[ $example == *.toml ]]; then
        expect=$(sed -n '1s/^# expect: //p' "$example")
      elif [[ -f $example.expect ]]; then
        expect=$(<"$example.expect")
      fi
      if [[ -z $expect ]]; then
        echo "check-schema: $example does not name the error it must fail with" >&2
        failed=1
        continue
      fi
    fi
    cases+=("$schema" "$example" "$expect")
  done
done

docker run --rm -i -v "$PWD:/repo:ro" -w /repo "$image" sh -s "${cases[@]}" <<'EOF' || failed=1
set -eu
pip install --quiet --disable-pip-version-check --root-user-action=ignore --no-deps -r scripts/requirements-check-schema.txt
pip check --quiet 2>/dev/null || pip check
failed=0
check-jsonschema --check-metaschema schema/*.schema.json contracts/*.schema.json >/dev/null || failed=1
check-jsonschema --builtin-schema vendor.dependabot .github/dependabot.yml >/dev/null || failed=1
while [ $# -ge 3 ]; do
  schema=$1 example=$2 expect=$3
  shift 3
  if out=$(check-jsonschema --schemafile "$schema" "$example" 2>&1); then ok=1; else ok=0; fi
  if [ -z "$expect" ]; then
    if [ $ok = 0 ]; then echo "check-schema: $example fails:" >&2; echo "$out" >&2; failed=1; fi
  elif [ $ok = 1 ]; then
    echo "check-schema: $example passes; it must fail with: $expect" >&2; failed=1
  elif ! printf '%s\n' "$out" | grep -qF -- "$expect"; then
    echo "check-schema: $example fails, but not with: $expect" >&2; echo "$out" >&2; failed=1
  fi
done
exit $failed
EOF

if (( failed == 0 )); then
  echo "check-schema: schemas, contracts, examples and dependabot.yml agree"
fi
exit "$failed"
