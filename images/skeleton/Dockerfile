# Stand-in that proves the release pipeline end to end: it serves the commit it
# was built from on 8456, which vhd apply publishes on the web map's port.
FROM busybox:1.38.0@sha256:a46257e06c82ad85242d0d38077d0a52d9896389ca60a1a59d3463db586e5fa4

ARG VHD_BUILD=dev
RUN mkdir -p /srv && echo "$VHD_BUILD" > /srv/build
COPY NOTICE /usr/share/doc/vhd/NOTICE

USER 65534:65534
EXPOSE 8456
HEALTHCHECK --interval=5s --timeout=2s --retries=3 --start-period=10s --start-interval=1s \
  CMD wget -qO /dev/null http://127.0.0.1:8456/build || exit 1
ENTRYPOINT ["httpd", "-f", "-p", "8456", "-h", "/srv"]
