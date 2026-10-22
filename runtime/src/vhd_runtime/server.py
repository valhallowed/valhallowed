"""The dedicated server's files, launch and saves, with BepInEx and the plugin.

Everything the runtime lays into the server directory is rewritten on every
start, so it always matches the image: a generated file is never edited by hand.
"""

import shutil
from collections.abc import Mapping
from pathlib import Path

from vhd_runtime.generation import Generation

EXECUTABLE = "valheim_server.x86_64"
GAME_APP_ID = "892970"
PLUGIN_GUID = "com.valhallowed.plugin"
PLUGIN_FILE = "Valhallowed.Plugin.dll"


def install_mods(pack: Path, plugin: Path, server_dir: Path, status_file: Path, interval_s: int) -> None:
    """Lay BepInEx, the plugin and the plugin's config into the server directory."""
    bepinex = server_dir / "BepInEx"
    # Only VHD's plugin loads: a plugin left behind by hand would be a private
    # code path (G2).
    shutil.rmtree(bepinex / "plugins", ignore_errors=True)
    shutil.copytree(pack / "BepInEx", bepinex, dirs_exist_ok=True)
    doorstop = server_dir / "doorstop_libs"
    doorstop.mkdir(exist_ok=True)
    shutil.copy2(pack / "doorstop_libs" / "libdoorstop_x64.so", doorstop / "libdoorstop_x64.so")
    plugins = bepinex / "plugins" / "Valhallowed"
    plugins.mkdir(parents=True)
    shutil.copy2(plugin, plugins / PLUGIN_FILE)
    config = bepinex / "config" / f"{PLUGIN_GUID}.cfg"
    config.parent.mkdir(exist_ok=True)
    config.write_text(f"[Status]\nPath = {status_file}\nIntervalSeconds = {interval_s}\n")


def command(server_dir: Path, saves_dir: Path, generation: Generation) -> list[str]:
    """The dedicated server's command line. The world stays out of the server list."""
    args = [
        str(server_dir / EXECUTABLE),
        "-nographics",
        "-batchmode",
        "-name",
        generation.world,
        "-port",
        str(generation.port),
        "-world",
        generation.world,
        "-savedir",
        str(saves_dir),
        "-public",
        "0",
    ]
    if generation.password is not None:
        args += ["-password", generation.password]
    return args


def environment(server_dir: Path, base: Mapping[str, str]) -> dict[str, str]:
    """The environment that loads BepInEx through doorstop, as the BepInEx pack's own script sets it."""
    env = dict(base)
    env.update(
        DOORSTOP_ENABLED="1",
        DOORSTOP_TARGET_ASSEMBLY="./BepInEx/core/BepInEx.Preloader.dll",
        LD_LIBRARY_PATH=f"{server_dir / 'linux64'}:{server_dir / 'doorstop_libs'}",
        LD_PRELOAD="libdoorstop_x64.so",
        SteamAppId=GAME_APP_ID,
    )
    return env


def save_exists(saves_dir: Path, world: str) -> bool:
    """Whether the world has a save: the 1.0+ chunked directory or the pre-1.0 single file."""
    worlds = saves_dir / "worlds_local"
    return (worlds / world).is_dir() or (worlds / f"{world}.db").is_file()
