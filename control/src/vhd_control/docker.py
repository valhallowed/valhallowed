"""The Docker CLI, which only vhd apply runs: it holds the socket for that run alone (G13)."""

import subprocess
from typing import Protocol

# Where the control plane's image installs the docker CLI and its Compose plugin.
DOCKER = "/usr/local/bin/docker"
TIMEOUT_S = 120
# Pulling an image can take a while on a slow link.
STREAM_TIMEOUT_S = 1800


class DockerError(Exception):
    """A docker command failed."""


class Docker(Protocol):
    def output(self, args: list[str]) -> str: ...
    def stream(self, args: list[str]) -> None: ...


class Cli:
    """The docker CLI in the control plane's image, its Compose plugin included."""

    def output(self, args: list[str]) -> str:
        """Run docker with args and return what it printed."""
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
        """Run docker with args, its output going straight to the server manager."""
        try:
            result = subprocess.run([DOCKER, *args], timeout=STREAM_TIMEOUT_S, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as error:
            raise DockerError(f"docker {' '.join(args)} did not finish within {STREAM_TIMEOUT_S} s") from error
        if result.returncode != 0:
            raise DockerError(f"docker {' '.join(args)} exited with {result.returncode}")
