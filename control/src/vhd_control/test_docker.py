import io
import subprocess
import time
from typing import Any

import pytest

from vhd_control import docker
from vhd_control.docker import Cli, DockerError, DockerTimeout, Ran


def test_a_docker_command_that_never_finishes_is_a_docker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(args: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(DockerError, match="did not finish"):
        Cli().output(["ps"])
    with pytest.raises(DockerError, match="did not finish"):
        Cli().stream(["compose", "up"])


@pytest.fixture
def shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """The docker CLI's place taken by a shell, so a one-off's arguments are a script."""
    monkeypatch.setattr(docker, "DOCKER", "/bin/sh")


@pytest.mark.usefixtures("shell")
def test_a_one_off_container_gives_its_status_and_output_and_passes_its_errors_on_as_printable_text() -> None:
    err = io.StringIO()
    script = "printf 'out\\n'; printf 'a\\033[31mred\\n' >&2; printf 'second\\n' >&2; exit 3"

    ran = Cli(err).run(["-c", script], 5)

    assert ran == Ran(3, "out\n", ("a?[31mred", "second"))
    assert err.getvalue() == "a?[31mred\nsecond\n"


@pytest.mark.usefixtures("shell")
def test_a_one_off_keeps_only_its_last_error_lines_and_reads_output_that_is_no_text() -> None:
    script = "for n in 1 2 3 4 5 6 7; do echo line $n >&2; done; printf '\\377\\n'"
    ran = Cli(io.StringIO()).run(["-c", script], 5)
    assert ran.errors == tuple(f"line {n}" for n in range(3, 8))
    assert ran.output == "�\n"


class ClosedPipe(io.StringIO):
    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


@pytest.mark.usefixtures("shell")
def test_a_one_off_whose_errors_reach_a_closed_pipe_still_runs_to_its_end() -> None:
    # Far more than a pipe holds: a relay that stopped reading would leave the command blocked.
    script = "i=0; while [ $i -lt 400 ]; do printf '%0512d\\n' $i >&2; i=$((i + 1)); done; echo out; exit 4"

    ran = Cli(ClosedPipe()).run(["-c", script], 10)

    assert ran.status == 4
    assert ran.output == "out\n"
    assert ran.errors[-1] == f"{399:0512d}"


@pytest.mark.usefixtures("shell")
def test_a_one_off_that_outlasts_its_timeout_is_killed() -> None:
    started = time.monotonic()
    with pytest.raises(DockerTimeout, match=r"did not finish within 0\.2 s"):
        Cli(io.StringIO()).run(["-c", "exec sleep 30"], 0.2)
    assert time.monotonic() - started < 10
