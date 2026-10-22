# Builds the VHD plugin against the dedicated server's own assemblies and the
# pinned BepInEx pack. The server is downloaded in a build stage and discarded:
# the output holds the plugin and the game build it was compiled against, and
# no game file.

FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS game
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates=20250419+deb13u1 \
      lib32gcc-s1=14.2.0-19 \
 && rm -rf /var/lib/apt/lists/*
# SteamCMD updates itself on its first run, so the tarball has no version to pin.
ADD https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz /tmp/steamcmd.tar.gz
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# SteamCMD's exit code is unreliable on a first run, so success is the app
# manifest it writes, and a failed attempt is retried.
RUN mkdir /steamcmd && tar -xzf /tmp/steamcmd.tar.gz -C /steamcmd \
 && for attempt in 1 2 3; do \
      /steamcmd/steamcmd.sh +force_install_dir /server +login anonymous +app_update 896660 validate +quit || true; \
      [[ -f /server/steamapps/appmanifest_896660.acf ]] && break; \
      echo "steamcmd attempt $attempt failed" >&2; \
    done \
 && mkdir /refs \
 && cp /server/valheim_server_Data/Managed/{assembly_valheim,UnityEngine,UnityEngine.CoreModule}.dll /refs/ \
 && sed -n 's/^[[:space:]]*"buildid"[[:space:]]*"\([0-9]*\)".*/\1/p' /server/steamapps/appmanifest_896660.acf > /refs/game-build \
 && [[ -s /refs/game-build ]]

FROM busybox:1.38.0@sha256:fd7dc98638c8e305f4dc34e979f1c0fdfdcaeb0fbf8fcff77ae834b6da3d7e6e AS bepinex
ADD --checksum=sha256:bce631497976a93977ceb08e166712e6c31d15244956f89f17df092a9b62e29f \
    https://thunderstore.io/package/download/denikson/BepInExPack_Valheim/5.4.2351/ /tmp/bepinex.zip
RUN mkdir /bepinex && unzip -q /tmp/bepinex.zip -d /bepinex

FROM mcr.microsoft.com/dotnet/sdk:10.0@sha256:747cbade5bfd918a175ac32f8f1d931edec2c7d0d5397a3de72eaef1ac1f66d2 AS build
ARG VHD_BUILD=dev
WORKDIR /src
COPY plugin/ ./
COPY --from=game /refs/*.dll /refs/game/
COPY --from=bepinex /bepinex/BepInExPack_Valheim/BepInEx/core/BepInEx.dll /refs/bepinex/
RUN dotnet build Valhallowed.Plugin.csproj --nologo -c Release -o /out \
      -p:GameManaged=/refs/game -p:BepInExCore=/refs/bepinex -p:VhdBuild="$VHD_BUILD"

# The plugin may reference only these assemblies. Adding one is a change here.
FROM build AS check
COPY images/plugin/refcheck/ /refcheck/
RUN dotnet run --project /refcheck -c Release -- /out/Valhallowed.Plugin.dll \
      netstandard BepInEx assembly_valheim UnityEngine UnityEngine.CoreModule

FROM scratch AS plugin
COPY --from=check /out/Valhallowed.Plugin.dll /
COPY --from=game /refs/game-build /
