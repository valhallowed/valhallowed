import re
from pathlib import Path

import pytest

from vhd_control.manifest import InvalidSettings, Settings, World, load
from vhd_control.naming import child_id
from vhd_control.reconcile import STATUS_URL, reconcile
from vhd_control.schemas import Schemas

REPO = Path(__file__).resolve().parents[3]
SCHEMAS = Schemas(REPO)


def settings(*worlds: World) -> Settings:
    return Settings(worlds=worlds, web_map_port=8456)


@pytest.mark.parametrize("example", sorted((REPO / "schema" / "examples" / "valid").glob("manifest-*.toml")))
def test_every_valid_manifest_reconciles_into_documents_runtimes_accept(tmp_path: Path, example: Path) -> None:
    (tmp_path / "manifest.toml").write_text(example.read_text())
    generation = reconcile(load(tmp_path, SCHEMAS), None)

    assert generation.number == 1
    for child in generation.children.values():
        document, secrets = child.numbered(generation.number)
        assert SCHEMAS.problem("contracts/game-runtime.generation.schema.json", document) is None
        assert SCHEMAS.problem("contracts/game-runtime.secrets.schema.json", secrets) is None


def test_a_password_reaches_only_its_world_secrets() -> None:
    generation = reconcile(settings(World("Ashwake", 2459, None), World("Meadowfold", 2456, "hearth-and-horn")), None)
    secrets = {c.world: c.secrets for c in generation.children.values()}
    assert secrets == {"Ashwake": {"contract": 1}, "Meadowfold": {"contract": 1, "password": "hearth-and-horn"}}
    assert all(c.generation["status_url"] == STATUS_URL for c in generation.children.values())


def test_unchanged_settings_keep_the_generation() -> None:
    first = reconcile(settings(World("Meadowfold", 2456, None)), None)
    assert reconcile(settings(World("Meadowfold", 2456, None)), first) is first


def test_a_change_to_any_world_moves_the_install_one_generation() -> None:
    first = reconcile(settings(World("Ashwake", 2459, None), World("Meadowfold", 2456, None)), None)
    second = reconcile(settings(World("Ashwake", 2459, None), World("Meadowfold", 2456, "hearth-and-horn")), first)
    assert second.number == 2
    ashwake = child_id("Ashwake")
    assert second.children[ashwake] == first.children[ashwake]


@pytest.mark.parametrize("ports", [(2456, 2456), (2456, 2458), (2458, 2456)])
def test_overlapping_world_ports_are_refused(ports: tuple[int, int]) -> None:
    with pytest.raises(InvalidSettings, match="share ports"):
        reconcile(settings(World("Ashwake", ports[0], None), World("Meadowfold", ports[1], None)), None)


def test_adjacent_world_ports_are_accepted() -> None:
    generation = reconcile(settings(World("Ashwake", 2456, None), World("Meadowfold", 2459, None)), None)
    assert len(generation.children) == 2


def test_child_ids_are_compose_names_that_never_collide() -> None:
    names = ["Meadowfold", "meadowfold", "Meadow fold", "Meadow_fold", "9 Worlds"]
    ids = [child_id(n) for n in names]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9-]*", i) for i in ids)
    assert child_id("Meadowfold") == child_id("Meadowfold")
