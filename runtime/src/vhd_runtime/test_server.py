from dataclasses import replace
from pathlib import Path

from vhd_runtime import server
from vhd_runtime.generation import Generation

GENERATION = Generation(number=1, world="Meadowfold", port=2456, status_interval_s=5, status_url=None, password=None)


def make_pack(root: Path) -> Path:
    pack = root / "pack"
    (pack / "BepInEx" / "core").mkdir(parents=True)
    (pack / "BepInEx" / "core" / "BepInEx.Preloader.dll").write_bytes(b"MZ")
    (pack / "doorstop_libs").mkdir()
    (pack / "doorstop_libs" / "libdoorstop_x64.so").write_bytes(b"\x7fELF")
    return pack


def test_install_mods_lays_in_bepinex_the_plugin_and_its_config(tmp_path: Path) -> None:
    plugin = tmp_path / "Valhallowed.Plugin.dll"
    plugin.write_bytes(b"MZ")
    server_dir = tmp_path / "server"
    stray = server_dir / "BepInEx" / "plugins" / "Other" / "Other.dll"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"MZ")
    patcher = server_dir / "BepInEx" / "patchers" / "Other.Patcher.dll"
    patcher.parent.mkdir(parents=True)
    patcher.write_bytes(b"MZ")

    server.install_mods(make_pack(tmp_path), plugin, server_dir, Path("/run/vhd/world.json"), 7)

    assert (server_dir / "BepInEx" / "core" / "BepInEx.Preloader.dll").exists()
    assert (server_dir / "doorstop_libs" / "libdoorstop_x64.so").exists()
    assert (server_dir / "BepInEx" / "plugins" / "Valhallowed" / "Valhallowed.Plugin.dll").exists()
    assert not stray.exists()
    assert not patcher.exists()
    config = (server_dir / "BepInEx" / "config" / "com.valhallowed.plugin.cfg").read_text()
    assert config == "[Status]\nPath = /run/vhd/world.json\nIntervalSeconds = 7\n"


def test_command_keeps_the_world_unlisted() -> None:
    args = server.command(Path("/data/server"), Path("/data/saves"), GENERATION)
    assert args[0] == "/data/server/valheim_server.x86_64"
    assert args[args.index("-world") + 1] == "Meadowfold"
    assert args[args.index("-port") + 1] == "2456"
    assert args[args.index("-savedir") + 1] == "/data/saves"
    assert args[args.index("-public") + 1] == "0"
    assert "-password" not in args


def test_command_passes_a_password_only_when_set() -> None:
    generation = replace(GENERATION, password="hearth-and-horn")
    args = server.command(Path("/s"), Path("/v"), generation)
    assert args[args.index("-password") + 1] == "hearth-and-horn"


def test_environment_loads_bepinex_through_doorstop() -> None:
    env = server.environment(Path("/data/server"), {"PATH": "/usr/bin"})
    assert env["PATH"] == "/usr/bin"
    assert env["DOORSTOP_ENABLED"] == "1"
    assert env["DOORSTOP_TARGET_ASSEMBLY"] == "./BepInEx/core/BepInEx.Preloader.dll"
    assert env["LD_PRELOAD"] == "libdoorstop_x64.so"
    assert env["LD_LIBRARY_PATH"] == "/data/server/linux64:/data/server/doorstop_libs"
    assert env["SteamAppId"] == "892970"


def test_save_exists_in_either_format(tmp_path: Path) -> None:
    worlds = tmp_path / "worlds_local"
    worlds.mkdir()
    assert not server.save_exists(tmp_path, "Meadowfold")
    (worlds / "Meadowfold").mkdir()
    assert server.save_exists(tmp_path, "Meadowfold")
    (worlds / "Ashwake.db").write_bytes(b"")
    assert server.save_exists(tmp_path, "Ashwake")
