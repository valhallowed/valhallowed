"""SteamCMD and the dedicated server, downloaded into the install at first start (G16).

Every start also updates the server, under either update policy; while the game runs, the
supervisor checks Steam's public game build on the interval the policy gives, and under the
default policy restarts an empty world to update (Game runtimes, Updates). An update that breaks
loading is what world truth holds.
"""

import logging
import os
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Protocol, cast
from urllib.request import urlopen

STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
SERVER_APP_ID = 896660
STEAMCMD_SCRIPT = "steamcmd.sh"
INSTALL_ATTEMPTS = 3
DOWNLOAD_TIMEOUT_S = 60
INSTALL_TIMEOUT_S = 3600
# An update of an installed server is a delta. Bounded more tightly, a Steam that hangs delays
# the start by this much; an update it cuts short leaves the install to be repaired, which
# gets the install's bound.
UPDATE_TIMEOUT_S = 900
# A check of the public build answered in 5 to 7 s; a SteamCMD that cannot reach Steam gave up
# after 80 to 140 s (Game facts).
CHECK_TIMEOUT_S = 300
# The dedicated server's app info runs to about 10 KiB; anything past this is not read.
CHECK_OUTPUT_MAX = 1024 * 1024
# A build id as the status contract takes it, bounded so no answer can swell a report.
BUILD_ID = re.compile(r"[0-9]{1,20}")
TERMINATE_TIMEOUT_S = 10
POLL_S = 1.0
# Steam's mark for a complete install; any other value is treated as not installed.
FULLY_INSTALLED = "4"

_log = logging.getLogger(__name__)


class Stopped(Exception):
    """A stop was requested while SteamCMD ran."""


class InstallFailed(Exception):
    """SteamCMD or the dedicated server could not be downloaded or installed; the message says why."""


class CheckFailed(Exception):
    """The check of Steam's public game build failed; the message says why."""


class Response(Protocol):
    def __enter__(self) -> IO[bytes]: ...
    def __exit__(self, *args: object) -> None: ...


Fetch = Callable[[str, float], Response]
Stopping = Callable[[], bool]
Run = Callable[[list[str], float, Stopping], None]
# Runs SteamCMD as Run does, and returns what it printed.
Query = Callable[[list[str], float, Stopping], str]


def fetch(url: str, timeout: float) -> Response:
    return cast(Response, urlopen(url, timeout=timeout))  # noqa: S310 - a fixed https URL


def run(command: list[str], timeout: float, stopping: Stopping) -> None:
    """Run SteamCMD until it exits, the timeout passes or a stop is requested. Its exit code
    is unreliable on a first run, so the caller checks the files it wrote instead."""
    try:
        finished = _supervise(command, timeout, stopping, None)
    except OSError as error:
        raise InstallFailed(f"SteamCMD could not start: {error}") from error
    if not finished:
        _log.error("SteamCMD did not finish within %d s", timeout)


def query(command: list[str], timeout: float, stopping: Stopping) -> str:
    """Run SteamCMD as run does, and return what it printed, at most CHECK_OUTPUT_MAX bytes of it.
    A file, not a pipe, takes the output, so SteamCMD never blocks on a reader."""
    with tempfile.TemporaryFile() as output:
        try:
            finished = _supervise(command, timeout, stopping, output)
        except OSError as error:
            raise CheckFailed(f"SteamCMD could not start: {error}") from error
        if not finished:
            raise CheckFailed(f"SteamCMD did not answer within {timeout:.0f} s")
        output.seek(0)
        return output.read(CHECK_OUTPUT_MAX).decode(errors="replace")


def _supervise(command: list[str], timeout: float, stopping: Stopping, output: IO[bytes] | None) -> bool:
    """Run a command until it exits, True, or the timeout passes, False. Raises Stopped when a stop
    is requested meanwhile, and OSError when it cannot start."""
    # A session of its own, so a stop reaches the SteamCMD binary the script starts.
    process = subprocess.Popen(command, start_new_session=True, stdout=output, stderr=output)  # noqa: S603
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if stopping():
            _end(process)
            raise Stopped("stop requested while SteamCMD ran")
        if time.monotonic() > deadline:
            _end(process)
            return False
        time.sleep(POLL_S)
    return True


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
    script = directory / STEAMCMD_SCRIPT
    if script.exists():
        return script
    partial = directory.with_name(f"{directory.name}.partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    _log.info("downloading SteamCMD from %s", STEAMCMD_URL)
    try:
        with (
            fetch(STEAMCMD_URL, DOWNLOAD_TIMEOUT_S) as response,
            tarfile.open(fileobj=response, mode="r|gz") as archive,
        ):
            archive.extractall(partial, filter="data")
    # A connection that fails or drops partway: gzip ends early, or the archive reads short.
    except (OSError, EOFError, tarfile.TarError) as error:
        shutil.rmtree(partial)
        raise InstallFailed(f"the SteamCMD download failed: {error}") from error
    if not (partial / script.name).exists():
        shutil.rmtree(partial)
        raise InstallFailed(f"the SteamCMD download did not contain {script.name}")
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
    if flags is None or flags.group(1) != FULLY_INSTALLED or build is None or not BUILD_ID.fullmatch(build.group(1)):
        return None
    return build.group(1)


def ensure_server(steamcmd: Path, install_dir: Path, run: Run = run, stopping: Stopping = lambda: False) -> str:
    """The dedicated server's build id, after installing or updating it. A first install
    validates every file and an update does not; an update that leaves the server not fully
    installed is retried with validation. When SteamCMD cannot reach Steam, the installed
    build stays as it was. Raises Stopped when a stop is requested meanwhile, and InstallFailed
    when no attempt leaves the server installed."""
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
    raise InstallFailed(f"SteamCMD did not install the dedicated server after {INSTALL_ATTEMPTS} attempts")


def public_build(steamcmd: Path, cache: Path, query: Query = query, stopping: Stopping = lambda: False) -> str:
    """Steam's public build of the dedicated server, from SteamCMD's app info (Game facts). The app
    info SteamCMD keeps in cache is removed first, since whether it answers from there once Steam
    has a newer build is not known. Raises CheckFailed, saying why, and Stopped when a stop is
    requested meanwhile."""
    try:
        cache.unlink(missing_ok=True)
    except OSError as error:
        raise CheckFailed(f"cannot remove SteamCMD's app info cache {cache}: {error.strerror or error}") from error
    command = [str(steamcmd), "+login", "anonymous", "+app_info_update", "1"]
    command += ["+app_info_print", str(SERVER_APP_ID), "+quit"]
    printed = query(command, CHECK_TIMEOUT_S, stopping)
    build = _public_buildid(printed)
    if build is None:
        failed = [line for line in map(_plain, printed.splitlines()) if "FAILED" in line or "ERROR" in line]
        raise CheckFailed("SteamCMD named no public game build" + (f": {failed[-1]}" if failed else ""))
    return build


def _public_buildid(printed: str) -> str | None:
    """The buildid under depots, branches, public in the app info SteamCMD printed, or None."""
    lines = printed.splitlines()
    start = next((n for n, line in enumerate(lines) if _plain(line) == f'"{SERVER_APP_ID}"'), None)
    if start is None:
        return None
    tokens = iter(_TOKEN.findall("\n".join(lines[start + 1 :])))
    try:
        if next(tokens) != ("", "{"):
            return None
        info = _block(tokens)
    except (StopIteration, ValueError):
        return None
    node: object = info
    for key in ("depots", "branches", "public", "buildid"):
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, str) and BUILD_ID.fullmatch(node) else None


# A KeyValues token: a quoted string, its escapes kept, or a brace.
_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')
# The colour codes SteamCMD writes among its lines.
_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _block(tokens: Iterator[tuple[str, str]]) -> dict[str, object]:
    """One KeyValues block, read up to its closing brace. Raises ValueError on a malformed one, and
    StopIteration on one cut short."""
    block: dict[str, object] = {}
    while True:
        key, brace = next(tokens)
        if brace == "}":
            return block
        if brace:
            raise ValueError("a block where a key belongs")
        value, brace = next(tokens)
        if brace == "{":
            block[key] = _block(tokens)
        elif brace:
            raise ValueError("a block closed where a value belongs")
        else:
            block[key] = value


def _plain(line: str) -> str:
    return _ESCAPE.sub("", line).strip()


def _app_update(steamcmd: Path, install_dir: Path, validate: bool) -> list[str]:
    command = [str(steamcmd), "+force_install_dir", str(install_dir), "+login", "anonymous"]
    command += ["+app_update", str(SERVER_APP_ID), *(["validate"] if validate else []), "+quit"]
    return command
