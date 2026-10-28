import subprocess
from typing import Any

import pytest

from vhd_control.docker import Cli, DockerError


def test_a_docker_command_that_never_finishes_is_a_docker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(args: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(DockerError, match="did not finish"):
        Cli().output(["ps"])
    with pytest.raises(DockerError, match="did not finish"):
        Cli().stream(["compose", "up"])
