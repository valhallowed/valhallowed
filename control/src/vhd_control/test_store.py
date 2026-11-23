import fcntl
import json
import os
import threading
import time
from pathlib import Path

import pytest

from vhd_control.manifest import World
from vhd_control.naming import child_id
from vhd_control.reconcile import Restore, reconcile
from vhd_control.store import Busy, State, Store
from vhd_control.test_reconcile import settings

MEADOWFOLD = child_id("Meadowfold")


def test_hand_writes_the_pair_a_runtime_reads(tmp_path: Path) -> None:
    store = Store(tmp_path)
    generation = reconcile(settings(World("Meadowfold", 2456, "hearth-and-horn")), None)
    state = State()

    store.hand(state, MEADOWFOLD, generation.children[MEADOWFOLD], 7)

    directory = tmp_path / MEADOWFOLD / "2456"
    document = json.loads((directory / "generation.json").read_text())
    secrets = json.loads((directory / "secrets.json").read_text())
    assert document["generation"] == secrets["generation"] == 7
    assert secrets["password"] == "hearth-and-horn"
    assert sorted(p.name for p in directory.iterdir()) == ["generation.json", "secrets.json"]
    assert (directory / "secrets.json").stat().st_mode & 0o777 == 0o644
    assert state.handed[MEADOWFOLD].number == 7


def test_state_survives_a_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path)
    generation = reconcile(settings(World("Meadowfold", 2456, None)), None)
    state = State(generation=generation, applied={MEADOWFOLD: 2456})
    store.hand(state, MEADOWFOLD, generation.children[MEADOWFOLD], generation.number)

    store.save(state)

    assert store.load() == state
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600


def test_state_from_before_the_web_map_lists_services_without_ports(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.save(State())
    document = json.loads((tmp_path / "state.json").read_text())
    document["services_applied"] = ["database", "intake"]
    (tmp_path / "state.json").write_text(json.dumps(document))

    assert store.load().services_applied == {"database": None, "intake": None}


def test_state_from_before_the_web_map_mounts_map_exports_holds_none(tmp_path: Path) -> None:
    store = Store(tmp_path)
    generation = reconcile(settings(World("Meadowfold", 2456, None)), None)
    store.save(State(generation=generation))
    document = json.loads((tmp_path / "state.json").read_text())
    del document["mounts_applied"]
    for service in document["generation"]["services"].values():
        del service["mounts"]
    (tmp_path / "state.json").write_text(json.dumps(document))

    state = store.load()
    assert state.mounts_applied == {}
    assert state.generation is not None
    assert state.generation.services["web-map"].mounts == ()


def test_no_state_yet(tmp_path: Path) -> None:
    assert Store(tmp_path).load() == State()


def test_the_lock_can_be_taken_again_once_released(tmp_path: Path) -> None:
    store = Store(tmp_path)
    with store.locked():
        pass
    with store.locked():
        pass


def test_a_leftover_temporary_file_never_lends_state_its_mode(tmp_path: Path) -> None:
    store = Store(tmp_path, owner=(os.getuid(), os.getgid()))
    leftover = tmp_path / ".state.json.tmp"
    leftover.write_text("{}")
    leftover.chmod(0o666)

    store.save(State())

    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
    assert not leftover.exists()
    assert (tmp_path / "state.json").stat().st_uid == os.getuid()


def test_prune_keeps_only_the_directories_containers_mount(tmp_path: Path) -> None:
    store = Store(tmp_path)
    generation = reconcile(settings(World("Meadowfold", 2456, None), World("Ashwake", 2459, None)), None)
    state = State()
    for cid, child in generation.children.items():
        store.hand(state, cid, child, 1)
    (tmp_path / MEADOWFOLD / "2462").mkdir()
    store.save(state)

    store.prune({MEADOWFOLD: 2456}, removed={child_id("Ashwake")})

    assert {p.name for p in tmp_path.iterdir()} == {MEADOWFOLD, "state.json"}
    assert [p.name for p in (tmp_path / MEADOWFOLD).iterdir()] == ["2456"]


def test_the_backups_folder_and_what_vhd_apply_mounted_survive_a_reload(tmp_path: Path) -> None:
    store = Store(tmp_path)
    generation = reconcile(settings(World("Meadowfold", 2456, None)), None)
    store.save(State(generation=generation, backups_applied="backups"))

    state = store.load()
    assert state.generation == generation
    assert state.generation is not None and state.generation.backups == "backups"
    assert state.backups_applied == "backups"


def test_a_state_from_before_backups_names_no_folder_so_the_next_pass_replaces_it(tmp_path: Path) -> None:
    store = Store(tmp_path)
    generation = reconcile(settings(World("Meadowfold", 2456, None)), None)
    store.save(State(generation=generation))
    document = json.loads((tmp_path / "state.json").read_text())
    del document["generation"]["backups"], document["backups_applied"]
    (tmp_path / "state.json").write_text(json.dumps(document))

    state = store.load()
    assert state.generation is not None and state.generation.backups == ""
    assert state.backups_applied is None
    assert reconcile(settings(World("Meadowfold", 2456, None)), state.generation).number == generation.number + 1


def test_restores_survive_a_reload(tmp_path: Path) -> None:
    store = Store(tmp_path)
    restores = {MEADOWFOLD: Restore("0" * 32, "20261118T194011Z")}
    store.save(State(restores=restores))
    assert store.load().restores == restores


def test_a_state_from_before_restores_holds_none(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.save(State())
    document = json.loads((tmp_path / "state.json").read_text())
    del document["restores"]
    (tmp_path / "state.json").write_text(json.dumps(document))
    assert store.load().restores == {}


def test_a_lock_held_past_the_wait_raises_busy(tmp_path: Path) -> None:
    store = Store(tmp_path)
    # flock locks belong to the open file description, so this second one conflicts in-process.
    with (tmp_path / ".lock").open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(Busy), store.locked(0.2):
            pytest.fail("the lock was taken while another holder had it")
        assert 0.15 <= time.monotonic() - started < 5


def test_a_lock_the_holder_releases_within_the_wait_is_taken(tmp_path: Path) -> None:
    store = Store(tmp_path)
    holder = (tmp_path / ".lock").open("a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    releaser = threading.Timer(0.2, holder.close)
    releaser.start()
    try:
        with store.locked(5):
            pass
    finally:
        releaser.join(5)


def test_a_free_lock_is_taken_with_a_wait(tmp_path: Path) -> None:
    with Store(tmp_path).locked(0.2):
        pass
