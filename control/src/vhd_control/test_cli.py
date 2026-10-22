import io
import shutil
from pathlib import Path

import pytest

from vhd_control import cli, layout
from vhd_control.test_daemon import REPO, SCHEMAS


def test_validate_shows_what_the_manifest_reconciles_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copy(REPO / "schema/examples/valid/manifest-two-worlds.toml", tmp_path / "manifest.toml")
    shutil.copy(REPO / "schema/examples/valid/secrets-one-world.toml", tmp_path / "secrets.toml")
    monkeypatch.setattr(layout, "INSTALL", tmp_path)
    out = io.StringIO()

    assert cli.validate(SCHEMAS, out) == 0
    assert out.getvalue().splitlines() == [
        "world Ashwake: udp 2459-2461, password unset",
        "world Meadowfold: udp 2456-2458, password set",
        "web map: tcp 8456",
    ]


def test_status_names_each_world_its_players_and_what_waits() -> None:
    status = {
        "contract": 1,
        "generation": 5,
        "worlds": {
            "Meadowfold": {
                "published": 4,
                "pending": {"generation": 5, "waits_for": "an empty world"},
                "state": "running",
                "report": {"contract": 1, "generation": 4, "world": "Meadowfold", "state": "running", "players": 2},
                "report_age_s": 1,
            },
            "Ashwake": {"state": "unknown"},
        },
    }
    out = io.StringIO()
    assert cli.show_status(status, out) == 0
    assert out.getvalue().splitlines() == [
        "generation 5",
        "Ashwake: unknown",
        "Meadowfold: running, 2 player(s); generation 5 waits for an empty world",
    ]


def test_a_refused_manifest_is_reported_with_a_failing_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "manifest.toml").write_text("schema = 2\n")
    monkeypatch.setattr(layout, "INSTALL", tmp_path)
    monkeypatch.setattr(layout, "SCHEMAS", REPO)
    assert cli.main(["validate"], io.StringIO()) == 1
