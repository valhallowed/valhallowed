import json
from pathlib import Path

import pytest

from vhd_runtime import __main__ as entry
from vhd_runtime import transfer
from vhd_runtime.test_backups import NOW
from vhd_runtime.test_transfer import RESULT, WORLD, backup_of, make_mounts, put_save
from vhd_runtime.transfer import Mounts


@pytest.fixture
def mounts(tmp_path: Path) -> Mounts:
    return make_mounts(tmp_path)


@pytest.fixture
def no_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    def never() -> int:
        raise AssertionError("an action started the supervisor")

    monkeypatch.setattr(entry, "supervise", never)


def test_no_arguments_start_the_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []

    def supervise() -> int:
        started.append(True)
        return 0

    monkeypatch.setattr(entry, "supervise", supervise)
    assert entry.main([]) == 0
    assert started == [True]


@pytest.mark.usefixtures("no_supervisor")
def test_import_runs_the_action_and_prints_its_result_last(
    mounts: Mounts, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    put_save(mounts)
    monkeypatch.setattr(transfer, "Mounts", lambda: mounts)
    assert entry.main(["import", WORLD]) == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    RESULT.validate(result)
    assert (result["action"], result["outcome"]) == ("import", "done")


@pytest.mark.usefixtures("no_supervisor")
def test_a_refused_import_exits_1(
    mounts: Mounts, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(transfer, "Mounts", lambda: mounts)
    assert entry.main(["import", WORLD]) == 1
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    RESULT.validate(result)
    assert result["outcome"] == "refused"


@pytest.mark.usefixtures("no_supervisor")
@pytest.mark.parametrize("named", [False, True])
def test_export_runs_the_action_and_prints_its_result_last(
    mounts: Mounts, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], named: bool
) -> None:
    name = backup_of(mounts, 41, NOW)
    monkeypatch.setattr(transfer, "Mounts", lambda: mounts)
    assert entry.main(["export", WORLD, *([name] if named else [])]) == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    RESULT.validate(result)
    assert (result["action"], result["outcome"], result["backup"]) == ("export", "done", name)


@pytest.mark.usefixtures("no_supervisor")
@pytest.mark.parametrize(
    "args",
    [
        ["bogus"],
        ["bogus", WORLD],
        ["import"],
        ["import", WORLD, "extra"],
        ["import", "../escape"],
        ["import", ""],
        ["export"],
        ["export", WORLD, "20261118T204012Z", "extra"],
        ["export", "Meadow/fold"],
        ["--help"],
    ],
)
def test_anything_else_exits_2_with_its_usage(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == entry.USAGE + "\n"
