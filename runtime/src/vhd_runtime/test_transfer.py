import dataclasses
import errno
import hashlib
import io
import json
import os
import shutil
import signal
from collections import namedtuple
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from vhd_runtime import backups, transfer
from vhd_runtime.generation import validator
from vhd_runtime.test_backups import CONTRACTS, NOW, NOW_NAME, RECORD, chunk_name, staging, write_save
from vhd_runtime.transfer import Mounts

RESULT = validator(CONTRACTS / "game-runtime.import-export.schema.json")
WORLD = "Meadowfold"
CHUNKS = [
    {"chunk": 258, "size": 64, "version": 3, "objects": 1200},
    {"chunk": 4660, "size": 32, "version": 7, "objects": 34},
]


def make_mounts(root: Path) -> Mounts:
    """The folders vhd mounts for an action: the import folder, the world's backups, an empty export folder."""
    mounts = Mounts(imported=root / "import", backups=root / "backups", exported=root / "export", contracts=CONTRACTS)
    for folder in (mounts.imported, mounts.backups, mounts.exported):
        folder.mkdir()
    return mounts


@pytest.fixture
def mounts(tmp_path: Path) -> Mounts:
    return make_mounts(tmp_path)


def put_save(mounts: Mounts, number: int = 41, chunks: list[dict[str, int]] = CHUNKS, **kwargs: Any) -> Path:
    """A synthesized 1.0+ save, put in the import folder as vhd mounts it: its folder, under its own name."""
    folder = mounts.imported / WORLD
    write_save(folder, number, chunks, **kwargs)
    return folder


def act(mounts: Mounts, capsys: pytest.CaptureFixture[str], now: datetime = NOW) -> tuple[int, dict[str, Any]]:
    """Import, as python -m vhd_runtime import does: the exit code and the result, its last line."""
    code = transfer.run("import", WORLD, CONTRACTS, lambda: transfer.import_save(mounts, WORLD, lambda: now))
    return code, result_of(capsys)


def export(mounts: Mounts, capsys: pytest.CaptureFixture[str], backup: str | None = None) -> tuple[int, dict[str, Any]]:
    """Export, as python -m vhd_runtime export does: the exit code and the result, its last line."""
    code = transfer.run("export", WORLD, CONTRACTS, lambda: transfer.export_backup(mounts, WORLD, backup, lambda: NOW))
    return code, result_of(capsys)


def result_of(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    lines = capsys.readouterr().out.splitlines()
    result: dict[str, Any] = json.loads(lines[-1])
    # vhd counts a result that breaks the contract as refused, so none may.
    RESULT.validate(result)
    return result


def kept(mounts: Mounts) -> list[str]:
    """What the backups folder holds."""
    return sorted(entry.name for entry in mounts.backups.iterdir())


def test_an_import_keeps_the_save_as_a_backup_of_its_world(mounts: Mounts, capsys: pytest.CaptureFixture[str]) -> None:
    folder = put_save(mounts, seconds_ago=3600)

    code, result = act(mounts, capsys)

    assert code == 0
    assert result == {
        "contract": 1,
        "action": "import",
        "world": WORLD,
        "outcome": "done",
        "backup": NOW_NAME,
        "save": {"number": 41, "objects": 1234, "written": "2026-11-18T19:40:12Z"},
    }
    assert backups.names(mounts.backups, NOW) == [NOW_NAME] and kept(mounts) == [NOW_NAME]
    backup = mounts.backups / NOW_NAME
    record = json.loads((backup / backups.RECORD).read_text())
    RECORD.validate(record)
    assert record["source"] == "import"
    assert (record["world"], record["save"], record["objects"]) == (WORLD, 41, 1234)
    assert record["written_ns"] == (folder / "_main.41.ok").stat().st_mtime_ns
    assert record["taken"] == "2026-11-18T20:40:12Z"
    names = sorted(entry.name for entry in folder.iterdir())
    assert sorted(file["name"] for file in record["files"]) == names
    assert sorted(entry.name for entry in backup.iterdir()) == sorted([*names, backups.RECORD])
    for file in record["files"]:
        data = (folder / file["name"]).read_bytes()
        assert (backup / file["name"]).read_bytes() == data
        assert (file["size"], file["sha256"]) == (len(data), hashlib.sha256(data).hexdigest())
    assert backups.last_saved(mounts.backups, NOW_NAME) == (41, record["written_ns"])


def test_an_import_comes_newest_after_the_runtimes_own_backups(
    mounts: Mounts, capsys: pytest.CaptureFixture[str]
) -> None:
    then = mounts.backups.parent / "then" / WORLD
    write_save(then, 7, [CHUNKS[0]])
    own = backups.take(then, mounts.backups, WORLD, NOW + timedelta(hours=1))
    assert own is not None
    put_save(mounts)

    code, result = act(mounts, capsys)

    assert code == 0 and result["backup"] == "20261118T214013Z"
    assert backups.names(mounts.backups, NOW) == ["20261118T214013Z", own.name]


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (
            lambda root: None,
            "the install folder holds nothing; it must hold exactly one entry, the save folder Meadowfold",
        ),
        (
            lambda root: [(root / WORLD).mkdir(), (root / "Ashwake").mkdir()],
            "the install folder holds Ashwake, Meadowfold; it must hold exactly one entry, the save folder Meadowfold",
        ),
        (lambda root: (root / "Ashwake").mkdir(), "the install folder holds Ashwake, not the save folder Meadowfold"),
        (lambda root: (root / WORLD).write_bytes(b"x"), "the install folder holds Meadowfold as a file, not a folder"),
        (
            lambda root: [(root.parent / "real").mkdir(), (root / WORLD).symlink_to(root.parent / "real")],
            "the install folder holds Meadowfold as a link, not a folder",
        ),
        (
            lambda root: [(root / f"{WORLD}.db").write_bytes(b"x"), (root / f"{WORLD}.fwl").write_bytes(b"x")],
            "the install folder holds Meadowfold.db, a pre-1.0 save; an import takes a 1.0+ save only",
        ),
        (
            lambda root: [(root / WORLD).mkdir(), (root / "Thumbs.db").write_bytes(b"x")],
            "the install folder holds Meadowfold, Thumbs.db; it must hold exactly one entry, "
            "the save folder Meadowfold",
        ),
    ],
)
def test_an_import_folder_that_is_not_exactly_the_worlds_save_folder_is_refused(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], make: Any, reason: str
) -> None:
    make(mounts.imported)
    code, result = act(mounts, capsys)
    assert (code, result["outcome"], result["reason"]) == (1, "refused", reason)
    assert kept(mounts) == []


def test_a_save_file_that_is_a_link_is_refused_without_reading_through_it(
    mounts: Mounts, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = put_save(mounts)
    secret = mounts.imported.parent / "secret"
    secret.write_bytes(b"not the game's")
    (folder / chunk_name(CHUNKS[0])).unlink()
    (folder / chunk_name(CHUNKS[0])).symlink_to(secret)
    code, result = act(mounts, capsys)
    assert code == 1
    assert result["reason"] == "the save holds 01_02__64_3.chunk as a link, and an import takes regular files only"
    assert kept(mounts) == []


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda folder: (folder / "backups").mkdir(), "the save holds backups as a folder, and an import takes"),
        (lambda folder: os.mkfifo(folder / "_main.40.db2"), "the save holds _main.40.db2 as a special file, and an"),
        (lambda folder: (folder / "notes.txt").write_text("x"), "the save holds notes.txt, which is no file of a save"),
        (lambda folder: (folder / "Meadowfold.db").write_text("x"), "the save holds Meadowfold.db, a pre-1.0 save's"),
        (lambda folder: (folder / "Meadowfold.fwl").write_text("x"), "the save holds Meadowfold.fwl, a pre-1.0 save's"),
        (lambda folder: (folder / "Thumbs.db").write_text("x"), "the save holds Thumbs.db, which is no file of a save"),
        (lambda folder: (folder / "Ashwake.fwl").write_text("x"), "the save holds Ashwake.fwl, which is no file of a"),
    ],
)
def test_a_save_folder_holding_anything_but_a_saves_files_is_refused(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], make: Any, reason: str
) -> None:
    make(put_save(mounts))
    code, result = act(mounts, capsys)
    assert code == 1 and result["reason"].startswith(reason)
    assert kept(mounts) == []


def test_a_folder_with_more_than_one_complete_save_is_refused_and_says_why(
    mounts: Mounts, capsys: pytest.CaptureFixture[str]
) -> None:
    put_save(mounts, 40, [CHUNKS[1]], seconds_ago=1800)
    put_save(mounts, 41)
    code, result = act(mounts, capsys)
    assert code == 1
    assert result["reason"] == (
        "the save folder holds 2 complete saves (40, 41); the game picks the newest by modification time, "
        "which a copy may have lost, so an import takes exactly one"
    )
    assert kept(mounts) == []


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        (("_main.41.ok",), "the save folder holds no complete save: no _main.<n>.ok"),
        (("_main.41.db2",), "save 41 lacks _main.41.db2"),
        ((chunk_name(CHUNKS[1]),), "save 41 lacks 12_34__32_7.chunk, which its .chunks file names"),
    ],
)
def test_a_save_that_is_not_complete_is_refused(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], missing: tuple[str, ...], reason: str
) -> None:
    put_save(mounts, missing=missing)
    code, result = act(mounts, capsys)
    assert (code, result["reason"]) == (1, reason)
    assert kept(mounts) == []


def test_a_save_numbered_above_any_saves_number_is_refused(mounts: Mounts, capsys: pytest.CaptureFixture[str]) -> None:
    put_save(mounts, 99999999999)
    code, result = act(mounts, capsys)
    assert (code, result["reason"]) == (
        1,
        "the save folder holds _main.99999999999.ok, numbered above any save's 4294967295",
    )
    assert kept(mounts) == []


def test_a_save_completed_before_1970_is_refused(mounts: Mounts, capsys: pytest.CaptureFixture[str]) -> None:
    folder = put_save(mounts)
    os.utime(folder / "_main.41.ok", ns=(-(10**9), -(10**9)))
    code, result = act(mounts, capsys)
    assert (code, result["reason"]) == (1, "the save's _main.41.ok was last modified before 1970, which no save is")
    assert kept(mounts) == []


def test_a_record_that_would_break_its_contract_is_never_named_as_a_backup(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put_save(mounts)
    newest_complete = backups.newest_complete

    def counts_below_zero(folder: Path) -> backups.Save | None:
        save = newest_complete(folder)
        return None if save is None else dataclasses.replace(save, objects=-1)

    monkeypatch.setattr(backups, "newest_complete", counts_below_zero)
    code, result = act(mounts, capsys)
    assert (code, result["reason"]) == (1, "the backup's record breaks its contract at objects: breaks minimum 0")
    assert kept(mounts) == []


def test_a_result_that_would_break_its_contract_is_printed_as_refused(capsys: pytest.CaptureFixture[str]) -> None:
    def done_without_its_save() -> transfer.Result:
        return {"contract": 1, "action": "import", "world": WORLD, "outcome": "done", "backup": NOW_NAME}

    code = transfer.run("import", WORLD, CONTRACTS, done_without_its_save)
    result = result_of(capsys)
    assert (code, result["outcome"]) == (1, "refused")
    assert result["reason"].startswith("import's result breaks its contract at (root): ")


def test_an_action_without_its_results_contract_is_refused_before_it_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def never() -> transfer.Result:
        raise AssertionError("acted without the result's contract")

    code = transfer.run("export", WORLD, tmp_path, never)
    result = result_of(capsys)
    assert code == 1 and result["reason"].startswith("export cannot read its result's contract")


def test_a_file_of_another_save_is_refused(mounts: Mounts, capsys: pytest.CaptureFixture[str]) -> None:
    folder = put_save(mounts)
    # A chunk an older save wrote, which the newer one's .chunks file does not name.
    (folder / "01_02__64_2.chunk").write_bytes(b"older")
    code, result = act(mounts, capsys)
    assert (code, result["reason"]) == (1, "the save folder holds 01_02__64_2.chunk, which is no file of save 41")
    assert kept(mounts) == []


def test_a_chunks_file_in_another_format_is_refused(mounts: Mounts, capsys: pytest.CaptureFixture[str]) -> None:
    (put_save(mounts) / "_main.41.chunks").write_bytes(b"\x2a")
    code, result = act(mounts, capsys)
    assert (code, result["reason"]) == (1, "save 41's .chunks file is not in the format this runtime reads")


def test_a_save_larger_than_an_import_takes_is_refused_before_anything_is_copied(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = put_save(mounts)
    # Sparse: the size is there, the disk space is not used.
    os.truncate(folder / "_main.41.db2", transfer.IMPORT_MAX_BYTES)

    def never(*args: object) -> dict[str, object]:
        raise AssertionError("copied a save larger than an import takes")

    monkeypatch.setattr(backups, "copy_file", never)
    code, result = act(mounts, capsys)
    assert code == 1
    assert result["reason"] == "the save holds 4097 MiB, more than the 4096 MiB an import takes"
    assert kept(mounts) == []


def test_a_save_the_backup_folder_has_no_room_for_is_refused(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put_save(mounts)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(0, 0, backups.RESERVE))
    code, result = act(mounts, capsys)
    assert code == 1 and result["reason"].endswith("and 1024 MiB must stay free")
    assert kept(mounts) == []


def test_a_copy_that_fails_removes_only_what_this_import_made(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put_save(mounts)
    other = mounts.backups / f"{backups.IMPORT_STAGING}{'0' * 32}"
    other.mkdir()
    copy_file = backups.copy_file
    copies = 0

    def fails_second(source: Path, target: Path, limit: int | None = None, **kwargs: bool) -> dict[str, object]:
        nonlocal copies
        copies += 1
        if copies == 2:
            raise OSError(errno.EIO, "Input/output error")
        return copy_file(source, target, limit, **kwargs)

    monkeypatch.setattr(backups, "copy_file", fails_second)
    code, result = act(mounts, capsys)
    assert (code, result["reason"]) == (1, "cannot copy the save into the world's backups folder: Input/output error")
    assert kept(mounts) == [other.name]


def test_a_file_that_grows_while_it_is_copied_is_refused(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = put_save(mounts)
    copy_file = backups.copy_file

    def grows(source: Path, target: Path, limit: int | None = None, **kwargs: bool) -> dict[str, object]:
        if source.name == "_main.41.db2":
            with source.open("ab") as appender:
                appender.write(b"more")
        return copy_file(source, target, limit, **kwargs)

    monkeypatch.setattr(backups, "copy_file", grows)
    code, result = act(mounts, capsys)
    assert (code, result["reason"]) == (1, "the save's _main.41.db2 grew while it was copied")
    assert kept(mounts) == []
    assert (folder / "_main.41.db2").exists()


def test_an_import_clears_the_staging_a_kill_cut_short_and_spares_any_an_import_holds(
    mounts: Mounts, capsys: pytest.CaptureFixture[str]
) -> None:
    put_save(mounts)
    cut_short = staging(mounts.backups, backups.IMPORT_STAGING_MAX_AGE_S + 1)
    under_way = staging(mounts.backups, backups.IMPORT_STAGING_MAX_AGE_S + 2)
    recent = staging(mounts.backups, backups.IMPORT_STAGING_MAX_AGE_S - 60)
    held = backups.lock_folder(under_way)
    try:
        code, _ = act(mounts, capsys)
    finally:
        os.close(held)
    assert code == 0 and not cut_short.exists()
    assert kept(mounts) == sorted([NOW_NAME, under_way.name, recent.name])


def test_an_import_holds_its_staging_until_it_is_named_so_no_cleanup_removes_it(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put_save(mounts)
    copy_file = backups.copy_file
    held: list[str] = []

    def cleared_meanwhile(source: Path, target: Path, limit: int | None = None, **kwargs: bool) -> dict[str, object]:
        # A start of the world's runtime well after the age, as the copy of a large save may outlast it.
        backups.clear_import_staging(mounts.backups, datetime.now(UTC) + timedelta(hours=2))
        assert target.parent.is_dir()
        with pytest.raises(BlockingIOError):
            backups.lock_folder(target.parent)
        held.append(target.parent.name)
        return copy_file(source, target, limit, **kwargs)

    monkeypatch.setattr(backups, "copy_file", cleared_meanwhile)
    code, _ = act(mounts, capsys)
    assert code == 0 and len(held) == len(CHUNKS) + 4 and kept(mounts) == [NOW_NAME]
    # Released once named.
    os.close(backups.lock_folder(mounts.backups / NOW_NAME))


class WrittenAsRead:
    """A reader of the save's file that something writes to, in place, once its copy has begun."""

    def __init__(self, reader: io.BufferedReader, write: Callable[[], None]) -> None:
        self.reader, self.write = reader, write

    def __enter__(self) -> "WrittenAsRead":
        return self

    def __exit__(self, *exc: object) -> None:
        self.reader.close()

    def fileno(self) -> int:
        return self.reader.fileno()

    def read(self, size: int) -> bytes:
        block = self.reader.read(size)
        self.write()
        self.write = lambda: None
        return block


@pytest.mark.parametrize(
    "write",
    [
        # Rewritten in place to the same size: only its modification time tells.
        lambda path: os.utime(path, ns=(path.stat().st_mtime_ns + 10**9,) * 2),
        lambda path: path.write_bytes(path.read_bytes()[::-1]),
        lambda path: os.truncate(path, 0),
    ],
)
def test_a_file_that_changes_while_it_is_copied_is_refused(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, write: Callable[[Path], None]
) -> None:
    folder = put_save(mounts)
    open_regular = backups.open_regular

    def written_as_read(source: Path) -> WrittenAsRead:
        reader = open_regular(source)
        return WrittenAsRead(reader, (lambda: write(source)) if source.name == "_main.41.db2" else lambda: None)

    monkeypatch.setattr(backups, "open_regular", written_as_read)
    code, result = act(mounts, capsys)
    assert (code, result["reason"]) == (1, "the save's _main.41.db2 changed while it was copied")
    assert kept(mounts) == []
    assert (folder / "_main.41.db2").exists()


def test_a_name_the_runtimes_own_backup_takes_meanwhile_is_named_again(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put_save(mounts)
    name_time = backups.name_time
    raced: list[str] = []

    def runtime_takes_it(store: Path, now: datetime) -> datetime:
        when = name_time(store, now)
        if not raced:
            # The runtime's turn renames its own copy into place under the same name first.
            name = when.strftime(backups.NAME_FORMAT)
            (store / name).mkdir()
            (store / name / backups.RECORD).write_text("{}")
            raced.append(name)
        return when

    monkeypatch.setattr(backups, "name_time", runtime_takes_it)
    code, result = act(mounts, capsys)
    assert code == 0 and raced == [NOW_NAME]
    assert result["backup"] == "20261118T204013Z"
    record = json.loads((mounts.backups / "20261118T204013Z" / backups.RECORD).read_text())
    assert record["taken"] == "2026-11-18T20:40:13Z"
    assert kept(mounts) == [NOW_NAME, "20261118T204013Z"]


def test_an_import_whose_every_name_is_taken_meanwhile_is_refused(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put_save(mounts)
    name_time = backups.name_time

    def runtime_takes_each(store: Path, now: datetime) -> datetime:
        when = name_time(store, now)
        (store / when.strftime(backups.NAME_FORMAT)).mkdir()
        (store / when.strftime(backups.NAME_FORMAT) / backups.RECORD).write_text("{}")
        return when

    monkeypatch.setattr(backups, "name_time", runtime_takes_each)
    code, result = act(mounts, capsys)
    assert code == 1 and result["reason"].startswith("the runtime's own backups took each name the import tried")
    assert not any(name.startswith(backups.IMPORT_STAGING) for name in kept(mounts))
    assert len(kept(mounts)) == backups.NAME_ATTEMPTS


def test_an_unexpected_error_is_refused_with_a_one_line_reason_as_the_last_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def breaks() -> transfer.Result:
        raise RuntimeError("first line\nsecond\x1b[31m line " + "x" * 600)

    code = transfer.run("import", WORLD, CONTRACTS, breaks)
    captured = capsys.readouterr()
    result = json.loads(captured.out.splitlines()[-1])
    RESULT.validate(result)
    assert code == 1 and result["outcome"] == "refused"
    assert result["reason"].startswith(
        "import stopped on an unexpected error: RuntimeError: first line second [31m line"
    )
    assert len(result["reason"]) == transfer.REASON_MAX
    assert "Traceback" not in captured.out


def signalled_on_copy(signum: int, copy: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """vhd, or Docker on its behalf, sending signum to the action as it copies its copy-th file."""
    copy_file = backups.copy_file
    copies = 0

    def signalled(source: Path, target: Path, limit: int | None = None, **kwargs: bool) -> dict[str, object]:
        nonlocal copies
        copies += 1
        if copies == copy:
            signal.raise_signal(signum)
        return copy_file(source, target, limit, **kwargs)

    monkeypatch.setattr(backups, "copy_file", signalled)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_an_import_a_signal_stops_removes_what_it_wrote_and_says_so_last(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, signum: signal.Signals
) -> None:
    put_save(mounts)
    before = {each: signal.getsignal(each) for each in transfer.STOP_SIGNALS}
    signalled_on_copy(signum, 2, monkeypatch)
    code, result = act(mounts, capsys)
    assert (code, result["reason"]) == (1, f"import stopped by {signum.name} before it finished")
    assert kept(mounts) == []
    assert {each: signal.getsignal(each) for each in transfer.STOP_SIGNALS} == before


def test_an_export_a_signal_stops_removes_what_it_wrote(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_of(mounts, 41, NOW)
    signalled_on_copy(signal.SIGTERM, 2, monkeypatch)
    code, result = export(mounts, capsys)
    assert (code, result["reason"]) == (1, "export stopped by SIGTERM before it finished")
    assert list(mounts.exported.iterdir()) == []


def test_an_interrupt_still_ends_with_a_result_line(capsys: pytest.CaptureFixture[str]) -> None:
    def interrupted() -> transfer.Result:
        raise KeyboardInterrupt

    code = transfer.run("import", WORLD, CONTRACTS, interrupted)
    result = result_of(capsys)
    assert code == 1 and result["reason"] == "import stopped on an unexpected error: KeyboardInterrupt"


def test_an_exit_is_not_taken_for_a_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    def exits() -> transfer.Result:
        raise SystemExit(3)

    with pytest.raises(SystemExit):
        transfer.run("import", WORLD, CONTRACTS, exits)
    assert capsys.readouterr().out == ""


def test_a_save_name_is_what_the_manifest_schema_takes_for_one() -> None:
    schema = json.loads((CONTRACTS.parent / "schema" / "manifest.schema.json").read_text())
    assert f"^{transfer._SAVE_NAME.pattern}$" == schema["$defs"]["save_name"]["pattern"]


def test_the_result_says_when_the_game_completed_the_save_in_utc_to_the_second() -> None:
    written_ns = int(datetime(2026, 11, 18, 20, 40, 0, tzinfo=UTC).timestamp()) * 10**9 + 999_999_999
    result = transfer._done("import", WORLD, NOW_NAME, transfer._summary(4294967295, 0, written_ns))
    RESULT.validate(result)
    assert result["save"] == {"number": 4294967295, "objects": 0, "written": "2026-11-18T20:40:00Z"}


def backup_of(mounts: Mounts, number: int, when: datetime) -> str:
    """A backup the runtime took of save number."""
    then = mounts.backups.parent / "then" / WORLD
    write_save(then, number, CHUNKS)
    taken = backups.take(then, mounts.backups, WORLD, when)
    assert taken is not None
    shutil.rmtree(then.parent)
    return taken.name


def exported(mounts: Mounts) -> Path:
    return mounts.exported / "worlds_local" / WORLD


def same_files(one: Path, other: Path) -> bool:
    files = sorted(entry.name for entry in one.iterdir())
    return files == sorted(entry.name for entry in other.iterdir()) and all(
        (one / name).read_bytes() == (other / name).read_bytes() for name in files
    )


def test_an_export_copies_the_newest_backup_in_the_layout_the_game_reads(
    mounts: Mounts, capsys: pytest.CaptureFixture[str]
) -> None:
    backup_of(mounts, 40, NOW - timedelta(hours=1))
    newest = backup_of(mounts, 41, NOW)

    code, result = export(mounts, capsys)

    record = json.loads((mounts.backups / newest / backups.RECORD).read_text())
    written = datetime.fromtimestamp(record["written_ns"] // 10**9, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert code == 0
    assert result == {
        "contract": 1,
        "action": "export",
        "world": WORLD,
        "outcome": "done",
        "backup": newest,
        "save": {"number": 41, "objects": 1234, "written": written},
    }
    assert [entry.name for entry in mounts.exported.iterdir()] == ["worlds_local"]
    save = backups.newest_complete(exported(mounts))
    assert save is not None and save.number == 41
    assert sorted(entry.name for entry in exported(mounts).iterdir()) == sorted(f["name"] for f in record["files"])
    for file in record["files"]:
        assert (exported(mounts) / file["name"]).read_bytes() == (mounts.backups / newest / file["name"]).read_bytes()


def test_an_export_copies_the_backup_it_names(mounts: Mounts, capsys: pytest.CaptureFixture[str]) -> None:
    older = backup_of(mounts, 40, NOW - timedelta(hours=1))
    backup_of(mounts, 41, NOW)
    code, result = export(mounts, capsys, older)
    assert code == 0 and result["backup"] == older and result["save"]["number"] == 40
    assert (exported(mounts) / "_main.40.ok").is_file()


def test_an_imported_save_exports_as_it_was_put_in_the_install_folder(
    mounts: Mounts, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = put_save(mounts)
    assert act(mounts, capsys)[0] == 0
    code, result = export(mounts, capsys)
    assert code == 0 and result["backup"] == NOW_NAME
    assert same_files(folder, exported(mounts))


def test_a_backup_whose_record_predates_its_source_exports(mounts: Mounts, capsys: pytest.CaptureFixture[str]) -> None:
    name = backup_of(mounts, 41, NOW)
    path = mounts.backups / name / backups.RECORD
    record = json.loads(path.read_text())
    del record["source"]
    path.write_text(json.dumps(record))
    assert export(mounts, capsys)[0] == 0


@pytest.mark.parametrize(
    ("backup", "reason"),
    [
        (None, "the world's backups folder holds no backup of Meadowfold"),
        ("20261118T100000Z", "backup 20261118T100000Z does not exist in the world's backups folder"),
        ("../import", "backup ../import does not exist in the world's backups folder"),
        (".import-00000000000000000000000000000000", "does not exist in the world's backups folder"),
    ],
)
def test_a_backup_that_is_not_there_is_refused(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], backup: str | None, reason: str
) -> None:
    (mounts.backups / ".import-00000000000000000000000000000000").mkdir()
    code, result = export(mounts, capsys, backup)
    assert code == 1 and result["reason"].endswith(reason)
    assert list(mounts.exported.iterdir()) == []


def test_a_backup_of_another_world_is_refused(mounts: Mounts, capsys: pytest.CaptureFixture[str]) -> None:
    name = backup_of(mounts, 41, NOW)
    path = mounts.backups / name / backups.RECORD
    path.write_text(json.dumps({**json.loads(path.read_text()), "world": "Ashwake"}))
    code, result = export(mounts, capsys)
    assert (code, result["reason"]) == (1, f"backup {name} is not a backup of Meadowfold")
    assert list(mounts.exported.iterdir()) == []


@pytest.mark.parametrize("written_ns", [-1, "1763498412000000000", None])
def test_a_backup_whose_record_says_no_time_the_save_was_completed_is_refused(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], written_ns: object
) -> None:
    name = backup_of(mounts, 41, NOW)
    path = mounts.backups / name / backups.RECORD
    path.write_text(json.dumps({**json.loads(path.read_text()), "written_ns": written_ns}))
    code, result = export(mounts, capsys)
    assert code == 1 and result["reason"].startswith(f"backup {name}'s record breaks its contract at written_ns")
    assert list(mounts.exported.iterdir()) == []


@pytest.mark.parametrize(
    "make",
    [
        lambda layout: layout.write_text("the server manager's"),
        lambda layout: layout.symlink_to(layout.parent.parent / "elsewhere", target_is_directory=True),
    ],
)
def test_an_export_refuses_a_worlds_local_that_is_no_folder_and_leaves_it(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], make: Callable[[Path], object]
) -> None:
    backup_of(mounts, 41, NOW)
    layout = mounts.exported / "worlds_local"
    make(layout)
    code, result = export(mounts, capsys)
    assert (code, result["reason"]) == (1, "the install folder holds worlds_local, and it is no folder")
    assert [entry.name for entry in mounts.exported.iterdir()] == ["worlds_local"] and os.path.lexists(layout)


def full_at_the_save_folder(mounts: Mounts, monkeypatch: pytest.MonkeyPatch) -> None:
    """The install folder's disk filling up as the export makes the world's save folder."""
    mkdir = Path.mkdir

    def full(self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        if self == exported(mounts):
            raise OSError(errno.ENOSPC, "No space left on device")
        mkdir(self, mode, parents, exist_ok)

    monkeypatch.setattr(Path, "mkdir", full)


def test_an_export_that_cannot_make_the_save_folder_removes_the_worlds_local_it_made(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_of(mounts, 41, NOW)
    full_at_the_save_folder(mounts, monkeypatch)
    code, result = export(mounts, capsys)
    assert (code, result["reason"]) == (1, "cannot write into the install folder: No space left on device")
    assert list(mounts.exported.iterdir()) == []


def test_an_export_that_cannot_make_the_save_folder_leaves_a_worlds_local_it_did_not_make(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_of(mounts, 41, NOW)
    other = mounts.exported / "worlds_local" / "Ashwake"
    other.mkdir(parents=True)
    full_at_the_save_folder(mounts, monkeypatch)
    code, _ = export(mounts, capsys)
    assert code == 1 and [entry.name for entry in other.parent.iterdir()] == ["Ashwake"]


def test_an_export_never_writes_over_a_save_already_in_the_export_folder(
    mounts: Mounts, capsys: pytest.CaptureFixture[str]
) -> None:
    backup_of(mounts, 41, NOW)
    exported(mounts).mkdir(parents=True)
    (exported(mounts) / "_main.9.ok").write_text("the server manager's")
    code, result = export(mounts, capsys)
    assert (code, result["reason"]) == (1, "the install folder already holds worlds_local/Meadowfold")
    assert [entry.name for entry in exported(mounts).iterdir()] == ["_main.9.ok"]


def test_an_export_the_install_folders_disk_has_no_room_for_is_refused_before_anything_is_written(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    name = backup_of(mounts, 41, NOW)
    needed = sum(file["size"] for file in json.loads((mounts.backups / name / backups.RECORD).read_text())["files"])
    usage = namedtuple("usage", "total used free")
    asked: list[Path] = []

    def short_by_one(path: Path) -> object:
        asked.append(Path(path))
        return usage(0, 0, backups.RESERVE + needed - 1)

    def never(*args: object) -> dict[str, object]:
        raise AssertionError("copied a backup the export folder has no room for")

    monkeypatch.setattr(shutil, "disk_usage", short_by_one)
    monkeypatch.setattr(backups, "copy_file", never)
    code, result = export(mounts, capsys)
    assert code == 1
    assert result["reason"] == (
        f"the install folder has 1024 MiB free; backup {name} needs 0 MiB, and 1024 MiB must stay free"
    )
    assert asked == [mounts.exported]
    assert list(mounts.exported.iterdir()) == []


def test_an_export_that_leaves_exactly_the_reserve_free_goes_ahead(
    mounts: Mounts, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    name = backup_of(mounts, 41, NOW)
    needed = sum(file["size"] for file in json.loads((mounts.backups / name / backups.RECORD).read_text())["files"])
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(0, 0, backups.RESERVE + needed))
    code, result = export(mounts, capsys)
    assert code == 0 and result["backup"] == name
    assert (exported(mounts) / "_main.41.ok").is_file()


def test_a_damaged_backup_is_refused_and_leaves_the_export_folder_as_it_was(
    mounts: Mounts, capsys: pytest.CaptureFixture[str]
) -> None:
    name = backup_of(mounts, 41, NOW)
    (mounts.backups / name / "_main.41.db2").write_bytes(b"damaged")
    code, result = export(mounts, capsys)
    assert (code, result["reason"]) == (1, f"backup {name}'s _main.41.db2 is not the file its record lists")
    assert list(mounts.exported.iterdir()) == []


def test_a_failed_export_removes_only_what_it_wrote(mounts: Mounts, capsys: pytest.CaptureFixture[str]) -> None:
    name = backup_of(mounts, 41, NOW)
    other = mounts.exported / "worlds_local" / "Ashwake"
    other.mkdir(parents=True)
    (mounts.backups / name / chunk_name(CHUNKS[0])).unlink()
    code, result = export(mounts, capsys)
    assert (code, result["reason"]) == (1, f"backup {name} lacks 01_02__64_3.chunk, which its record lists")
    assert [entry.name for entry in (mounts.exported / "worlds_local").iterdir()] == ["Ashwake"]


def test_a_backup_file_that_is_a_link_is_refused_without_reading_through_it(
    mounts: Mounts, capsys: pytest.CaptureFixture[str]
) -> None:
    name = backup_of(mounts, 41, NOW)
    secret = mounts.backups.parent / "secret"
    secret.write_bytes(b"db2 of save 41")
    (mounts.backups / name / "_main.41.db2").unlink()
    (mounts.backups / name / "_main.41.db2").symlink_to(secret)
    code, result = export(mounts, capsys)
    assert (code, result["reason"]) == (1, f"cannot copy backup {name} into the install folder: _main.41.db2 is a link")
    assert list(mounts.exported.iterdir()) == []
