# The game runtime: the supervisor, BepInEx and the plugin on a pinned base
# (G16), the snapshot Debian of images/debian. The plugin is built here against
# the dedicated server's own assemblies, downloaded in a build stage and
# discarded. SteamCMD and the server download into /data at first start, so no
# Valve or Iron Gate file is in any image.

# SteamCMD, for the plugin build and for scripts/game-build.sh, which asks Steam
# for the dedicated server's current build.
FROM debian AS steamcmd
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates=20250419+deb13u1 \
      lib32gcc-s1=14.2.0-19 \
 && rm -rf /var/lib/apt/lists/*
# SteamCMD updates itself on its first run, so the tarball has no version to pin.
ADD https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz /tmp/steamcmd.tar.gz
RUN mkdir /steamcmd && tar -xzf /tmp/steamcmd.tar.gz -C /steamcmd && rm /tmp/steamcmd.tar.gz

# The dedicated server build GAME_BUILD names, downloaded to take its assemblies
# and discarded in the same step: the layer holds only the assemblies and the
# build id, and is reused exactly while the build is unchanged. SteamCMD's exit
# code is unreliable on a first run, so success is the app manifest it writes,
# marked fully installed, and a failed attempt is retried.
FROM steamcmd AS game
ARG GAME_BUILD
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN if [[ ! $GAME_BUILD =~ ^[0-9]+$ ]]; then \
      echo "GAME_BUILD must be the dedicated server's build id; scripts/game-build.sh prints it" >&2; exit 1; \
    fi \
 && manifest=/server/steamapps/appmanifest_896660.acf \
 && installed() { grep -qE '^[[:space:]]*"StateFlags"[[:space:]]+"4"' "$manifest" 2>/dev/null; } \
 && for attempt in 1 2 3; do \
      /steamcmd/steamcmd.sh +force_install_dir /server +login anonymous +app_update 896660 validate +quit || true; \
      installed && break; \
      echo "steamcmd attempt $attempt failed" >&2; \
    done \
 && installed \
 && mkdir /refs \
 && cp /server/valheim_server_Data/Managed/{assembly_valheim,UnityEngine,UnityEngine.CoreModule}.dll /refs/ \
 && sed -n 's/^[[:space:]]*"buildid"[[:space:]]*"\([0-9]*\)".*/\1/p' "$manifest" > /refs/game-build \
 && if [[ $(</refs/game-build) != "$GAME_BUILD" ]]; then \
      echo "Steam served build $(</refs/game-build), not GAME_BUILD $GAME_BUILD; build again" >&2; exit 1; \
    fi \
 && rm -rf /server /steamcmd /root/Steam /root/.steam /root/.local/share/Steam

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
COPY images/game-runtime/refcheck/ /refcheck/
RUN dotnet run --project /refcheck -c Release -- /out/Valhallowed.Plugin.dll \
      netstandard BepInEx assembly_valheim UnityEngine UnityEngine.CoreModule

# The plugin and the game build it was compiled against: the bake target
# "plugin" exports these files, and the runtime below carries the DLL.
FROM scratch AS plugin
COPY --from=check /out/Valhallowed.Plugin.dll /
COPY --from=game /refs/game-build /

FROM debian AS runtime
# lib32gcc-s1 for SteamCMD, which is 32-bit; libatomic1 and the PulseAudio
# libraries for the server's PlayFab library, which it loads at start; tini reaps
# the processes SteamCMD leaves behind; Python and jsonschema for the supervisor.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates=20250419+deb13u1 \
      lib32gcc-s1=14.2.0-19 \
      libatomic1=14.2.0-19 \
      libpulse0=17.0+dfsg1-2+b1 \
      libpulse-mainloop-glib0=17.0+dfsg1-2+b1 \
      python3=3.13.5-1 \
      python3-jsonschema=4.19.2-6 \
      tini=0.19.0-3+b8 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --user-group --home-dir /data --shell /usr/sbin/nologin vhd \
 && mkdir -p /data /run/vhd \
 && chown vhd:vhd /data /run/vhd
COPY --from=bepinex /bepinex/BepInExPack_Valheim/BepInEx/ /opt/vhd/bepinex/BepInEx/
COPY --from=bepinex /bepinex/BepInExPack_Valheim/doorstop_libs/libdoorstop_x64.so /opt/vhd/bepinex/doorstop_libs/
ADD --checksum=sha256:e6e534ef6f4347b6449407ee046a3d09cb0174c6f688c996ad0bed94b74b3933 \
    https://raw.githubusercontent.com/AzumattDev/BepInEx/fd72254e7debd1b8b7289c1639a8a231c49e403f/LICENSE \
    /opt/vhd/licenses/BepInEx-LICENSE
ADD --checksum=sha256:7e54ab4766ef78637a05fb3454663a66de6bb960dbd4c7088890ed76b0844f36 \
    https://raw.githubusercontent.com/BepInEx/HarmonyX/217cc408cc3909ee67c727a2d782bede84f6c2b1/LICENSE \
    /opt/vhd/licenses/HarmonyX-LICENSE
ADD --checksum=sha256:0b64833222e3c6a426ad29362d26334619fe02eaf2b89ea9c37805521896c920 \
    https://raw.githubusercontent.com/MonoMod/MonoMod/164dd5bcb54675e2d4263785d758995cd0c2ad87/LICENSE \
    /opt/vhd/licenses/MonoMod-LICENSE
ADD --checksum=sha256:13d58ecbed676511c0c4c4009bff107025e50007ff5e7901dc562700ec62509e \
    https://raw.githubusercontent.com/jbevain/cecil/882ca5eedda1e62eb41bd5869aeb15d8f1538e51/LICENSE.txt \
    /opt/vhd/licenses/Mono.Cecil-LICENSE
ADD --checksum=sha256:20c17d8b8c48a600800dfd14f95d5cb9ff47066a9641ddeab48dc54aec96e331 \
    https://raw.githubusercontent.com/NeighTools/UnityDoorstop/v4.4.0/LICENSE \
    /opt/vhd/licenses/UnityDoorstop-LICENSE
COPY images/game-runtime/NOTICE /opt/vhd/licenses/NOTICE
COPY --from=check /out/Valhallowed.Plugin.dll /opt/vhd/plugin/
COPY contracts/*.schema.json /opt/vhd/contracts/
COPY schema/*.schema.json /opt/vhd/schema/
COPY runtime/src/vhd_runtime/ /opt/vhd/runtime/vhd_runtime/
ENV PYTHONPATH=/opt/vhd/runtime PYTHONDONTWRITEBYTECODE=1
USER vhd
WORKDIR /data
EXPOSE 2456-2458/udp
ENTRYPOINT ["tini", "--", "python3", "-m", "vhd_runtime"]
