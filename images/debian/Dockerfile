# Debian as it was when the pinned base image was built, for every VHD image built
# on Debian (docker-bake.hcl hands it to them as the "vhd-debian" context). The image
# names that snapshot in its own sources, so every package an image pins stays
# installable, and nothing an image installs moves until this base image does. A
# snapshot's Release files expire; their signatures are still checked.
FROM debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4
RUN sources=/etc/apt/sources.list.d/debian.sources \
 && sed -i -e '/^URIs: /d' -e 's|^# \(http://snapshot\.debian\.org/archive/.*\)$|URIs: \1|' "$sources" \
 && [ "$(grep -c '^URIs: http://snapshot\.debian\.org/' "$sources")" -eq 2 ] \
 && printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/80snapshot
