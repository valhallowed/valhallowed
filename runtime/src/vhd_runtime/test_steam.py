import io
import tarfile
from pathlib import Path
from typing import IO

import pytest

from vhd_runtime import steam

MANIFEST = '"AppState"\n{\n\t"appid"\t\t"896660"\n\t"buildid"\t\t"25909431"\n}\n'


class Download:
    def __init__(self, members: dict[str, bytes]) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(data))
        self._bytes = buffer.getvalue()
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float) -> "Download":
        self.urls.append(url)
        return self

    def __enter__(self) -> IO[bytes]:
        return io.BytesIO(self._bytes)

    def __exit__(self, *args: object) -> None:
        pass


def test_downloads_steamcmd_once(tmp_path: Path) -> None:
    download = Download({"steamcmd.sh": b"#!/bin/sh\n", "linux32/steamcmd": b"\x7fELF"})
    script = steam.ensure_steamcmd(tmp_path, download)
    assert script == tmp_path / "steamcmd.sh"
    assert (tmp_path / "linux32" / "steamcmd").exists()
    steam.ensure_steamcmd(tmp_path, download)
    assert download.urls == [steam.STEAMCMD_URL]


def test_refuses_a_download_without_steamcmd(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"steamcmd\.sh"):
        steam.ensure_steamcmd(tmp_path, Download({"other": b""}))


def install_manifest(install_dir: Path) -> None:
    (install_dir / "steamapps").mkdir(parents=True)
    (install_dir / "steamapps" / "appmanifest_896660.acf").write_text(MANIFEST)


def test_an_installed_server_is_kept(tmp_path: Path) -> None:
    install_manifest(tmp_path)
    calls: list[list[str]] = []
    assert steam.ensure_server(tmp_path / "steamcmd.sh", tmp_path, lambda c, t: calls.append(c)) == "25909431"
    assert calls == []


def test_installs_and_retries_until_the_manifest_exists(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], timeout: float) -> None:
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
        steam.ensure_server(Path("/s/steamcmd.sh"), tmp_path, lambda c, t: calls.append(c))
    assert len(calls) == steam.INSTALL_ATTEMPTS
