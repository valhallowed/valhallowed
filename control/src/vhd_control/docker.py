"""The Docker CLI, which vhd runs for vhd apply, vhd import and vhd export: it holds the socket for
that run alone, never the control plane (G13)."""

import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Protocol, TextIO

from vhd_control.text import printable

# Where the control plane's image installs the docker CLI and its Compose plugin.
DOCKER = "/usr/local/bin/docker"
TIMEOUT_S = 120
# Pulling an image can take a while on a slow link.
STREAM_TIMEOUT_S = 1800
# What vhd keeps of a one-off container's standard error for its own words: the last lines, each
# read at most this long.
ERROR_LINES = 5
LINE_MAX = 1 << 16
# How long vhd reads a one-off's output once its command has ended: a process the command left may
# hold its pipes open for as long as it runs.
READ_AFTER_S = 5


class DockerError(Exception):
    """A docker command failed."""


class DockerTimeout(DockerError):
    """A docker command did not finish in time. Its CLI is killed; a container it started is not."""


@dataclass(frozen=True)
class Ran:
    """A one-off container's exit status, its standard output, and the last lines of its standard
    error, printable, which the server manager saw as they came."""

    status: int
    output: str
    errors: tuple[str, ...]


class Docker(Protocol):
    def output(self, args: list[str]) -> str: ...
    def stream(self, args: list[str]) -> None: ...


class OneOff(Docker, Protocol):
    def run(self, args: list[str], timeout_s: float) -> Ran: ...


class Cli:
    """The docker CLI in the control plane's image, its Compose plugin included. err: where a
    one-off container's standard error goes, the server manager's terminal unless told otherwise."""

    def __init__(self, err: TextIO | None = None) -> None:
        self._err = err

    def output(self, args: list[str]) -> str:
        try:
            result = subprocess.run(  # noqa: S603 - docker, with arguments vhd built
                [DOCKER, *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=False
            )
        except subprocess.TimeoutExpired as error:
            raise DockerError(f"docker {' '.join(args)} did not finish within {TIMEOUT_S} s") from error
        if result.returncode != 0:
            raise DockerError(f"docker {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout

    def stream(self, args: list[str]) -> None:
        # Its output goes straight to the server manager.
        try:
            result = subprocess.run([DOCKER, *args], timeout=STREAM_TIMEOUT_S, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as error:
            raise DockerError(f"docker {' '.join(args)} did not finish within {STREAM_TIMEOUT_S} s") from error
        if result.returncode != 0:
            raise DockerError(f"docker {' '.join(args)} exited with {result.returncode}")

    def run(self, args: list[str], timeout_s: float) -> Ran:
        """Run a command whatever its exit status, its standard error passed on line by line as
        printable text: a container's words, which may name a file whose name holds a terminal's
        escapes. Raises DockerTimeout after timeout_s, having killed the command."""
        err = self._err or sys.stderr
        output: list[str] = []
        errors: deque[str] = deque(maxlen=ERROR_LINES)
        process = subprocess.Popen(  # noqa: S603 - docker, with arguments vhd built
            [DOCKER, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace"
        )
        stdout, stderr = process.stdout, process.stderr
        if stdout is None or stderr is None:
            process.kill()
            process.wait()
            raise DockerError("docker gave vhd no pipes to read")
        readers = [
            threading.Thread(target=_read, args=(stdout, output.append), daemon=True),
            threading.Thread(target=_relay, args=(stderr, err, errors), daemon=True),
        ]
        # A thread takes its signal mask from the one starting it. With every signal blocked in the
        # readers, the main thread alone takes a stop, so where it holds one (transfer.py), it waits.
        held = signal.pthread_sigmask(signal.SIG_BLOCK, signal.valid_signals())
        try:
            for reader in readers:
                reader.start()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, held)
        try:
            status = process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired as error:
            process.kill()
            raise DockerTimeout(f"docker {' '.join(args)} did not finish within {timeout_s:g} s") from error
        except BaseException:
            process.kill()
            raise
        finally:
            deadline = time.monotonic() + READ_AFTER_S
            for reader in readers:
                reader.join(max(0.0, deadline - time.monotonic()))
            # Closing a pipe waits for a read of it to end, so one a reader still reads stays open.
            if not any(reader.is_alive() for reader in readers):
                stdout.close()
                stderr.close()
            process.wait()
        return Ran(status, "".join(output), tuple(errors))


def _read(stream: IO[str], keep: Callable[[str], None]) -> None:
    keep(stream.read())


def _relay(stream: IO[str], err: TextIO, kept: deque[str]) -> None:
    """Pass each line on to err and keep it. Once err takes no more, as a closed pipe does, the
    lines are still read and kept: a command whose standard error no one read would block on it."""
    shown_to: TextIO | None = err
    for line in iter(lambda: stream.readline(LINE_MAX), ""):
        shown = printable(line.rstrip("\n"))
        kept.append(shown)
        if shown_to is None:
            continue
        try:
            print(shown, file=shown_to, flush=True)
        except (OSError, ValueError):
            shown_to = None
