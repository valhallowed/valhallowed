import json
from pathlib import Path

from vhd_control.manifest import Settings, World
from vhd_control.naming import child_id
from vhd_control.reconcile import reconcile
from vhd_control.store import State, Store

MEADOWFOLD = child_id("Meadowfold")


def test_hand_writes_the_pair_a_runtime_reads(tmp_path: Path) -> None:
    store = Store(tmp_path)
    generation = reconcile(Settings((World("Meadowfold", 2456, "hearth-and-horn"),), 8456), None)
    state = State()

    store.hand(state, MEADOWFOLD, generation.children[MEADOWFOLD], 7)

    directory = tmp_path / MEADOWFOLD
    document = json.loads((directory / "generation.json").read_text())
    secrets = json.loads((directory / "secrets.json").read_text())
    assert document["generation"] == secrets["generation"] == 7
    assert secrets["password"] == "hearth-and-horn"
    assert sorted(p.name for p in directory.iterdir()) == ["generation.json", "secrets.json"]
    assert (directory / "secrets.json").stat().st_mode & 0o777 == 0o644
    assert state.handed[MEADOWFOLD].number == 7


def test_state_survives_a_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path)
    generation = reconcile(Settings((World("Meadowfold", 2456, None),), 8456), None)
    state = State(generation=generation, applied={MEADOWFOLD: 2456})
    store.hand(state, MEADOWFOLD, generation.children[MEADOWFOLD], generation.number)

    store.save(state)

    assert store.load() == state
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600


def test_no_state_yet(tmp_path: Path) -> None:
    assert Store(tmp_path).load() == State()


def test_the_lock_can_be_taken_again_once_released(tmp_path: Path) -> None:
    store = Store(tmp_path)
    with store.locked():
        pass
    with store.locked():
        pass
