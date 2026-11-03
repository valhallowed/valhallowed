"""The dedicated server's files, launch and saves, with BepInEx and the plugin.

Everything the runtime lays into the server directory is rewritten on every
start, so it always matches the image: a generated file is never edited by hand.
"""

import shutil
from collections.abc import Mapping
from pathlib import Path

from vhd_runtime.generation import Generation

# The plugin's config (B12): contracts/plugin.config.schema.json states every key.
PluginConfig = Mapping[str, Mapping[str, str | int]]

EXECUTABLE = "valheim_server.x86_64"
GAME_APP_ID = "892970"
PLUGIN_GUID = "com.valhallowed.plugin"
PLUGIN_FILE = "Valhallowed.Plugin.dll"


def plugin_config(generation: Generation, status_file: Path) -> PluginConfig:
    """The plugin's config for a generation, as the B12 contract states its keys."""
    return {
        "Status": {"Path": str(status_file), "IntervalSeconds": generation.status_interval_s},
        "Intake": {"Url": generation.intake_url},
    }


def render_config(config: PluginConfig) -> str:
    """The config as BepInEx reads it: a [Section] each, then one Key = value line per key."""
    return "\n".join(
        f"[{section}]\n" + "".join(f"{key} = {value}\n" for key, value in keys.items())
        for section, keys in config.items()
    )


def install_mods(pack: Path, plugin: Path, server_dir: Path, config: PluginConfig) -> None:
    """Lay BepInEx, the plugin and the plugin's config into the server directory."""
    bepinex = server_dir / "BepInEx"
    # Only VHD's code loads: a plugin or preloader patcher left behind by hand
    # would be a private code path (G2).
    shutil.rmtree(bepinex, ignore_errors=True)
    shutil.copytree(pack / "BepInEx", bepinex)
    doorstop = server_dir / "doorstop_libs"
    doorstop.mkdir(exist_ok=True)
    shutil.copy2(pack / "doorstop_libs" / "libdoorstop_x64.so", doorstop / "libdoorstop_x64.so")
    plugins = bepinex / "plugins" / "Valhallowed"
    plugins.mkdir(parents=True)
    shutil.copy2(plugin, plugins / PLUGIN_FILE)
    path = bepinex / "config" / f"{PLUGIN_GUID}.cfg"
    path.parent.mkdir(exist_ok=True)
    path.write_text(render_config(config))


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
