# Stand-in that proves the release pipeline end to end: it serves the commit it
# was built from on 8456, which vhd apply publishes on the web map's port.
FROM busybox:1.38.0@sha256:fd7dc98638c8e305f4dc34e979f1c0fdfdcaeb0fbf8fcff77ae834b6da3d7e6e

ARG VHD_BUILD=dev
RUN mkdir -p /srv && echo "$VHD_BUILD" > /srv/build
COPY NOTICE /usr/share/doc/vhd/NOTICE

USER 65534:65534
EXPOSE 8456
HEALTHCHECK --interval=5s --timeout=2s --retries=3 --start-period=10s --start-interval=1s \
  CMD wget -qO /dev/null http://127.0.0.1:8456/build || exit 1
ENTRYPOINT ["httpd", "-f", "-p", "8456", "-h", "/srv"]
