import errno
import hashlib
import json
import os
import re
import shutil
import stat
import struct
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from vhd_runtime import backups
from vhd_runtime.backups import BackupFailed, Place, take
from vhd_runtime.generation import Backups as Settings

SAVES = Path(__file__).resolve().parent / "saves"
NOW = datetime(2026, 11, 18, 20, 40, 12, tzinfo=UTC)
NOW_NAME = "20261118T204012Z"


def chunk_name(chunk: dict[str, int]) -> str:
    return f"{chunk['chunk'] >> 8:02x}_{chunk['chunk'] & 0xFF:02x}__{chunk['size']}_{chunk['version']}.chunk"


def write_save(
    folder: Path,
    number: int,
    chunks: Iterable[dict[str, int]],
    *,
    ok: bool = True,
    missing: Iterable[str] = (),
    seconds_ago: float = 0,
) -> None:
    """A synthesized 1.0+ save as the game leaves it: its chunk files, its .chunks file in the
    game's format, its .db2 and .fwl2, and, once complete, its .ok, each written seconds_ago."""
    folder.mkdir(parents=True, exist_ok=True)
    chunks = list(chunks)
    written: list[Path] = []
    for chunk in chunks:
        path = folder / chunk_name(chunk)
        path.write_bytes(f"zdos of {chunk_name(chunk)}".encode())
        written.append(path)
    header = struct.pack("<hii", 42, sum(c["objects"] for c in chunks), len(chunks))
    entries = b"".join(struct.pack("<HBIi", c["chunk"], c["size"], c["version"], c["objects"]) for c in chunks)
    (folder / f"_main.{number}.chunks").write_bytes(header + entries)
    written.append(folder / f"_main.{number}.chunks")
    for kind in ("db2", "fwl2") + (("ok",) if ok else ()):
        path = folder / f"_main.{number}.{kind}"
        path.write_bytes(f"{kind} of save {number}".encode())
        written.append(path)
    stamp = NOW.timestamp() - seconds_ago
    for path in written:
        os.utime(path, (stamp, stamp))
    for name in missing:
        (folder / name).unlink()


def case(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((SAVES / name).read_text())
    return document


def build(folder: Path, document: dict[str, Any]) -> None:
    for save in document["saves"]:
        write_save(
            folder,
            save["number"],
            save["chunks"],
            ok=save["ok"],
            missing=save.get("missing", ()),
            seconds_ago=save["seconds_ago"],
        )


@pytest.mark.parametrize("name", sorted(p.name for p in SAVES.glob("*.json")))
def test_each_synthesized_save_is_backed_up_as_its_case_says(tmp_path: Path, name: str) -> None:
    document = case(name)
    folder, store = tmp_path / "worlds_local" / "Meadowfold", tmp_path / "backups"
    folder.mkdir(parents=True)
    store.mkdir()
    build(folder, document)
    expected = document["newest_complete"]

    if "failure" in document:
        with pytest.raises(BackupFailed, match=f"^{re.escape(document['failure'])}$"):
            take(folder, store, "Meadowfold", NOW)
        assert list(store.iterdir()) == []
        return
    backup = take(folder, store, "Meadowfold", NOW)
    if expected is None:
        assert backup is None
        assert list(store.iterdir()) == []
        return
    assert backup is not None
    assert (backup.name, backup.saved[0]) == (NOW_NAME, expected["save"])
    assert sorted(p.name for p in (store / NOW_NAME).iterdir()) == sorted([*expected["files"], "backup.json"])
    record = json.loads((store / NOW_NAME / "backup.json").read_text())
    assert record["world"] == "Meadowfold"
    assert record["save"] == expected["save"]
    assert record["objects"] == expected["objects"]
    assert record["written_ns"] == backup.saved[1] == (folder / f"_main.{expected['save']}.ok").stat().st_mtime_ns
    assert record["taken"] == "2026-11-18T20:40:12Z"
    for file in record["files"]:
        data = (folder / file["name"]).read_bytes()
        assert file["size"] == len(data)
        assert file["sha256"] == hashlib.sha256(data).hexdigest()


def test_the_save_backed_up_last_is_not_backed_up_again_but_one_of_its_number_written_since_is(
    tmp_path: Path,
) -> None:
    folder, store = tmp_path / "world", tmp_path / "backups"
    store.mkdir()
    write_save(folder, 7, [{"chunk": 1, "size": 64, "version": 1, "objects": 5}])
    save = backups.newest_complete(folder)
    assert save is not None
    assert take(folder, store, "Meadowfold", NOW, last=save.saved) is None
    assert list(store.iterdir()) == []
    # As after a restore: the game numbers its saves on from the restored one's.
    backup = take(folder, store, "Meadowfold", NOW, last=(7, save.written_ns - 1))
    assert backup is not None and backup.saved == save.saved


def test_a_copy_that_loses_a_file_to_the_next_save_takes_that_save_instead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, store = tmp_path / "world", tmp_path / "backups"
    store.mkdir()
    old = {"chunk": 1, "size": 64, "version": 1, "objects": 5}
    write_save(folder, 7, [old], seconds_ago=60)
    copy_file = backups.copy_file
    saved = False

    def next_save_completes(source: Path, target: Path) -> dict[str, object]:
        nonlocal saved
        if not saved:
            # Save 8 completes while save 7 is copied, and the game removes save 7's chunk.
            saved = True
            write_save(folder, 8, [{"chunk": 1, "size": 64, "version": 2, "objects": 6}])
            (folder / chunk_name(old)).unlink()
        return copy_file(source, target)

    monkeypatch.setattr(backups, "copy_file", next_save_completes)
    backup = take(folder, store, "Meadowfold", NOW)
    assert backup is not None and (backup.name, backup.saved[0]) == (NOW_NAME, 8)
    assert [p.name for p in store.iterdir()] == [NOW_NAME]


def test_a_world_saving_through_every_copy_fails_and_keeps_no_partial_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, store = tmp_path / "world", tmp_path / "backups"
    store.mkdir()
    write_save(folder, 7, [{"chunk": 1, "size": 64, "version": 1, "objects": 5}])

    def gone(source: Path, target: Path) -> dict[str, object]:
        raise backups.Gone(source.name)

    monkeypatch.setattr(backups, "copy_file", gone)
    with pytest.raises(BackupFailed, match="the world saved again during each copy"):
        take(folder, store, "Meadowfold", NOW)
    assert list(store.iterdir()) == []


def test_a_backup_it_cannot_write_fails_with_the_reason_and_is_no_backup(tmp_path: Path) -> None:
    folder, store = tmp_path / "world", tmp_path / "backups"
    store.mkdir()
    write_save(folder, 7, [{"chunk": 1, "size": 64, "version": 1, "objects": 5}])
    # A file where the copy's folder goes: the tests run as root, which no permission stops.
    (store / f".partial-{NOW_NAME}").write_text("in the way")
    with pytest.raises(BackupFailed, match=f"^cannot write backup {NOW_NAME} into {store}: File exists$"):
        take(folder, store, "Meadowfold", NOW)
    assert backups.names(store, NOW) == []


@pytest.mark.parametrize(
    "data", [b"", struct.pack("<hii", 41, 0, 0), struct.pack("<hii", 42, 5, 1), struct.pack("<hii", 42, 5, 0) + b"x"]
)
def test_a_chunks_file_in_another_format_fails_closed(tmp_path: Path, data: bytes) -> None:
    folder, store = tmp_path / "world", tmp_path / "backups"
    store.mkdir()
    write_save(folder, 7, [])
    (folder / "_main.7.chunks").write_bytes(data)
    with pytest.raises(BackupFailed, match=r"^save 7's \.chunks file is not in the format this runtime reads$"):
        take(folder, store, "Meadowfold", NOW)


def held(store: Path, *times: datetime) -> None:
    for when in times:
        backup = store / when.strftime(backups.NAME_FORMAT)
        backup.mkdir()
        (backup / backups.RECORD).write_text("{}")


def test_prune_keeps_the_newest_and_the_newest_of_each_day(tmp_path: Path) -> None:
    store = tmp_path / "backups"
    store.mkdir()
    # Three a day for five days, the newest at 18:00 on the 18th.
    times = [datetime(2026, 11, day, hour, tzinfo=UTC) for day in range(14, 19) for hour in (6, 12, 18)]
    held(store, *times)
    (store / "notes.txt").write_text("the server manager's own")

    left, failure = backups.prune(store, keep=2, keep_daily=3, now=NOW)

    assert failure is None
    assert left == ["20261118T180000Z", "20261118T120000Z", "20261117T180000Z", "20261116T180000Z"]
    assert backups.names(store, NOW) == left
    assert (store / "notes.txt").exists()


def test_prune_never_removes_the_newest(tmp_path: Path) -> None:
    store = tmp_path / "backups"
    store.mkdir()
    held(store, NOW - timedelta(hours=1), NOW)
    assert backups.prune(store, keep=1, keep_daily=0, now=NOW) == ([NOW_NAME], None)


def test_a_folder_without_its_record_is_no_backup(tmp_path: Path) -> None:
    store = tmp_path / "backups"
    store.mkdir()
    (store / NOW_NAME).mkdir()
    assert backups.names(store, NOW) == []
    assert backups.prune(store, keep=1, keep_daily=0, now=NOW) == ([], None)
    assert (store / NOW_NAME).exists()


def test_copies_a_stop_cut_short_are_cleared(tmp_path: Path) -> None:
    store = tmp_path / "backups"
    store.mkdir()
    (store / f".partial-{NOW_NAME}").mkdir()
    held(store, NOW)
    backups.clear_partials(store)
    assert [p.name for p in store.iterdir()] == [NOW_NAME]


def test_a_backup_says_when_it_was_taken_and_which_save_it_holds(tmp_path: Path) -> None:
    folder, store = tmp_path / "world", tmp_path / "backups"
    store.mkdir()
    write_save(folder, 4294967295, [{"chunk": 1, "size": 64, "version": 1, "objects": 5}])
    backup = take(folder, store, "Meadowfold", NOW)
    assert backup is not None
    assert backups.taken(NOW_NAME) == NOW
    assert backups.last_saved(store, NOW_NAME) == backup.saved
    assert backup.saved[0] == 4294967295
    assert backups.last_saved(store, "20261118T000000Z") is None


def test_a_folder_named_for_a_time_that_does_not_exist_is_no_backup(tmp_path: Path) -> None:
    store = tmp_path / "backups"
    store.mkdir()
    held(store, NOW)
    (store / "99999999T999999Z").mkdir()
    (store / "99999999T999999Z" / backups.RECORD).write_text("{}")
    assert backups.names(store, NOW) == [NOW_NAME]


def test_after_the_clock_goes_back_a_new_backup_still_comes_newest(tmp_path: Path) -> None:
    folder, store = tmp_path / "world", tmp_path / "backups"
    store.mkdir()
    ahead = NOW + timedelta(hours=2)
    held(store, ahead)
    write_save(folder, 7, [{"chunk": 1, "size": 64, "version": 1, "objects": 5}])
    backup = take(folder, store, "Meadowfold", NOW)
    assert backup is not None
    assert backup.name == (ahead + timedelta(seconds=1)).strftime(backups.NAME_FORMAT)
    assert backups.names(store, NOW)[0] == backup.name


def test_a_backup_the_folder_has_no_room_for_is_not_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder, store = tmp_path / "world", tmp_path / "backups"
    store.mkdir()
    write_save(folder, 7, [{"chunk": 1, "size": 64, "version": 1, "objects": 5}])
    usage = shutil.disk_usage(store)
    # Room for the save, but not for the reserve the worlds and the database keep.
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage._replace(free=backups.RESERVE))
    with pytest.raises(BackupFailed, match=r"has 1024 MiB free; save 7 needs 0 MiB, and 1024 MiB must stay free$"):
        take(folder, store, "Meadowfold", NOW)
    assert list(store.iterdir()) == []


def test_a_file_the_next_save_removes_before_the_room_is_reckoned_starts_the_copy_again(tmp_path: Path) -> None:
    folder = tmp_path / "world"
    write_save(folder, 7, [{"chunk": 1, "size": 64, "version": 1, "objects": 5}])
    save = backups.newest_complete(folder)
    assert save is not None
    (folder / "00_01__64_1.chunk").unlink()
    with pytest.raises(backups.Gone):
        backups._room(folder, save, tmp_path)


def test_a_file_system_that_cannot_sync_a_directory_still_takes_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, store = tmp_path / "world", tmp_path / "backups"
    store.mkdir()
    write_save(folder, 7, [{"chunk": 1, "size": 64, "version": 1, "objects": 5}])
    fsync = os.fsync

    def no_directories(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "Invalid argument")
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", no_directories)
    backup = take(folder, store, "Meadowfold", NOW)
    assert backup is not None and backups.names(store, NOW) == [backup.name]


def test_a_backup_named_well_after_now_is_left_alone_and_named_in_the_failure(tmp_path: Path) -> None:
    place = Place(tmp_path / "world", tmp_path / "backups", "Meadowfold")
    place.backups.mkdir()
    held(place.backups, datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC))
    write_save(place.saves, 7, [{"chunk": 1, "size": 64, "version": 1, "objects": 5}])

    tended = backups.tend(place, Settings(every_s=3600, keep=1, keep_daily=0), NOW, None, starting=True)

    assert tended.backup is not None and tended.backup.name == NOW_NAME
    assert tended.names == [NOW_NAME]
    assert tended.failure == (
        f"{place.backups} holds 99991231T235959Z, named for after now, which the runtime leaves alone as no backup"
    )
    assert (place.backups / "99991231T235959Z").exists()


def test_a_save_folder_that_cannot_be_read_fails_the_take_by_name_and_still_prunes(tmp_path: Path) -> None:
    place = Place(tmp_path / "world", tmp_path / "backups", "Meadowfold")
    place.backups.mkdir()
    held(place.backups, NOW - timedelta(days=1), NOW - timedelta(hours=1))
    write_save(place.saves, 7, [])
    (place.saves / "_main.7.chunks").unlink()
    (place.saves / "_main.7.chunks").mkdir()

    tended = backups.tend(place, Settings(every_s=3600, keep=1, keep_daily=0), NOW, None)

    assert tended.take_failed
    assert tended.failure is not None and "_main.7.chunks" in tended.failure
    assert len(tended.names) == 1


def test_a_backup_that_cannot_be_removed_stays_listed_and_says_why(tmp_path: Path) -> None:
    store = tmp_path / "backups"
    store.mkdir()
    old, new = NOW - timedelta(days=1), NOW
    held(store, old, new)
    # Where the old one would be moved aside: a folder in the way.
    blocked = store / f".partial-{old.strftime(backups.NAME_FORMAT)}"
    blocked.mkdir()
    (blocked / "file").write_text("in the way")

    left, failure = backups.prune(store, keep=1, keep_daily=0, now=NOW)
    assert left == [NOW_NAME, old.strftime(backups.NAME_FORMAT)]
    assert failure is not None and failure.startswith(f"cannot remove backup {old.strftime(backups.NAME_FORMAT)}")


def test_a_removal_cut_short_leaves_no_backup_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "backups"
    store.mkdir()
    held(store, NOW - timedelta(days=1), NOW)

    def cut_short(path: Path, *args: object, **kwargs: object) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(shutil, "rmtree", cut_short)
    left, failure = backups.prune(store, keep=1, keep_daily=0, now=NOW)
    assert left == [NOW_NAME]
    assert failure is not None and "Input/output error" in failure


def test_a_starting_turn_clears_cut_short_copies_and_backs_up_a_save_no_backup_holds(tmp_path: Path) -> None:
    place = Place(tmp_path / "world", tmp_path / "backups", "Meadowfold")
    place.backups.mkdir()
    write_save(place.saves, 5, [{"chunk": 1, "size": 64, "version": 5, "objects": 5}], seconds_ago=3600)
    first = take(place.saves, place.backups, "Meadowfold", NOW - timedelta(hours=1))
    assert first is not None
    # The game stopped with save 6, and the stop never backed it up.
    write_save(place.saves, 6, [{"chunk": 1, "size": 64, "version": 6, "objects": 6}])
    (place.backups / ".partial-20261118T204000Z").mkdir()

    tended = backups.tend(place, Settings(every_s=3600, keep=24, keep_daily=14), NOW, None, starting=True)

    assert tended.backup is not None and tended.backup.saved[0] == 6
    assert tended.names == [NOW_NAME, first.name]
    assert tended.newest == tended.backup.saved
    assert (tended.failure, tended.take_failed) == (None, False)
    assert not (place.backups / ".partial-20261118T204000Z").exists()
    again = backups.tend(place, Settings(every_s=3600, keep=24, keep_daily=14), NOW, None, starting=True)
    assert again.backup is None and again.newest == tended.backup.saved
