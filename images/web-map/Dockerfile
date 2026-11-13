# The web map: a TypeScript service (G19) on Node.js, on the snapshot Debian of
# images/debian. The build stage compiles and tests it; the image carries the
# compiled service, its production dependencies (Leaflet among them, which it
# serves itself, never from a CDN), its page and the page's compiled script, the
# contracts it reads and the design system it is styled from.

# Named once here; its checksums change with it, and scripts/check-upstream.sh
# reports a newer LTS release.
ARG NODE_VERSION=26.13.1

# Node links libatomic, which the base leaves out.
FROM vhd-debian AS node
ARG NODE_VERSION
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1=14.2.0-19 \
 && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:518674de2b621728a5e4486f5fb3b7d2900bb3cc4aa97969edaa463db99f9778 \
    https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz /tmp/node.tar.gz
RUN mkdir /node && tar -xzf /tmp/node.tar.gz -C /node --strip-components=1 && rm /tmp/node.tar.gz

# npm fetches over HTTPS, which needs the CA certificates the base leaves out.
# A failing test fails the build.
FROM node AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates=20250419+deb13u1 \
 && rm -rf /var/lib/apt/lists/*
ENV PATH=/node/bin:$PATH
WORKDIR /src/web-map
COPY web-map/package.json web-map/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY web-map/tsconfig.json web-map/tsconfig.page.json ./
COPY web-map/src/ src/
COPY web-map/public/ public/
COPY contracts/*.schema.json /src/contracts/
COPY design/tokens.css /src/design/
COPY design/fonts/ /src/design/fonts/
RUN npm run build && npm test && npm prune --omit=dev \
 && find node_modules -mindepth 1 -type d -empty -delete

FROM vhd-debian AS web-map
ARG NODE_VERSION
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1=14.2.0-19 tini=0.19.0-3+b8 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin vhd \
 && mkdir -p /opt/vhd/licenses
COPY --from=node /node/bin/node /usr/local/bin/node
# Node's license, which carries the licenses of what it bundles. The RUN above
# makes its directory: ADD would give one it creates the file's mode, which no
# one could traverse.
ADD --checksum=sha256:8e603aa87c70d36e497004f0dec63111511cb61b7d65ace12e10ea791599286c --chmod=0644 \
    https://raw.githubusercontent.com/nodejs/node/v${NODE_VERSION}/LICENSE /opt/vhd/licenses/node-LICENSE
COPY images/web-map/NOTICE /opt/vhd/licenses/NOTICE
COPY design/fonts/OFL-*.txt /opt/vhd/licenses/
COPY web-map/package.json /opt/vhd/web-map/
COPY --from=build /src/web-map/node_modules/ /opt/vhd/web-map/node_modules/
COPY --from=build /src/web-map/dist/server/ /opt/vhd/web-map/server/
COPY --from=build /src/web-map/dist/page/ /opt/vhd/web-map/page/
COPY web-map/public/ /opt/vhd/web-map/public/
COPY contracts/*.schema.json /opt/vhd/contracts/
COPY design/tokens.css /opt/vhd/design/
COPY design/fonts/*.woff2 /opt/vhd/design/fonts/
ARG VHD_BUILD=dev
ENV VHD_BUILD=$VHD_BUILD
USER vhd
EXPOSE 8456
# compose up --wait waits for it, so vhd apply returns once the web map serves.
HEALTHCHECK --interval=5s --timeout=2s --retries=3 --start-period=10s --start-interval=1s \
  CMD ["node", "-e", "fetch('http://127.0.0.1:8456/build').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
ENTRYPOINT ["tini", "--", "node", "/opt/vhd/web-map/server/main.js"]
