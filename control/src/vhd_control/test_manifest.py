import shutil
from pathlib import Path

import pytest

from vhd_control.manifest import InvalidSettings, Settings, World, load
from vhd_control.schemas import Schemas

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "schema" / "examples"
SCHEMAS = Schemas(REPO)


def install(tmp_path: Path, manifest: str, secrets: str | None = None) -> Path:
    shutil.copy(EXAMPLES / manifest, tmp_path / "manifest.toml")
    if secrets is not None:
        shutil.copy(EXAMPLES / secrets, tmp_path / "secrets.toml")
    return tmp_path


def test_one_world_takes_every_default_from_the_schema(tmp_path: Path) -> None:
    settings = load(install(tmp_path, "valid/manifest-one-world.toml"), SCHEMAS)
    assert settings == Settings(worlds=(World("Meadowfold", 2456, None),), web_map_port=8456)


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
    with pytest.raises(InvalidSettings, match=r"secrets\.toml at world/Meadowfold/password"):
        load(install(tmp_path, "valid/manifest-one-world.toml", "invalid/secrets-short-password.toml"), SCHEMAS)


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
