"""SteamCMD and the dedicated server, downloaded into the install at first start (G16).

An installed server is kept as it is: when to update the game is the control
plane's policy (Game runtimes), and that capability comes later.
"""

import logging
import re
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import IO, Protocol, cast
from urllib.request import urlopen

STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
SERVER_APP_ID = 896660
INSTALL_ATTEMPTS = 3
DOWNLOAD_TIMEOUT_S = 60
INSTALL_TIMEOUT_S = 3600

_log = logging.getLogger(__name__)


class Response(Protocol):
    def __enter__(self) -> IO[bytes]: ...
    def __exit__(self, *args: object) -> None: ...


Fetch = Callable[[str, float], Response]
Run = Callable[[list[str], float], None]


def fetch(url: str, timeout: float) -> Response:
    return cast(Response, urlopen(url, timeout=timeout))  # noqa: S310 - a fixed https URL


def run(command: list[str], timeout: float) -> None:
    # SteamCMD's exit code is unreliable on a first run; the caller checks the
    # files it wrote instead.
    subprocess.run(command, check=False, timeout=timeout)  # noqa: S603


def ensure_steamcmd(directory: Path, fetch: Fetch = fetch) -> Path:
    """SteamCMD in directory, downloaded from Valve on the first start."""
    script = directory / "steamcmd.sh"
    if script.exists():
        return script
    _log.info("downloading SteamCMD from %s", STEAMCMD_URL)
    directory.mkdir(parents=True, exist_ok=True)
    with fetch(STEAMCMD_URL, DOWNLOAD_TIMEOUT_S) as response, tarfile.open(fileobj=response, mode="r|gz") as archive:
        archive.extractall(directory, filter="data")
    if not script.exists():
        raise RuntimeError(f"the SteamCMD download did not contain {script.name}")
    return script


def server_build(install_dir: Path) -> str | None:
    """The installed dedicated server's build id, or None if it is not installed."""
    manifest = install_dir / "steamapps" / f"appmanifest_{SERVER_APP_ID}.acf"
    if not manifest.exists():
        return None
    match = re.search(r'^\s*"buildid"\s+"(\d+)"', manifest.read_text(), re.MULTILINE)
    return match.group(1) if match else None


def ensure_server(steamcmd: Path, install_dir: Path, run: Run = run) -> str:
    """The dedicated server's build id, installing the server first if it is missing."""
    build = server_build(install_dir)
    if build is not None:
        return build
    command = [
        str(steamcmd),
        "+force_install_dir",
        str(install_dir),
        "+login",
        "anonymous",
        "+app_update",
        str(SERVER_APP_ID),
        "validate",
        "+quit",
    ]
    for attempt in range(1, INSTALL_ATTEMPTS + 1):
        _log.info("installing the dedicated server, attempt %d of %d", attempt, INSTALL_ATTEMPTS)
        run(command, INSTALL_TIMEOUT_S)
        build = server_build(install_dir)
        if build is not None:
            _log.info("dedicated server build %s installed", build)
            return build
    raise RuntimeError(f"SteamCMD did not install the dedicated server after {INSTALL_ATTEMPTS} attempts")
