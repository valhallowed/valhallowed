"""The Docker CLI, which vhd runs for vhd apply, vhd import and vhd export: it holds the socket for
that run alone, never the control plane (G13)."""

import subprocess
from typing import Protocol

# Where the control plane's image installs the docker CLI and its Compose plugin.
DOCKER = "/usr/local/bin/docker"
TIMEOUT_S = 120
# Pulling an image can take a while on a slow link.
STREAM_TIMEOUT_S = 1800


class DockerError(Exception):
    """A docker command failed."""


class DockerTimeout(DockerError):
    """A docker command did not finish in time. Its CLI is killed; a container it started is not."""


class Docker(Protocol):
    def output(self, args: list[str]) -> str: ...
    def stream(self, args: list[str]) -> None: ...


class OneOff(Docker, Protocol):
    def run(self, args: list[str], timeout_s: float) -> tuple[int, str]: ...


class Cli:
    """The docker CLI in the control plane's image, its Compose plugin included."""

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

    def run(self, args: list[str], timeout_s: float) -> tuple[int, str]:
        """A command's exit status and standard output, whatever the status; its standard error goes
        straight to the server manager. Raises DockerTimeout after timeout_s."""
        try:
            result = subprocess.run(  # noqa: S603 - docker, with arguments vhd built
                [DOCKER, *args],
                stdout=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise DockerTimeout(f"docker {' '.join(args)} did not finish within {timeout_s:g} s") from error
        return result.returncode, result.stdout
