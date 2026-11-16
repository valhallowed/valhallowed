# The images the repo's checks and acceptance run in, each pinned by digest in
# this one place.
# Nothing builds this file: Dependabot keeps the pins current, and
# scripts/tool-image.sh hands each to the check that runs it.
FROM python:3.13.18-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS python
FROM rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667 AS actionlint
FROM koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d AS shellcheck
# Acceptance's browser: Chrome's headless shell, on its stable channel, which
# its version tags do not tell from the dev channel's.
FROM chromedp/headless-shell:stable@sha256:8c574e39f0db5c1d9efe7c472c6e0fcfc1d91ac0be70eb1dead0f2d52cf104cc AS headless-shell
