import io
import sys
import tarfile
import time
from pathlib import Path
from typing import IO

import pytest

from vhd_runtime import steam

MANIFEST = '"AppState"\n{\n\t"appid"\t\t"896660"\n\t"StateFlags"\t\t"{flags}"\n\t"buildid"\t\t"25909431"\n}\n'
STEAMCMD = {"steamcmd.sh": b"#!/bin/sh\n", "linux32/steamcmd": b"\x7fELF"}


class Download:
    def __init__(self, members: dict[str, bytes], cut: bool = False) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(data))
        self._bytes = buffer.getvalue()
        if cut:
            # The connection drops partway: the archive ends after its first member.
            self._bytes = self._bytes[: len(self._bytes) // 2]
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float) -> "Download":
        self.urls.append(url)
        return self

    def __enter__(self) -> IO[bytes]:
        return io.BytesIO(self._bytes)

    def __exit__(self, *args: object) -> None:
        pass


def test_downloads_steamcmd_once(tmp_path: Path) -> None:
    directory = tmp_path / "steamcmd"
    download = Download(STEAMCMD)
    script = steam.ensure_steamcmd(directory, download)
    assert script == directory / "steamcmd.sh"
    assert (directory / "linux32" / "steamcmd").exists()
    steam.ensure_steamcmd(directory, download)
    assert download.urls == [steam.STEAMCMD_URL]


def test_refuses_a_download_without_steamcmd(tmp_path: Path) -> None:
    directory = tmp_path / "steamcmd"
    with pytest.raises(RuntimeError, match=r"steamcmd\.sh"):
        steam.ensure_steamcmd(directory, Download({"other": b""}))
    assert not directory.exists()


def test_an_interrupted_download_leaves_no_steamcmd(tmp_path: Path) -> None:
    directory = tmp_path / "steamcmd"
    with pytest.raises((EOFError, tarfile.TarError)):
        steam.ensure_steamcmd(directory, Download(STEAMCMD, cut=True))
    assert not (directory / "steamcmd.sh").exists()

    steam.ensure_steamcmd(directory, Download(STEAMCMD))
    assert (directory / "linux32" / "steamcmd").exists()
    assert not (tmp_path / "steamcmd.partial").exists()


def install_manifest(install_dir: Path, flags: str = "4") -> None:
    (install_dir / "steamapps").mkdir(parents=True, exist_ok=True)
    (install_dir / "steamapps" / "appmanifest_896660.acf").write_text(MANIFEST.replace("{flags}", flags))


def test_an_installed_server_is_kept(tmp_path: Path) -> None:
    install_manifest(tmp_path)
    calls: list[list[str]] = []
    assert steam.ensure_server(tmp_path / "steamcmd.sh", tmp_path, lambda c, t, s: calls.append(c)) == "25909431"
    assert calls == []


def test_a_server_not_fully_installed_is_installed_again(tmp_path: Path) -> None:
    install_manifest(tmp_path, flags="1026")
    calls: list[list[str]] = []

    def run(command: list[str], timeout: float, stopping: steam.Stopping) -> None:
        calls.append(command)
        install_manifest(tmp_path)

    assert steam.ensure_server(Path("/s/steamcmd.sh"), tmp_path, run) == "25909431"
    assert len(calls) == 1


def test_installs_and_retries_until_the_manifest_exists(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], timeout: float, stopping: steam.Stopping) -> None:
        calls.append(command)
        if len(calls) == 2:
            install_manifest(tmp_path)

    assert steam.ensure_server(Path("/s/steamcmd.sh"), tmp_path, run) == "25909431"
    assert len(calls) == 2
    command = calls[0]
    assert command.index("+force_install_dir") < command.index("+login")
    assert command[command.index("+login") + 1] == "anonymous"
    assert command[command.index("+app_update") + 1] == "896660"


def test_gives_up_after_the_last_attempt(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    with pytest.raises(RuntimeError, match="did not install"):
        steam.ensure_server(Path("/s/steamcmd.sh"), tmp_path, lambda c, t, s: calls.append(c))
    assert len(calls) == steam.INSTALL_ATTEMPTS


def test_a_stop_ends_steamcmd(tmp_path: Path) -> None:
    started = time.monotonic()
    with pytest.raises(steam.Stopped):
        steam.run([sys.executable, "-c", "import time; time.sleep(60)"], 120, lambda: True)
    assert time.monotonic() - started < steam.TERMINATE_TIMEOUT_S


def test_a_stop_during_the_install_is_passed_on(tmp_path: Path) -> None:
    def run(command: list[str], timeout: float, stopping: steam.Stopping) -> None:
        raise steam.Stopped("stop requested while SteamCMD ran")

    with pytest.raises(steam.Stopped):
        steam.ensure_server(Path("/s/steamcmd.sh"), tmp_path, run)
