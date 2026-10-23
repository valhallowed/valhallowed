# The control plane and vhd, its CLI: the Python package on the snapshot Debian
# of images/debian, with the docker CLI and its Compose plugin. Only vhd apply
# runs them, as the one container with Docker's socket, for its run alone (G13).
# vhd apply runs this Compose, not the host's, so every install renders and
# starts the same way.

# Each version is named once here; its checksums change with it, and
# scripts/check-upstream.sh reports a newer release.
ARG DOCKER_VERSION=29.9.2
ARG COMPOSE_VERSION=5.6.1

FROM vhd-debian AS docker
ARG DOCKER_VERSION
ADD --checksum=sha256:9ac225999fd5e863f7694c728ef23d480616b1e5639328fa309befd4243d69eb \
    https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz /tmp/docker.tgz
RUN tar -xzf /tmp/docker.tgz -C /tmp docker/docker

FROM vhd-debian AS control-plane
ARG DOCKER_VERSION
ARG COMPOSE_VERSION
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3=3.13.5-1 \
      python3-jsonschema=4.19.2-6 \
      tini=0.19.0-3+b8 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin vhd
COPY --from=docker /tmp/docker/docker /usr/local/bin/docker
ADD --checksum=sha256:47737f4236e6d3ccea7291995203f8f018058783b4c3a9469119811a0b5db8c0 --chmod=0755 \
    https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-x86_64 \
    /usr/local/libexec/docker/cli-plugins/docker-compose
ADD --checksum=sha256:2d81ea060825006fc8f3fe28aa5dc0ffeb80faf325b612c955229157b8c10dc0 \
    https://raw.githubusercontent.com/docker/cli/v${DOCKER_VERSION}/LICENSE /opt/vhd/licenses/docker-cli-LICENSE
ADD --checksum=sha256:a8c869fbda819afb8d80e0ac19bac52e766bc6c19cb38cf94f52d64c4be2aab6 \
    https://raw.githubusercontent.com/docker/cli/v${DOCKER_VERSION}/NOTICE /opt/vhd/licenses/docker-cli-NOTICE
ADD --checksum=sha256:58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd \
    https://raw.githubusercontent.com/docker/compose/v${COMPOSE_VERSION}/LICENSE /opt/vhd/licenses/docker-compose-LICENSE
COPY images/control-plane/NOTICE /opt/vhd/licenses/NOTICE
COPY contracts/*.schema.json /opt/vhd/contracts/
COPY schema/*.schema.json /opt/vhd/schema/
COPY control/src/vhd_control/ /opt/vhd/control/vhd_control/
ENV PYTHONPATH=/opt/vhd/control PYTHONDONTWRITEBYTECODE=1
# vhd apply renders who the control plane runs as; vhd itself runs as root for the socket.
USER vhd
ENTRYPOINT ["tini", "--", "python3", "-m", "vhd_control"]
