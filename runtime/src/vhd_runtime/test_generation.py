import json
from pathlib import Path

import pytest

from vhd_runtime.generation import Generation, IncompleteGeneration, InvalidGeneration, Reader

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
MINIMAL = {
    "contract": 1,
    "generation": 4,
    "world": {"name": "Meadowfold", "port": 2456},
    "plugin": {"status_interval_s": 5},
}


def write(directory: Path, name: str, document: object) -> None:
    (directory / name).write_text(json.dumps(document))


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
        password="hearth-and-horn",
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


def test_same_settings_ignores_only_the_number() -> None:
    first = Generation(4, "Meadowfold", 2456, 5, None, None)
    assert first.same_settings(Generation(5, "Meadowfold", 2456, 5, None, None))
    assert not first.same_settings(Generation(5, "Meadowfold", 2456, 5, None, "hearth-and-horn"))
    assert not first.same_settings(Generation(5, "Meadowfold", 2459, 5, None, None))


def test_refuses_an_unknown_contract_version(tmp_path: Path) -> None:
    write(tmp_path, "generation.json", {**MINIMAL, "contract": 2})
    with pytest.raises(InvalidGeneration, match="contract"):
        Reader(tmp_path, CONTRACTS).read()


def test_refuses_a_save_name_the_manifest_schema_refuses(tmp_path: Path) -> None:
    write(tmp_path, "generation.json", {**MINIMAL, "world": {"name": "../Meadowfold", "port": 2456}})
    with pytest.raises(InvalidGeneration, match="world/name"):
        Reader(tmp_path, CONTRACTS).read()


def test_refuses_short_password(tmp_path: Path) -> None:
    write(tmp_path, "generation.json", MINIMAL)
    write(tmp_path, "secrets.json", {"contract": 1, "generation": 4, "password": "horn"})
    with pytest.raises(InvalidGeneration, match=r"secrets\.json at password"):
        Reader(tmp_path, CONTRACTS).read()


def test_refuses_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "generation.json").write_text("{")
    with pytest.raises(InvalidGeneration, match=r"generation\.json"):
        Reader(tmp_path, CONTRACTS).read()


def test_refuses_a_mount_that_shows_other_worlds(tmp_path: Path) -> None:
    write(tmp_path, "generation.json", MINIMAL)
    write(tmp_path, "secrets.json", {"contract": 1, "generation": 4})
    (tmp_path / "world-ashwake-1a2b3c").mkdir()
    with pytest.raises(InvalidGeneration, match="other worlds"):
        Reader(tmp_path, CONTRACTS).read()
