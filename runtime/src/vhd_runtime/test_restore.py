import json
import os
import shutil
from collections import namedtuple
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from vhd_runtime import backups, restore, transfer
from vhd_runtime.restore import Place, RestoreFailed
from vhd_runtime.test_backups import CONTRACTS, NOW, RECORD, chunk_name, write_save
from vhd_runtime.transfer import Mounts

CHUNK = {"chunk": 258, "size": 64, "version": 3, "objects": 120}


@pytest.fixture
def place(tmp_path: Path) -> Place:
    place = Place(saves=tmp_path / "saves", vhd=tmp_path / "vhd", backups=tmp_path / "backups", world="Meadowfold")
    place.backups.mkdir()
    return place


def backup_of(place: Place, number: int = 3, chunk: dict[str, int] = CHUNK) -> str:
    """A backup of a synthesized save, taken as a running world takes one."""
    source = place.saves.parent / "elsewhere" / place.world
    write_save(source, number, [chunk])
    taken = backups.take(backups.Place(source, place.backups, place.world, RECORD), NOW)
    assert taken is not None
    shutil.rmtree(source)
    return taken.name


def edit_record(place: Place, name: str, edit: Callable[[dict[str, Any]], object]) -> None:
    path = place.backups / name / backups.RECORD
    record = json.loads(path.read_text())
    edit(record)
    path.write_text(json.dumps(record))


def test_a_staged_backup_is_its_complete_save_and_says_its_objects(place: Place) -> None:
    name = backup_of(place)
    assert restore.stage(place, name, RECORD) == 120
    save = backups.newest_complete(place.staged)
    assert save is not None and save.number == 3 and chunk_name(CHUNK) in save.files


def test_a_backup_whose_record_predates_its_source_is_staged(place: Place) -> None:
    name = backup_of(place)
    edit_record(place, name, lambda r: r.pop("source"))
    assert restore.stage(place, name, RECORD) == 120


def test_a_backup_that_is_gone_is_refused(place: Place) -> None:
    gone = r"^backup 20261118T194011Z no longer exists in the world's backups folder$"
    with pytest.raises(RestoreFailed, match=gone):
        restore.stage(place, "20261118T194011Z", RECORD)


def test_a_backup_of_another_world_is_refused(place: Place) -> None:
    name = backup_of(place)
    edit_record(place, name, lambda r: r.update(world="Ashwake"))
    with pytest.raises(RestoreFailed, match="not a backup of Meadowfold"):
        restore.stage(place, name, RECORD)
    assert not place.staged.exists()


@pytest.mark.parametrize(
    ("file", "reason"),
    [
        ("../escape", "breaks its contract at files/0/name"),
        ("_main.3.ok/../x", "breaks its contract at files/0/name"),
        ("notes.txt", "breaks its contract at files/0/name"),
        ("", "breaks its contract at files/0/name"),
        ("_main.3.ok\n", "lists a file that is no part of a save"),
    ],
)
def test_a_record_naming_a_file_outside_a_save_is_refused(place: Place, file: str, reason: str) -> None:
    name = backup_of(place)
    edit_record(place, name, lambda r: r["files"][0].update(name=file))
    with pytest.raises(RestoreFailed, match=reason):
        restore.stage(place, name, RECORD)
    assert not (place.vhd / "escape").exists() and not place.staged.exists()


def test_a_file_named_twice_is_refused(place: Place) -> None:
    name = backup_of(place)
    edit_record(place, name, lambda r: r["files"].append(r["files"][0]))
    with pytest.raises(RestoreFailed, match=f"^backup {name}'s record lists 01_02__64_3.chunk twice$"):
        restore.stage(place, name, RECORD)


@pytest.mark.parametrize(
    ("edit", "where"),
    [
        (lambda r: r.update(unknown=1), r"\(root\): Additional properties are not allowed"),
        (lambda r: r.pop("written_ns"), r"\(root\): 'written_ns' is a required property"),
        (lambda r: r.update(save=4294967296), "save: breaks maximum 4294967295"),
        (lambda r: r.update(written_ns=-1), "written_ns: breaks minimum 0"),
        (lambda r: r.update(objects="120"), 'objects: breaks type "integer"'),
        (lambda r: r.update(taken="2026-11-18 19:40:11"), "taken: breaks pattern"),
        (lambda r: r.update(source="elsewhere"), "source: breaks enum"),
        (lambda r: r.update(world="Meadow/fold"), "world: breaks pattern"),
        (lambda r: r["files"][0].update(sha256="0" * 63), "files/0/sha256: breaks pattern"),
        (lambda r: r["files"][0].update(size=-1), "files/0/size: breaks minimum 0"),
    ],
)
def test_a_restore_of_a_backup_whose_record_breaks_its_contract_fails_closed(
    place: Place, edit: Callable[[dict[str, Any]], object], where: str
) -> None:
    name = backup_of(place)
    edit_record(place, name, edit)
    with pytest.raises(RestoreFailed, match=f"^backup {name}'s record breaks its contract at {where}"):
        restore.stage(place, name, RECORD)
    assert not place.staged.exists()


@pytest.mark.parametrize("text", [None, "[]", '{"type": 5}'])
def test_a_records_contract_that_cannot_be_read_fails_closed(tmp_path: Path, text: str | None) -> None:
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    if text is not None:
        (contracts / restore.RECORD_CONTRACT).write_text(text)
    with pytest.raises(RestoreFailed, match=r"^cannot read the contract of a backup's record"):
        restore.record_contract(contracts)


def test_a_damaged_file_is_refused_and_nothing_stays_staged(place: Place) -> None:
    name = backup_of(place)
    (place.backups / name / "_main.3.db2").write_bytes(b"damaged")
    with pytest.raises(RestoreFailed, match="not the file its record lists"):
        restore.stage(place, name, RECORD)
    assert not place.staged.exists()


def test_a_file_missing_from_the_backup_is_refused(place: Place) -> None:
    name = backup_of(place)
    (place.backups / name / chunk_name(CHUNK)).unlink()
    with pytest.raises(RestoreFailed, match="lacks"):
        restore.stage(place, name, RECORD)


def test_a_record_that_leaves_out_a_file_of_its_save_is_refused(place: Place) -> None:
    name = backup_of(place)
    edit_record(place, name, lambda r: r.update(files=[f for f in r["files"] if not f["name"].endswith(".chunk")]))
    with pytest.raises(RestoreFailed, match="not a complete save"):
        restore.stage(place, name, RECORD)


def test_a_record_that_names_another_save_is_refused(place: Place) -> None:
    name = backup_of(place)
    edit_record(place, name, lambda r: r.update(save=4))
    with pytest.raises(RestoreFailed, match="does not hold the save its record names"):
        restore.stage(place, name, RECORD)


def test_a_record_that_cannot_be_read_is_refused(place: Place) -> None:
    name = backup_of(place)
    (place.backups / name / backups.RECORD).write_text("{")
    with pytest.raises(RestoreFailed, match="cannot be read"):
        restore.stage(place, name, RECORD)


def test_a_backup_the_data_volume_has_no_room_for_is_refused(place: Place, monkeypatch: pytest.MonkeyPatch) -> None:
    name = backup_of(place)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(0, 0, backups.RESERVE))
    with pytest.raises(RestoreFailed, match="must stay free"):
        restore.stage(place, name, RECORD)
    assert not place.staged.exists()


def test_a_swap_sets_the_save_aside_and_a_swap_back_returns_it(place: Place) -> None:
    write_save(place.save, 9, [{"chunk": 1, "size": 64, "version": 9, "objects": 5}])
    restore.stage(place, backup_of(place), RECORD)
    restore.swap(place, had_save=True)
    assert (place.save / "_main.3.ok").is_file() and (place.aside / "_main.9.ok").is_file()
    restore.unswap(place, had_save=True)
    assert (place.save / "_main.9.ok").is_file() and not place.aside.exists()
    restore.unswap(place, had_save=True)
    assert (place.save / "_main.9.ok").is_file()


def test_a_swap_cut_short_between_its_renames_is_finished(place: Place) -> None:
    write_save(place.save, 9, [{"chunk": 1, "size": 64, "version": 9, "objects": 5}])
    restore.stage(place, backup_of(place), RECORD)
    place.save.rename(place.aside)
    restore.swap(place, had_save=True)
    assert (place.save / "_main.3.ok").is_file() and (place.aside / "_main.9.ok").is_file()
    restore.swap(place, had_save=True)
    assert (place.save / "_main.3.ok").is_file()


def test_a_swap_back_cut_short_is_finished(place: Place) -> None:
    write_save(place.save, 9, [{"chunk": 1, "size": 64, "version": 9, "objects": 5}])
    restore.stage(place, backup_of(place), RECORD)
    restore.swap(place, had_save=True)
    shutil.rmtree(place.save)
    restore.unswap(place, had_save=True)
    assert (place.save / "_main.9.ok").is_file()


def test_a_world_that_never_started_gets_the_backup_and_loses_it_on_a_swap_back(place: Place) -> None:
    restore.stage(place, backup_of(place), RECORD)
    restore.swap(place, had_save=False)
    assert (place.save / "_main.3.ok").is_file() and not place.aside.exists()
    restore.unswap(place, had_save=False)
    assert not place.save.exists()


def test_finishing_records_the_outcome_and_forgets_the_intent(place: Place) -> None:
    intent = restore.Intent(id="a" * 32, backup="20261118T194011Z", phase="swapped", before=7, had_save=True)
    restore.write_intent(place, intent)
    assert restore.read_intent(place) == intent
    restore.finish(place, restore.Done(id="a" * 32, outcome="done"))
    assert restore.read_intent(place) is None
    assert restore.read_done(place) == restore.Done(id="a" * 32, outcome="done")


def test_a_record_of_another_shape_reads_as_none(place: Place) -> None:
    place.vhd.mkdir()
    place.intent.write_text(json.dumps({"id": "a" * 32, "unknown": 1}))
    place.done.write_text("[]")
    assert restore.read_intent(place) is None and restore.read_done(place) is None


def test_a_backup_file_that_is_a_link_is_refused_without_reading_through_it(place: Place) -> None:
    name = backup_of(place)
    secret = place.saves.parent / "secret"
    secret.write_bytes(b"db2 of save 3")
    (place.backups / name / "_main.3.db2").unlink()
    (place.backups / name / "_main.3.db2").symlink_to(secret)
    with pytest.raises(RestoreFailed, match="is a link"):
        restore.stage(place, name, RECORD)
    assert not place.staged.exists()


def test_a_backup_file_that_is_a_pipe_is_refused_without_waiting(place: Place) -> None:
    name = backup_of(place)
    (place.backups / name / "_main.3.db2").unlink()
    os.mkfifo(place.backups / name / "_main.3.db2")
    with pytest.raises(RestoreFailed, match="not a regular file"):
        restore.stage(place, name, RECORD)


def test_a_backup_file_larger_than_its_record_says_is_refused(place: Place) -> None:
    name = backup_of(place)
    with (place.backups / name / "_main.3.db2").open("ab") as appender:
        appender.write(b"x" * (2 << 20))
    with pytest.raises(RestoreFailed, match="larger than its record says"):
        restore.stage(place, name, RECORD)
    assert not place.staged.exists()


def test_a_record_longer_than_any_save_needs_cannot_be_read(place: Place) -> None:
    name = backup_of(place)
    (place.backups / name / backups.RECORD).write_text(" " * (restore.RECORD_MAX_BYTES + 1) + "{}")
    with pytest.raises(RestoreFailed, match="cannot be read"):
        restore.stage(place, name, RECORD)


def test_an_intent_with_an_id_status_would_refuse_reads_as_none(place: Place) -> None:
    restore.write_intent(place, restore.Intent(id="A" * 32, backup="20261118T194011Z", phase="swapped"))
    assert restore.read_intent(place) is None


def imported(place: Place) -> str:
    """A backup of a synthesized save, taken as vhd import takes one from the install folder."""
    mounts = Mounts(imported=place.saves.parent / "import", backups=place.backups, contracts=CONTRACTS)
    write_save(mounts.imported / place.world, 3, [CHUNK])
    result = transfer.import_save(mounts, place.world, lambda: NOW)
    shutil.rmtree(mounts.imported)
    return result["backup"]


def test_an_imported_backup_is_checked_and_staged_like_any_other(place: Place) -> None:
    name = imported(place)
    assert json.loads((place.backups / name / backups.RECORD).read_text())["source"] == "import"
    assert restore.stage(place, name, RECORD) == 120
    save = backups.newest_complete(place.staged)
    assert save is not None and save.number == 3 and chunk_name(CHUNK) in save.files


def link_db2(place: Place, name: str) -> None:
    secret = place.saves.parent / "secret"
    secret.write_bytes(b"db2 of save 3")
    (place.backups / name / "_main.3.db2").unlink()
    (place.backups / name / "_main.3.db2").symlink_to(secret)


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        (lambda place, name: (place.backups / name / "_main.3.db2").write_bytes(b"db2 of save 4"), "not the file"),
        (lambda place, name: (place.backups / name / chunk_name(CHUNK)).unlink(), "lacks"),
        (link_db2, "is a link"),
        (lambda place, name: edit_record(place, name, lambda r: r.update(world="Ashwake")), "not a backup of"),
        (lambda place, name: edit_record(place, name, lambda r: r.update(save=4)), "does not hold the save"),
        (
            lambda place, name: edit_record(place, name, lambda r: r.update(files=r["files"][1:])),
            "not a complete save",
        ),
    ],
)
def test_a_tampered_imported_backup_is_refused_and_nothing_stays_staged(
    place: Place, tamper: Callable[[Place, str], object], reason: str
) -> None:
    name = imported(place)
    tamper(place, name)
    with pytest.raises(RestoreFailed, match=reason):
        restore.stage(place, name, RECORD)
    assert not place.staged.exists()
