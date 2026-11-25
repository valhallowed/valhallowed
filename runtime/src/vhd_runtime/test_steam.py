import io
import sys
import tarfile
import time
from pathlib import Path
from typing import IO
from urllib.error import URLError

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
    with pytest.raises(steam.InstallFailed, match=r"steamcmd\.sh"):
        steam.ensure_steamcmd(directory, Download({"other": b""}))
    assert not directory.exists()


def test_an_interrupted_download_leaves_no_steamcmd(tmp_path: Path) -> None:
    directory = tmp_path / "steamcmd"
    with pytest.raises(steam.InstallFailed, match="download failed"):
        steam.ensure_steamcmd(directory, Download(STEAMCMD, cut=True))
    assert not (directory / "steamcmd.sh").exists()

    steam.ensure_steamcmd(directory, Download(STEAMCMD))
    assert (directory / "linux32" / "steamcmd").exists()
    assert not (tmp_path / "steamcmd.partial").exists()


def test_a_download_that_cannot_connect_fails_the_install(tmp_path: Path) -> None:
    def unreachable(url: str, timeout: float) -> Download:
        raise URLError("[Errno -3] Temporary failure in name resolution")

    with pytest.raises(steam.InstallFailed, match="name resolution"):
        steam.ensure_steamcmd(tmp_path / "steamcmd", unreachable)
    assert not (tmp_path / "steamcmd.partial").exists()


def install_manifest(install_dir: Path, flags: str = "4") -> None:
    (install_dir / "steamapps").mkdir(parents=True, exist_ok=True)
    (install_dir / "steamapps" / "appmanifest_896660.acf").write_text(MANIFEST.replace("{flags}", flags))


def test_an_installed_server_is_updated_without_validation(tmp_path: Path) -> None:
    install_manifest(tmp_path)
    calls: list[list[str]] = []
    assert steam.ensure_server(tmp_path / "steamcmd.sh", tmp_path, lambda c, t, s: calls.append(c)) == "25909431"
    assert len(calls) == 1
    assert "+app_update" in calls[0]
    assert "validate" not in calls[0]


def test_an_update_that_breaks_the_install_is_validated(tmp_path: Path) -> None:
    install_manifest(tmp_path)
    calls: list[list[str]] = []

    timeouts: list[float] = []

    def run(command: list[str], timeout: float, stopping: steam.Stopping) -> None:
        calls.append(command)
        timeouts.append(timeout)
        install_manifest(tmp_path, flags="1026" if len(calls) == 1 else "4")

    assert steam.ensure_server(Path("/s/steamcmd.sh"), tmp_path, run) == "25909431"
    assert ["validate" in c for c in calls] == [False, True]
    # An update of an installed server is bounded more tightly than the repair it may need.
    assert timeouts == [steam.UPDATE_TIMEOUT_S, steam.INSTALL_TIMEOUT_S]


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
    assert "validate" in command


def test_gives_up_after_the_last_attempt(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    with pytest.raises(steam.InstallFailed, match="did not install"):
        steam.ensure_server(Path("/s/steamcmd.sh"), tmp_path, lambda c, t, s: calls.append(c))
    assert len(calls) == steam.INSTALL_ATTEMPTS


def test_a_steamcmd_that_cannot_start_fails_the_install(tmp_path: Path) -> None:
    script = tmp_path / "steamcmd.sh"
    script.write_text("#!/bin/sh\n")
    with pytest.raises(steam.InstallFailed, match="could not start"):
        steam.run([str(script)], 5, lambda: False)


def test_a_stop_ends_steamcmd() -> None:
    started = time.monotonic()
    with pytest.raises(steam.Stopped):
        steam.run([sys.executable, "-c", "import time; time.sleep(60)"], 120, lambda: True)
    assert time.monotonic() - started < steam.TERMINATE_TIMEOUT_S


def test_a_stop_during_the_install_is_passed_on(tmp_path: Path) -> None:
    def run(command: list[str], timeout: float, stopping: steam.Stopping) -> None:
        raise steam.Stopped("stop requested while SteamCMD ran")

    with pytest.raises(steam.Stopped):
        steam.ensure_server(Path("/s/steamcmd.sh"), tmp_path, run)


# What SteamCMD printed for app_info_print 896660, cut to the parts the check reads past or reads.
APP_INFO = """steamcmd.sh[10]: Starting  /data/steamcmd/linux32/steamcmd
ILocalize::AddFile() failed to load file "public/steambootstrapper_english.txt".
Connecting anonymously to Steam Public...\x1b[0mOK
\x1b[0mAppID : 896660, change number : 41390420/41390420, last change : Tue Nov 24 15:51:06 2026
"896660"
{
\t"common"
\t{
\t\t"name"\t\t"Valheim Dedicated Server"
\t\t"associations"
\t\t{
\t\t}
\t}
\t"depots"
\t{
\t\t"896661"
\t\t{
\t\t\t"manifests"
\t\t\t{
\t\t\t\t"public"
\t\t\t\t{
\t\t\t\t\t"gid"\t\t"6965848522742639928"
\t\t\t\t}
\t\t\t}
\t\t}
\t\t"branches"
\t\t{
\t\t\t"public"
\t\t\t{
\t\t\t\t"buildid"\t\t"{build}"
\t\t\t\t"timeupdated"\t\t"1793193375"
\t\t\t}
\t\t\t"default_old"
\t\t\t{
\t\t\t\t"buildid"\t\t"25909431"
\t\t\t\t"description"\t\t"\tPrevious \\"stable\\""
\t\t\t}
\t\t}
\t\t"privatebranches"\t\t"1"
\t}
}
Unloading Steam API...\x1b[0mOK
\x1b[0m"""
NO_CONNECTION = """Connecting anonymously to Steam Public...\x1b[0mRetrying...
\x1b[0mRetrying...
\x1b[0mFAILED (No Connection)
\x1b[0mUnloading Steam API...\x1b[0mOK
"""


def answers(printed: str, asked: list[list[str]] | None = None) -> steam.Query:
    def query(command: list[str], timeout: float, stopping: steam.Stopping) -> str:
        if asked is not None:
            asked.append(command)
        return printed

    return query


def test_the_public_build_is_read_from_the_app_info_steamcmd_prints(tmp_path: Path) -> None:
    asked: list[list[str]] = []
    printed = APP_INFO.replace("{build}", "26266951")
    build = steam.public_build(Path("/s/steamcmd.sh"), tmp_path / "appinfo.vdf", answers(printed, asked))
    assert build == "26266951"
    assert asked == [
        ["/s/steamcmd.sh", "+login", "anonymous", "+app_info_update", "1", "+app_info_print", "896660", "+quit"]
    ]


def test_the_app_info_cache_is_removed_before_each_check(tmp_path: Path) -> None:
    cache = tmp_path / "Steam" / "appcache" / "appinfo.vdf"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"older app info")
    seen: list[bool] = []

    def query(command: list[str], timeout: float, stopping: steam.Stopping) -> str:
        seen.append(cache.exists())
        return APP_INFO.replace("{build}", "26266951")

    steam.public_build(Path("/s/steamcmd.sh"), cache, query)
    steam.public_build(Path("/s/steamcmd.sh"), cache, query)
    assert seen == [False, False]


def test_a_steamcmd_that_cannot_reach_steam_says_so(tmp_path: Path) -> None:
    with pytest.raises(steam.CheckFailed, match=r"^SteamCMD named no public game build: FAILED \(No Connection\)$"):
        steam.public_build(Path("/s/steamcmd.sh"), tmp_path / "appinfo.vdf", answers(NO_CONNECTION))


@pytest.mark.parametrize(
    "printed",
    [
        "",
        APP_INFO.replace("{build}", "26266951").split('\t\t"branches"')[0],
        APP_INFO.replace("{build}", "build 9"),
        APP_INFO.replace("{build}", "9" * 21),
        APP_INFO.replace("{build}", "26266951").replace('"public"\n\t\t\t{', '"beta"\n\t\t\t{'),
        APP_INFO.replace("{build}", "26266951").replace('"896660"\n{', '"896660"\n"common"'),
        APP_INFO.replace("{build}", "26266951").replace('"privatebranches"\t\t"1"', "{"),
    ],
    ids=["nothing", "cut-short", "not-digits", "too-long", "no-public-branch", "no-block", "brace-for-key"],
)
def test_app_info_without_a_public_build_fails_the_check(tmp_path: Path, printed: str) -> None:
    with pytest.raises(steam.CheckFailed, match=r"^SteamCMD named no public game build$"):
        steam.public_build(Path("/s/steamcmd.sh"), tmp_path / "appinfo.vdf", answers(printed))


def test_a_cache_that_cannot_be_removed_fails_the_check(tmp_path: Path) -> None:
    cache = tmp_path / "appinfo.vdf"
    cache.mkdir()
    with pytest.raises(steam.CheckFailed, match="cannot remove SteamCMD's app info cache"):
        steam.public_build(Path("/s/steamcmd.sh"), cache, answers(APP_INFO))


def test_a_stop_during_a_check_is_passed_on(tmp_path: Path) -> None:
    def query(command: list[str], timeout: float, stopping: steam.Stopping) -> str:
        raise steam.Stopped("stop requested while SteamCMD ran")

    with pytest.raises(steam.Stopped):
        steam.public_build(Path("/s/steamcmd.sh"), tmp_path / "appinfo.vdf", query)


def test_a_query_returns_what_steamcmd_printed_on_either_stream() -> None:
    script = "import sys; print('out'); print('err', file=sys.stderr)"
    assert sorted(steam.query([sys.executable, "-c", script], 30, lambda: False).split()) == ["err", "out"]


def test_a_query_reads_no_more_than_its_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(steam, "CHECK_OUTPUT_MAX", 10)
    assert steam.query([sys.executable, "-c", "print('x' * 100)"], 30, lambda: False) == "x" * 10


def test_a_query_that_does_not_answer_in_time_fails_the_check() -> None:
    with pytest.raises(steam.CheckFailed, match="did not answer within 1 s"):
        steam.query([sys.executable, "-c", "import time; time.sleep(60)"], 1, lambda: False)


def test_a_query_that_cannot_start_fails_the_check(tmp_path: Path) -> None:
    script = tmp_path / "steamcmd.sh"
    script.write_text("#!/bin/sh\n")
    with pytest.raises(steam.CheckFailed, match="could not start"):
        steam.query([str(script)], 5, lambda: False)


def test_a_stop_ends_a_query() -> None:
    started = time.monotonic()
    with pytest.raises(steam.Stopped):
        steam.query([sys.executable, "-c", "import time; time.sleep(60)"], 120, lambda: True)
    assert time.monotonic() - started < steam.TERMINATE_TIMEOUT_S


def test_an_installed_build_id_longer_than_the_contract_takes_is_no_install(tmp_path: Path) -> None:
    manifest = tmp_path / "steamapps" / "appmanifest_896660.acf"
    manifest.parent.mkdir()
    manifest.write_text(MANIFEST.replace("{flags}", "4").replace("25909431", "9" * 21))
    assert steam.server_build(tmp_path) is None
    manifest.write_text(MANIFEST.replace("{flags}", "4").replace("25909431", "9" * 20))
    assert steam.server_build(tmp_path) == "9" * 20
