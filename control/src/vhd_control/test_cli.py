import io
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from vhd_control import cli, client, layout
from vhd_control.schemas import Schemas
from vhd_control.test_daemon import REPO, SCHEMAS


def test_validate_shows_what_the_manifest_reconciles_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copy(REPO / "schema/examples/valid/manifest-two-worlds.toml", tmp_path / "manifest.toml")
    shutil.copy(REPO / "schema/examples/valid/secrets-one-world.toml", tmp_path / "secrets.toml")
    monkeypatch.setattr(layout, "INSTALL", tmp_path)
    out = io.StringIO()

    cli.validate(SCHEMAS, out)
    assert out.getvalue().splitlines() == [
        "world Ashwake: udp 2459-2461, password unset",
        "world Meadowfold: udp 2456-2458, password set",
        "web map: tcp 8456",
        "backups: into backups/, relative to the install folder, after a new save at most every 1h, "
        "keeping the newest 24 and 14 daily",
        "updates: when-empty, checking every 15m",
    ]


INTAKE_REPORT = {"contract": 1, "generation": 5, "service": "intake", "state": "running", "accepted": 40, "dropped": 2}


def test_status_names_each_world_its_players_and_what_waits() -> None:
    status = {
        "contract": 2,
        "generation": 5,
        "worlds": {
            "Meadowfold": {
                "published": 4,
                "pending": {"generation": 5, "waits_for": "an empty world"},
                "state": "running",
                "report": {
                    "contract": 1,
                    "generation": 4,
                    "world": "Meadowfold",
                    "state": "running",
                    "players": 2,
                    "backups": {"names": ["20261118T194012Z", "20261118T184011Z"]},
                },
                "report_age_s": 1,
            },
            "Ashwake": {"state": "unknown"},
            "Frostmere": {
                "state": "running",
                "report": {
                    "contract": 1,
                    "generation": 4,
                    "world": "Frostmere",
                    "state": "running",
                    "backups": {"names": ["99999999T999999Z"], "failure": "save 1 lacks _main.1.db2\x1b[2J"},
                },
            },
        },
        "services": {
            "intake": {
                "published": 5,
                "state": "running",
                "report": INTAKE_REPORT,
                "report_age_s": 1,
            },
            "database": {"state": "unknown", "pending": {"generation": 5, "waits_for": "vhd apply"}},
        },
    }
    out = io.StringIO()
    cli.show_status(status, out, now=datetime(2026, 11, 18, 20, 4, 12, tzinfo=UTC))
    assert out.getvalue().splitlines() == [
        "generation 5",
        "Ashwake: unknown",
        "Frostmere: running, 1 backup(s) (backups: save 1 lacks _main.1.db2?[2J)",
        "Meadowfold: running, 2 player(s), 2 backup(s), the newest 24m old; generation 5 waits for an empty world",
        "database service: unknown; generation 5 waits for vhd apply",
        "intake service: running, 40 event(s) taken, 2 dropped",
    ]


def test_a_refused_manifest_is_reported_with_a_failing_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "manifest.toml").write_text("schema = 2\n")
    monkeypatch.setattr(layout, "INSTALL", tmp_path)
    monkeypatch.setattr(layout, "SCHEMAS", REPO)
    assert cli.main(["validate"], io.StringIO()) == 1


def test_status_shows_an_override_and_only_printable_reasons() -> None:
    held = {"contract": 1, "generation": 3, "world": "Ashwake", "state": "held", "reason": "loaded empty\x1b[2J"}
    status = {
        "contract": 2,
        "generation": 3,
        "override": {"generation": 3, "worlds": {"Meadowfold": 2, "Ashwake": None}},
        "worlds": {"Ashwake": {"state": "held", "report": held, "report_age_s": 1}},
        "services": {},
    }
    out = io.StringIO()
    cli.show_status(status, out)
    assert out.getvalue().splitlines() == [
        "generation 3",
        "generation 3 restarted Ashwake (players unknown), Meadowfold (2 player(s)), as the server manager confirmed",
        "Ashwake: held (loaded empty?[2J)",
    ]


NOW = datetime(2026, 11, 18, 20, 4, 12, tzinfo=UTC)
BACKUP = "20261118T194012Z"


def world_status(**report: object) -> dict[str, Any]:
    """A status document of one world, whose runtime reported these fields besides the basics."""
    basics = {"contract": 1, "generation": 1, "world": "Meadowfold", "state": "running"}
    return {
        "contract": 2,
        "generation": 1,
        "worlds": {"Meadowfold": {"state": "running", "report": {**basics, **report}}},
    }


def backups_of(status: dict[str, Any], world: str = "Meadowfold") -> list[str]:
    out = io.StringIO()
    cli.show_backups(status, world, out, now=NOW)
    return out.getvalue().splitlines()


def test_backups_lists_the_names_newest_first_with_their_ages() -> None:
    names = ["20261118T194012Z", "20261118T184011Z", "20261117T194012Z", "not-a-time"]
    assert backups_of(world_status(backups={"names": names})) == [
        "20261118T194012Z, 24m old",
        "20261118T184011Z, 84m old",
        "20261117T194012Z, 24h old",
        "not-a-time",
    ]


def test_backups_says_when_there_is_none_yet() -> None:
    assert backups_of(world_status(backups={"names": []})) == ["Meadowfold: no backup yet"]


def test_backups_says_when_the_runtime_has_not_reported_them() -> None:
    assert backups_of(world_status()) == ["Meadowfold: its runtime has not reported its backups yet"]
    status = world_status()
    del status["worlds"]["Meadowfold"]["report"]
    assert backups_of(status) == ["Meadowfold: its runtime has not reported its backups yet"]


def test_backups_shows_the_failure_as_printable_text() -> None:
    status = world_status(backups={"names": [BACKUP], "failure": "disk full\x1b[2J"})
    assert backups_of(status) == [f"{BACKUP}, 24m old", "Meadowfold: backups: disk full?[2J"]


def test_backups_names_a_world_the_install_does_not_run() -> None:
    with pytest.raises(cli.NoSuchWorld, match="runs no world Hollowmere"):
        backups_of(world_status(), "Hollowmere")


def test_backups_for_an_unknown_world_fails_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(layout, "SCHEMAS", REPO)
    monkeypatch.setattr(client, "status", lambda path, schemas: world_status())
    assert cli.main(["backups", "Hollowmere"], io.StringIO()) == 1
    assert "runs no world Hollowmere" in capsys.readouterr().err


def test_restore_says_it_is_handed(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, str]] = []

    def restore(path: Path, schemas: Schemas, world: str, backup: str) -> dict[str, Any]:
        asked.append((world, backup))
        return {"contract": 1, "id": "a" * 32, "world": world, "backup": backup}

    monkeypatch.setattr(layout, "SCHEMAS", REPO)
    monkeypatch.setattr(client, "restore", restore)
    out = io.StringIO()
    assert cli.main(["restore", "Meadowfold", BACKUP], out) == 0
    assert asked == [("Meadowfold", BACKUP)]
    assert f"holds restore {'a' * 32} of Meadowfold's backup {BACKUP} and hands it" in out.getvalue()


def test_a_refused_restore_fails_the_command_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(path: Path, schemas: Schemas, world: str, backup: str) -> dict[str, Any]:
        raise client.Refused("Meadowfold's runtime has not reported backup " + backup)

    monkeypatch.setattr(layout, "SCHEMAS", REPO)
    monkeypatch.setattr(client, "restore", refuse)
    out = io.StringIO()
    assert cli.main(["restore", "Meadowfold", BACKUP], out) == 1
    assert out.getvalue() == ""
    assert f"vhd restore: Meadowfold's runtime has not reported backup {BACKUP}" in capsys.readouterr().err


def status_line(world: dict[str, Any]) -> str:
    out = io.StringIO()
    cli.show_status({"contract": 2, "generation": 2, "worlds": {"Meadowfold": world}, "services": {}}, out, now=NOW)
    return out.getvalue().splitlines()[1]


HELD = {"id": "a" * 32, "backup": BACKUP}


def report_with(**more: object) -> dict[str, Any]:
    return world_status(**more)["worlds"]["Meadowfold"]["report"]  # type: ignore[no-any-return]


def test_status_shows_a_restore_asked() -> None:
    world = {"state": "running", "report": report_with(), "restore": HELD}
    assert status_line(world) == f"Meadowfold: running; restore of backup {BACKUP} asked"


@pytest.mark.parametrize("held", [HELD, None])
def test_status_shows_the_outcome_of_the_restore_held_or_of_the_newest_when_none_is(
    held: dict[str, str] | None,
) -> None:
    acted = {"id": "a" * 32, "outcome": "failed", "reason": "no space\x1b[2J"}
    world: dict[str, Any] = {"state": "running", "report": report_with(restore=acted)}
    if held is not None:
        world["restore"] = held
    asked = f"; restore of backup {BACKUP} asked" if held else ""
    assert status_line(world) == f"Meadowfold: running{asked}; restore failed (no space?[2J)"


def test_status_hides_the_outcome_of_another_restore_than_the_one_held() -> None:
    acted = {"id": "b" * 32, "outcome": "done"}
    world = {"state": "running", "report": report_with(restore=acted), "restore": HELD}
    assert status_line(world) == f"Meadowfold: running; restore of backup {BACKUP} asked"
