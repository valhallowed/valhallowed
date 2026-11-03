# The data layer's services, one container each (G14): the database, with
# PostgreSQL from the snapshot Debian of images/debian, so its major version
# follows Debian's; and intake. vhd apply runs each with its name as the command.

FROM vhd-debian AS data-layer
# Debian's PostgreSQL makes a cluster when it installs; the database service
# makes its own, in its volume.
RUN mkdir -p /etc/postgresql-common/createcluster.d \
 && echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.d/vhd.conf \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-17=17.11-0+deb13u1 \
      python3=3.13.5-1 \
      python3-jsonschema=4.19.2-6 \
      python3-psycopg=3.2.6-1 \
      tini=0.19.0-3+b8 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin vhd \
 && mkdir -p /opt/vhd/licenses /data /run/postgresql \
 && chown vhd:vhd /data /run/postgresql
COPY images/data-layer/NOTICE /opt/vhd/licenses/NOTICE
COPY contracts/*.schema.json /opt/vhd/contracts/
COPY schema/*.schema.json /opt/vhd/schema/
COPY data/src/vhd_data/ /opt/vhd/data/vhd_data/
ENV PYTHONPATH=/opt/vhd/data PYTHONDONTWRITEBYTECODE=1
# A new volume takes /data's owner from the image, so the service can write it.
USER vhd
ENTRYPOINT ["tini", "--", "python3", "-m", "vhd_data"]

# The data layer's tests, against the PostgreSQL and psycopg the image ships.
# Never published.
FROM data-layer AS test
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pytest=8.3.5-2 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=data-tests . /opt/vhd/data/vhd_data/
USER vhd
WORKDIR /opt/vhd/data
ENTRYPOINT ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "vhd_data"]
