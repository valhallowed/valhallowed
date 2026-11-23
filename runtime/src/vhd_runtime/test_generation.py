import json
import shutil
from pathlib import Path

import pytest

from vhd_runtime.generation import (
    Backups,
    Generation,
    IncompleteGeneration,
    InvalidGeneration,
    Reader,
    Restore,
    Updates,
    validator,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
INTAKE = "http://intake:9458/events"
BACKUPS = Backups(every_s=3600, keep=24, keep_daily=14)
UPDATES = Updates(policy="when-empty", every_s=900)
MINIMAL = {
    "contract": 4,
    "generation": 4,
    "world": {"name": "Meadowfold", "port": 2456},
    "plugin": {"status_interval_s": 5, "intake_url": "http://intake:9458/events"},
    "backups": {"every_s": 3600, "keep": 24, "keep_daily": 14},
    "updates": {"policy": "when-empty", "every_s": 900},
}


def write(directory: Path, name: str, document: object) -> None:
    """Replace the file whole, as the control plane does."""
    temporary = directory / f".{name}.tmp"
    temporary.write_text(json.dumps(document))
    temporary.replace(directory / name)


def test_no_generation_yet(tmp_path: Path) -> None:
    assert Reader(tmp_path, CONTRACTS).read() is None


def test_reads_generation_and_password(tmp_path: Path) -> None:
    write(tmp_path, "generation.json", {**MINIMAL, "status_url": "http://control-plane:9457/status"})
    write(tmp_path, "secrets.json", {"contract": 1, "generation": 4, "password": "hearth-and-horn"})
    assert Reader(tmp_path, CONTRACTS).read() == Generation(
        number=4,
        world="Meadowfold",
        port=2456,
        status_interval_s=5,
        status_url="http://control-plane:9457/status",
        intake_url=INTAKE,
        password="hearth-and-horn",
        backups=BACKUPS,
        updates=UPDATES,
    )


def test_secrets_without_a_password_mean_no_password(tmp_path: Path) -> None:
    write(tmp_path, "generation.json", MINIMAL)
    write(tmp_path, "secrets.json", {"contract": 1, "generation": 4})
    generation = Reader(tmp_path, CONTRACTS).read()
    assert generation is not None
    assert generation.password is None
    assert generation.status_url is None


def test_a_generation_without_its_secrets_is_incomplete(tmp_path: Path) -> None:
    write(tmp_path, "generation.json", MINIMAL)
    with pytest.raises(IncompleteGeneration, match=r"no secrets\.json"):
        Reader(tmp_path, CONTRACTS).read()


def test_secrets_for_another_generation_are_incomplete(tmp_path: Path) -> None:
    write(tmp_path, "generation.json", MINIMAL)
    write(tmp_path, "secrets.json", {"contract": 1, "generation": 3, "password": "hearth-and-horn"})
    with pytest.raises(IncompleteGeneration, match="generation 3, not 4"):
        Reader(tmp_path, CONTRACTS).read()


def test_a_changed_file_is_read_again(tmp_path: Path) -> None:
    reader = Reader(tmp_path, CONTRACTS)
    write(tmp_path, "generation.json", MINIMAL)
    write(tmp_path, "secrets.json", {"contract": 1, "generation": 4})
    first = reader.read()
    assert reader.read() is first
    write(tmp_path, "generation.json", {**MINIMAL, "generation": 5})
    write(tmp_path, "secrets.json", {"contract": 1, "generation": 5, "password": "hearth-and-horn"})
    second = reader.read()
    assert second is not None
    assert (second.number, second.password) == (5, "hearth-and-horn")


def test_a_short_password_is_refused_without_being_quoted(tmp_path: Path) -> None:
    write(tmp_path, "generation.json", MINIMAL)
    write(tmp_path, "secrets.json", {"contract": 1, "generation": 4, "password": "horn"})
    with pytest.raises(InvalidGeneration, match=r"secrets\.json at password: breaks minLength 5") as refused:
        Reader(tmp_path, CONTRACTS).read()
    assert "horn" not in str(refused.value)


def test_a_generation_never_shows_its_password_in_a_repr() -> None:
    generation = Generation(4, "Meadowfold", 2456, 5, None, INTAKE, "hearth-and-horn", BACKUPS, UPDATES)
    assert "hearth-and-horn" not in repr(generation)


def test_reads_a_confirmed_restart(tmp_path: Path) -> None:
    write(tmp_path, "generation.json", {**MINIMAL, "disconnect_players": True})
    write(tmp_path, "secrets.json", {"contract": 1, "generation": 4})
    generation = Reader(tmp_path, CONTRACTS).read()
    assert generation is not None
    assert generation.disconnect_players


def test_reads_a_restore(tmp_path: Path) -> None:
    restore = {"id": "5f0c9a3e1b7d4c2a8e6f0b1d3c5a7e9f", "backup": "20261118T194011Z"}
    write(tmp_path, "generation.json", {**MINIMAL, "restore": restore})
    write(tmp_path, "secrets.json", {"contract": 1, "generation": 4})
    generation = Reader(tmp_path, CONTRACTS).read()
    assert generation is not None
    assert generation.restore == Restore(**restore)


def test_same_settings_ignores_a_restore_whose_cost_the_runtime_decides() -> None:
    first = Generation(4, "Meadowfold", 2456, 5, None, INTAKE, None, BACKUPS, UPDATES)
    asked = Restore("5f0c9a3e1b7d4c2a8e6f0b1d3c5a7e9f", "20261118T194011Z")
    restoring = Generation(5, "Meadowfold", 2456, 5, None, INTAKE, None, BACKUPS, UPDATES, restore=asked)
    assert first.same_settings(restoring) and restoring.same_settings(first)


def test_same_settings_ignores_the_number_the_confirmation_and_what_is_adopted_in_place() -> None:
    first = Generation(4, "Meadowfold", 2456, 5, None, INTAKE, None, BACKUPS, UPDATES)
    assert first.same_settings(Generation(5, "Meadowfold", 2456, 5, None, INTAKE, None, BACKUPS, UPDATES))
    assert first.same_settings(
        Generation(5, "Meadowfold", 2456, 5, None, INTAKE, None, BACKUPS, UPDATES, disconnect_players=True)
    )
    assert first.same_settings(
        Generation(5, "Meadowfold", 2456, 5, None, INTAKE, None, Backups(1800, 48, 0), Updates("at-start", 3600))
    )
    password = Generation(5, "Meadowfold", 2456, 5, None, INTAKE, "hearth-and-horn", BACKUPS, UPDATES)
    assert not first.same_settings(password)
    assert not first.same_settings(Generation(5, "Meadowfold", 2459, 5, None, INTAKE, None, BACKUPS, UPDATES))


def test_refuses_an_unknown_contract_version(tmp_path: Path) -> None:
    write(tmp_path, "generation.json", {**MINIMAL, "contract": 1})
    with pytest.raises(InvalidGeneration, match="contract"):
        Reader(tmp_path, CONTRACTS).read()


def test_a_reader_whose_contract_refers_to_a_schema_the_image_lacks_fails_at_the_start(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    shutil.copytree(CONTRACTS, contracts)
    refers = r"^game-runtime\.generation\.schema\.json refers to .*, which cannot be found$"
    with pytest.raises(ValueError, match=refers):
        Reader(tmp_path, contracts)


def test_a_ref_inside_a_value_that_holds_data_is_data_not_a_reference(tmp_path: Path) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"link": {"const": {"$ref": "missing.schema.json"}, "examples": [{"$ref": "gone.json"}]}},
    }
    path = tmp_path / "contracts" / "data.schema.json"
    path.parent.mkdir()
    path.write_text(json.dumps(schema))
    assert validator(path).is_valid({"link": {"$ref": "missing.schema.json"}})


@pytest.mark.parametrize("keyword", ["properties", "$defs", "dependencies"])
def test_a_schema_named_for_a_data_keyword_is_a_schema_whose_ref_is_checked(tmp_path: Path, keyword: str) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        keyword: {"default": {"$ref": "missing.schema.json"}},
    }
    path = tmp_path / "contracts" / "named.schema.json"
    path.parent.mkdir()
    path.write_text(json.dumps(schema))
    refers = r"^named\.schema\.json refers to missing\.schema\.json, which cannot be found$"
    with pytest.raises(ValueError, match=refers):
        validator(path)


def test_refuses_a_save_name_the_manifest_schema_refuses(tmp_path: Path) -> None:
    write(tmp_path, "generation.json", {**MINIMAL, "world": {"name": "../Meadowfold", "port": 2456}})
    with pytest.raises(InvalidGeneration, match="world/name"):
        Reader(tmp_path, CONTRACTS).read()


def test_refuses_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "generation.json").write_text("{")
    with pytest.raises(InvalidGeneration, match=r"generation\.json"):
        Reader(tmp_path, CONTRACTS).read()


def test_refuses_a_mount_of_the_whole_generations_volume(tmp_path: Path) -> None:
    """The volume's root holds state.json and each world's directory, and no generation.json."""
    (tmp_path / "state.json").write_text("{}")
    (tmp_path / "world-ashwake-1a2b3c").mkdir()
    with pytest.raises(InvalidGeneration, match="other worlds"):
        Reader(tmp_path, CONTRACTS).read()
