"""SteamCMD and the dedicated server, downloaded into the install at first start (G16).

Every start also updates the server: the default until the control plane sets an
update policy (Game runtimes). An update that breaks loading is what world truth
holds.
"""

import logging
import os
import re
import shutil
import signal
import subprocess
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Protocol, cast
from urllib.request import urlopen

STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
SERVER_APP_ID = 896660
INSTALL_ATTEMPTS = 3
DOWNLOAD_TIMEOUT_S = 60
INSTALL_TIMEOUT_S = 3600
# An update of an installed server is a delta. Bounded more tightly, a Steam that hangs delays
# the start by this much; an update it cuts short leaves the install to be repaired, which
# gets the install's bound.
UPDATE_TIMEOUT_S = 900
TERMINATE_TIMEOUT_S = 10
POLL_S = 1.0
# Steam's mark for a complete install; any other value is treated as not installed.
FULLY_INSTALLED = "4"

_log = logging.getLogger(__name__)


class Stopped(Exception):
    """A stop was requested while SteamCMD ran."""


class Response(Protocol):
    def __enter__(self) -> IO[bytes]: ...
    def __exit__(self, *args: object) -> None: ...


Fetch = Callable[[str, float], Response]
Stopping = Callable[[], bool]
Run = Callable[[list[str], float, Stopping], None]


def fetch(url: str, timeout: float) -> Response:
    return cast(Response, urlopen(url, timeout=timeout))  # noqa: S310 - a fixed https URL


def run(command: list[str], timeout: float, stopping: Stopping) -> None:
    """Run SteamCMD until it exits, the timeout passes or a stop is requested. Its exit code
    is unreliable on a first run, so the caller checks the files it wrote instead."""
    # A session of its own, so a stop reaches the SteamCMD binary the script starts.
    process = subprocess.Popen(command, start_new_session=True)  # noqa: S603
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if stopping():
            _end(process)
            raise Stopped("stop requested while SteamCMD ran")
        if time.monotonic() > deadline:
            _log.error("SteamCMD did not finish within %d s", timeout)
            _end(process)
            return
        time.sleep(POLL_S)


def _end(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=TERMINATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        process.wait()


def ensure_steamcmd(directory: Path, fetch: Fetch = fetch) -> Path:
    """SteamCMD in directory, downloaded from Valve on the first start. The archive is
    unpacked beside the directory and moved into place whole, so an interrupted download
    never leaves a SteamCMD that looks installed."""
    script = directory / "steamcmd.sh"
    if script.exists():
        return script
    partial = directory.with_name(f"{directory.name}.partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    _log.info("downloading SteamCMD from %s", STEAMCMD_URL)
    with fetch(STEAMCMD_URL, DOWNLOAD_TIMEOUT_S) as response, tarfile.open(fileobj=response, mode="r|gz") as archive:
        archive.extractall(partial, filter="data")
    if not (partial / script.name).exists():
        shutil.rmtree(partial)
        raise RuntimeError(f"the SteamCMD download did not contain {script.name}")
    shutil.rmtree(directory, ignore_errors=True)
    partial.rename(directory)
    return script


def server_build(install_dir: Path) -> str | None:
    """The installed dedicated server's build id, or None unless Steam records it fully installed."""
    manifest = install_dir / "steamapps" / f"appmanifest_{SERVER_APP_ID}.acf"
    if not manifest.exists():
        return None
    text = manifest.read_text()
    flags = re.search(r'^\s*"StateFlags"\s+"(\d+)"', text, re.MULTILINE)
    build = re.search(r'^\s*"buildid"\s+"(\d+)"', text, re.MULTILINE)
    if flags is None or flags.group(1) != FULLY_INSTALLED or build is None:
        return None
    return build.group(1)


def ensure_server(steamcmd: Path, install_dir: Path, run: Run = run, stopping: Stopping = lambda: False) -> str:
    """The dedicated server's build id, after installing or updating it. A first install
    validates every file and an update does not; an update that leaves the server not fully
    installed is retried with validation. When SteamCMD cannot reach Steam, the installed
    build stays as it was. Raises Stopped when a stop is requested meanwhile."""
    installed = server_build(install_dir)
    validate = installed is None
    for attempt in range(1, INSTALL_ATTEMPTS + 1):
        _log.info(
            "%s the dedicated server, attempt %d of %d",
            "installing" if validate else "updating",
            attempt,
            INSTALL_ATTEMPTS,
        )
        run(_app_update(steamcmd, install_dir, validate), INSTALL_TIMEOUT_S if validate else UPDATE_TIMEOUT_S, stopping)
        build = server_build(install_dir)
        if build is not None:
            was = "" if installed in (None, build) else f", was {installed}"
            _log.info("dedicated server build %s%s", build, was)
            return build
        validate = True
    raise RuntimeError(f"SteamCMD did not install the dedicated server after {INSTALL_ATTEMPTS} attempts")


def _app_update(steamcmd: Path, install_dir: Path, validate: bool) -> list[str]:
    command = [str(steamcmd), "+force_install_dir", str(install_dir), "+login", "anonymous"]
    command += ["+app_update", str(SERVER_APP_ID), *(["validate"] if validate else []), "+quit"]
    return command
