# The images the repo's checks run in, each pinned by digest in this one place.
# Nothing builds this file: Dependabot keeps the pins current, and
# scripts/tool-image.sh hands each to the check that runs it.
FROM python:3.13.17-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS python
FROM rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667 AS actionlint
FROM koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d AS shellcheck
