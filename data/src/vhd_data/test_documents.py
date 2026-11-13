import json
from pathlib import Path

import pytest

from vhd_data.documents import IncompleteDocuments, InvalidDocuments, Reader


def _contracts() -> Path:
    """The contracts beside these tests: the repo's, or the image's /opt/vhd/contracts."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "contracts" / "intake.events.schema.json").exists():
            return parent / "contracts"
    raise RuntimeError("no contracts/ above the tests")


CONTRACTS = _contracts()
PASSWORD = "q7Zr2Lx9VbN4tKp1Wm8sYc3Hd6Fj0GaE"
STATUS_URL = "http://control-plane:9457/status"
DATABASE = {"host": "database", "port": 5432, "name": "vhd", "user": "intake"}


def write(
    directory: Path,
    generation: dict[str, object],
    secrets: dict[str, object],
    number: int = 1,
    secrets_contract: int = 1,
) -> None:
    """Each service's documents carry that service's contract versions: pass the secrets' when it is not 1."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "secrets.json").write_text(json.dumps({**secrets, "contract": secrets_contract, "generation": number}))
    (directory / "generation.json").write_text(json.dumps({**generation, "contract": 1, "generation": number}))


def intake_documents(directory: Path, worlds: dict[str, str], number: int = 1, password: str = PASSWORD) -> None:
    generation = {"status_url": STATUS_URL, "status_interval_s": 5, "database": DATABASE, "worlds": worlds}
    write(directory, generation, {"password": password}, number)


def test_a_complete_generation_is_read(tmp_path: Path) -> None:
    intake_documents(tmp_path, {"world-meadowfold-1a2b3c": "Meadowfold"})
    documents = Reader("intake", tmp_path, CONTRACTS).read()
    assert documents is not None
    assert documents.number == 1
    assert documents.generation["worlds"] == {"world-meadowfold-1a2b3c": "Meadowfold"}
    assert PASSWORD not in repr(documents)


def test_nothing_is_read_before_the_control_plane_writes(tmp_path: Path) -> None:
    assert Reader("intake", tmp_path, CONTRACTS).read() is None


def test_secrets_for_another_generation_are_incomplete(tmp_path: Path) -> None:
    intake_documents(tmp_path, {}, number=2)
    (tmp_path / "secrets.json").write_text(json.dumps({"contract": 1, "generation": 1, "password": PASSWORD}))
    with pytest.raises(IncompleteDocuments, match="generation 1, not 2"):
        Reader("intake", tmp_path, CONTRACTS).read()


def test_a_bad_secret_is_refused_without_its_value(tmp_path: Path) -> None:
    intake_documents(tmp_path, {}, password="short-but-secret")
    with pytest.raises(InvalidDocuments) as raised:
        Reader("intake", tmp_path, CONTRACTS).read()
    assert "short-but-secret" not in str(raised.value)
    assert "breaks pattern" in str(raised.value)


def test_database_secrets_without_the_web_map_password_are_refused(tmp_path: Path) -> None:
    write(
        tmp_path,
        {"status_url": STATUS_URL, "status_interval_s": 1},
        {"passwords": {"intake": PASSWORD}},
        secrets_contract=2,
    )
    with pytest.raises(InvalidDocuments, match="'web_map' is a required property"):
        Reader("database", tmp_path, CONTRACTS).read()


def test_database_secrets_of_the_first_contract_are_refused(tmp_path: Path) -> None:
    write(
        tmp_path,
        {"status_url": STATUS_URL, "status_interval_s": 1},
        {"passwords": {"intake": PASSWORD, "web_map": PASSWORD}},
    )
    with pytest.raises(InvalidDocuments, match="breaks const 2"):
        Reader("database", tmp_path, CONTRACTS).read()


def test_the_whole_volume_mounted_is_refused(tmp_path: Path) -> None:
    intake_documents(tmp_path, {})
    (tmp_path / "world-meadowfold-1a2b3c").mkdir()
    with pytest.raises(InvalidDocuments, match="other children's directories"):
        Reader("intake", tmp_path, CONTRACTS).read()
