#!/usr/bin/env bash
# Check one Python component (a directory with a pyproject.toml and a
# requirements-dev.txt of exact pins): ruff lint and format, mypy --strict and
# pytest, in a pinned Python image, so Docker is the only dependency.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

[[ $# -eq 1 && -f $1/pyproject.toml && -f $1/requirements-dev.txt ]] || {
  echo "usage: $0 COMPONENT_DIR" >&2
  exit 2
}
component=$1
image=python:3.13.17-slim@sha256:fe14fd57a4c0361110eaa0c284112b189750bcb43fb04bb5d1ee7b7744027ccf

docker run --rm -i -v "$PWD:/repo:ro" -w "/repo/$component" \
  -e PYTHONDONTWRITEBYTECODE=1 -e RUFF_CACHE_DIR=/tmp/ruff -e MYPY_CACHE_DIR=/tmp/mypy \
  "$image" sh -s <<'SCRIPT'
set -eu
pip install --quiet --disable-pip-version-check --root-user-action=ignore --no-deps -r requirements-dev.txt
pip check --quiet 2>/dev/null || pip check
ruff check src
ruff format --check src
mypy src
python -m pytest -q -p no:cacheprovider
SCRIPT
echo "check-python: $component passes"
