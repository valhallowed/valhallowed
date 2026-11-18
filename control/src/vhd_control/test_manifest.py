import shutil
from pathlib import Path

import pytest

from vhd_control.manifest import Backups, InvalidSettings, Settings, Updates, World, load
from vhd_control.test_daemon import REPO, SCHEMAS
from vhd_control.test_reconcile import BACKUPS, UPDATES

EXAMPLES = REPO / "schema" / "examples"


def install(tmp_path: Path, manifest: str, secrets: str | None = None) -> Path:
    shutil.copy(EXAMPLES / manifest, tmp_path / "manifest.toml")
    if secrets is not None:
        shutil.copy(EXAMPLES / secrets, tmp_path / "secrets.toml")
    return tmp_path


def test_one_world_takes_every_default_from_the_schema(tmp_path: Path) -> None:
    settings = load(install(tmp_path, "valid/manifest-one-world.toml"), SCHEMAS)
    assert settings == Settings(
        worlds=(World("Meadowfold", 2456, None),), web_map_port=8456, backups=BACKUPS, updates=UPDATES
    )


def test_backups_and_updates_take_their_units_from_the_manifest(tmp_path: Path) -> None:
    settings = load(install(tmp_path, "valid/manifest-backups-and-updates.toml"), SCHEMAS)
    assert settings.backups == Backups(path="/srv/vhd-backups", every_s=1800, keep=48, keep_daily=30)
    assert settings.updates == Updates(policy="at-start", every_s=3600)


@pytest.mark.parametrize(
    ("table", "value", "refusal"),
    [
        ("backups", "4m", r"backups/every: '4m' is outside 5m to 1d"),
        ("updates", "2d", r"updates/every: '2d' is outside 5m to 1d"),
    ],
)
def test_a_duration_outside_its_range_is_refused(tmp_path: Path, table: str, value: str, refusal: str) -> None:
    (tmp_path / "manifest.toml").write_text(f'schema = 1\n\n[world.Meadowfold]\n\n[{table}]\nevery = "{value}"\n')
    with pytest.raises(InvalidSettings, match=rf"^manifest\.toml at {refusal}$"):
        load(tmp_path, SCHEMAS)


def test_secrets_reach_their_world(tmp_path: Path) -> None:
    settings = load(install(tmp_path, "valid/manifest-one-world.toml", "valid/secrets-one-world.toml"), SCHEMAS)
    assert settings.worlds == (World("Meadowfold", 2456, "hearth-and-horn"),)


def test_worlds_sort_by_name(tmp_path: Path) -> None:
    settings = load(install(tmp_path, "valid/manifest-two-worlds.toml"), SCHEMAS)
    assert [w.name for w in settings.worlds] == ["Ashwake", "Meadowfold"]
    assert [w.port for w in settings.worlds] == [2459, 2456]


def test_a_manifest_that_breaks_its_schema_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidSettings, match=r"manifest\.toml at world/Meadowfold: .*'prot' was unexpected"):
        load(install(tmp_path, "invalid/manifest-unknown-key.toml"), SCHEMAS)


def test_secrets_that_break_their_schema_are_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidSettings, match=r"secrets\.toml at world/Meadowfold/password: breaks minLength 5"):
        load(install(tmp_path, "valid/manifest-one-world.toml", "invalid/secrets-short-password.toml"), SCHEMAS)


@pytest.mark.parametrize("value", ['"horn"', "12345"])
def test_a_refused_secret_never_appears_in_the_refusal(tmp_path: Path, value: str) -> None:
    root = install(tmp_path, "valid/manifest-one-world.toml")
    (root / "secrets.toml").write_text(f"schema = 1\n\n[world.Meadowfold]\npassword = {value}\n")
    with pytest.raises(InvalidSettings) as refused:
        load(root, SCHEMAS)
    assert value.strip('"') not in str(refused.value)


def test_secrets_for_a_world_the_manifest_lacks_are_refused(tmp_path: Path) -> None:
    root = install(tmp_path, "valid/manifest-one-world.toml")
    (root / "secrets.toml").write_text('schema = 1\n\n[world.Ashwake]\npassword = "hearth-and-horn"\n')
    with pytest.raises(InvalidSettings, match=r"'Ashwake', which manifest\.toml does not"):
        load(root, SCHEMAS)


def test_unreadable_toml_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "manifest.toml").write_text("schema = \n")
    with pytest.raises(InvalidSettings, match=r"^manifest\.toml: "):
        load(tmp_path, SCHEMAS)


def test_a_missing_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidSettings, match=r"^manifest\.toml: "):
        load(tmp_path, SCHEMAS)
