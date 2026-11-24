import subprocess
from typing import Any

import pytest

from vhd_control.docker import Cli, DockerError, DockerTimeout


def test_a_docker_command_that_never_finishes_is_a_docker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(args: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(DockerError, match="did not finish"):
        Cli().output(["ps"])
    with pytest.raises(DockerError, match="did not finish"):
        Cli().stream(["compose", "up"])
    with pytest.raises(DockerTimeout, match="did not finish within 1800 s"):
        Cli().run(["run", "image"], 1800)


def test_a_one_off_container_gives_its_exit_status_and_output_whatever_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs)
        return subprocess.CompletedProcess(args, 1, stdout='{"outcome": "refused"}\n')

    monkeypatch.setattr(subprocess, "run", run)
    assert Cli().run(["run", "image"], 1800) == (1, '{"outcome": "refused"}\n')
    # Its standard error is the server manager's to read; the output is decoded whatever it holds.
    assert seen[0]["stdout"] == subprocess.PIPE and "stderr" not in seen[0]
    assert seen[0]["errors"] == "replace" and seen[0]["timeout"] == 1800
