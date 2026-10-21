# Stand-in that proves the release pipeline end to end: it serves the commit it
# was built from on the web map's default port. Removed when the first real
# image lands.
FROM busybox:1.38.0@sha256:fd7dc98638c8e305f4dc34e979f1c0fdfdcaeb0fbf8fcff77ae834b6da3d7e6e

ARG VHD_BUILD=dev
RUN mkdir -p /srv && echo "$VHD_BUILD" > /srv/build

USER 65534:65534
EXPOSE 8456
HEALTHCHECK --interval=5s --timeout=2s --retries=3 \
  CMD wget -qO /dev/null http://127.0.0.1:8456/build || exit 1
ENTRYPOINT ["httpd", "-f", "-p", "8456", "-h", "/srv"]
