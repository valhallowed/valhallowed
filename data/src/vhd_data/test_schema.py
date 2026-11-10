import json
from pathlib import Path

import pytest

from vhd_data.schema import GRANTS, migrations
from vhd_data.test_documents import CONTRACTS


def test_the_release_migrations_are_numbered_from_one() -> None:
    assert [number for number, _ in migrations()] == list(range(1, len(migrations()) + 1))


def test_a_gap_in_the_migrations_is_a_fault(tmp_path: Path) -> None:
    (tmp_path / "001_first.sql").write_text("SELECT 1;")
    (tmp_path / "003_third.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="numbered 1 to 2"):
        migrations(tmp_path)


def test_a_stray_file_is_a_fault(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("")
    with pytest.raises(ValueError, match="not a migration"):
        migrations(tmp_path)


def test_every_role_the_secrets_hold_a_password_for_has_grants() -> None:
    # apply looks up the grants of every role it is handed a password for: a role without an entry is a KeyError.
    secrets = json.loads((CONTRACTS / "database.secrets.schema.json").read_text())
    assert set(secrets["properties"]["passwords"]["properties"]) <= set(GRANTS)


def test_the_web_map_role_has_grants() -> None:
    assert "web_map" in GRANTS
