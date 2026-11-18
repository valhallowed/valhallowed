import io
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vhd_control import cli, layout
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
                    "backups": {"names": [], "failure": "save 1 lacks _main.1.db2\x1b[2J"},
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
        "Frostmere: running, no backup yet (the last backup failed: save 1 lacks _main.1.db2?[2J)",
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
