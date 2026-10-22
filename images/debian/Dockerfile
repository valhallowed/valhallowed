# Debian as it was when the pinned base image was built, for every VHD image built
# on Debian (docker-bake.hcl hands it to them as the "debian" context). The image
# names that snapshot in its own sources, so every package an image pins stays
# installable, and nothing an image installs moves until this base image does. A
# snapshot's Release files expire; their signatures are still checked.
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
RUN sources=/etc/apt/sources.list.d/debian.sources \
 && sed -i -e '/^URIs: /d' -e 's|^# \(http://snapshot\.debian\.org/archive/.*\)$|URIs: \1|' "$sources" \
 && [ "$(grep -c '^URIs: http://snapshot\.debian\.org/' "$sources")" -eq 2 ] \
 && printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/80snapshot
