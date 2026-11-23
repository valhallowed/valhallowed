"""A world's restore: one of its backups swapped in for its save before the game starts, and swapped
back out if world truth fails it (Game runtimes).

Each step is recorded before it is taken, so a restore that a stop or a crash cut short is carried
forward or back at the next start, and the world is never left without its save:

    /data/vhd/restore.json          the restore under way: its id, its backup, its phase, whether the
                                    world had a save, and what the runtime remembered of the world
    /data/vhd/restore-done.json     the newest restore finished: its id, outcome and reason
    /data/vhd/restore/staged/       the backup's files, copied and checked, until the swap
    /data/vhd/restore/aside/        the save the restore replaced, until world truth passes

Its phases, in order: staging, swapping, swapped (the game may run; world truth has not judged the
restored save), then judged on a pass, or unswapping on a fail or when a newer generation drops or
replaces the restore first, and the done record last. Everything here is in the data volume,
beside the saves, so each swap is a rename. A record may carry keys a later runtime adds.
"""

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast, get_args

from jsonschema import Draft202012Validator

from vhd_runtime import backups, generation

Phase = Literal["staging", "swapping", "swapped", "judged", "unswapping"]
# How a restore ends.
Finished = Literal["done", "failed", "rolled-back"]
_PHASES = get_args(Phase)
_FINISHED = get_args(Finished)
_INTENT_KEYS = {"id", "backup", "phase", "before", "had_save", "objects", "reason"}
_DONE_KEYS = {"id", "outcome", "reason"}

# The files a 1.0+ save is made of (Game facts): the save's own four, and its chunks.
SAVE_FILE = re.compile(r"_main\.[0-9]+\.(chunks|db2|fwl2|ok)|[0-9a-f]{2}_[0-9a-f]{2}__[0-9]+_[0-9]+\.chunk")
_ID = re.compile(r"[0-9a-f]{32}")
# A record lists a save's few thousand files at most; anything longer is no record of ours.
RECORD_MAX_BYTES = 4 << 20
# The contract a backup's record keeps, in the image's contracts folder.
RECORD_CONTRACT = "game-runtime.backup.schema.json"


class RestoreFailed(Exception):
    """The restore was refused before it touched the world's save. The message says why, in one line."""


@dataclass(frozen=True)
class Place:
    """Where a world's restore works: its saves, VHD's own files in the data volume, its backups,
    and its save name."""

    saves: Path
    vhd: Path
    backups: Path
    world: str

    @property
    def save(self) -> Path:
        return backups.world_folder(self.saves, self.world)

    @property
    def legacy_save(self) -> Path:
        """A pre-1.0 save: a single file beside the 1.0+ folders (Game facts)."""
        return self.saves / "worlds_local" / f"{self.world}.db"

    @property
    def staged(self) -> Path:
        return self.vhd / "restore" / "staged"

    @property
    def aside(self) -> Path:
        return self.vhd / "restore" / "aside"

    @property
    def intent(self) -> Path:
        return self.vhd / "restore.json"

    @property
    def done(self) -> Path:
        return self.vhd / "restore-done.json"


@dataclass(frozen=True)
class Intent:
    id: str
    backup: str
    phase: Phase
    # The count the runtime remembered of the world before the restore, put back on a swap back.
    before: int | None = None
    # Whether the world had a save to set aside; one that never started has none.
    had_save: bool = False
    # The objects the restored save holds, as its .chunks file counts them.
    objects: int | None = None
    # Why the restore went the way it did, for status.
    reason: str | None = None


@dataclass(frozen=True)
class Done:
    id: str
    outcome: Finished
    reason: str | None = None


@dataclass(frozen=True)
class Record:
    """What a backup's record says of its save: its number, when the game completed it, and each
    of its files."""

    number: int
    written_ns: int
    files: list[dict[str, Any]]


def read_intent(place: Place) -> Intent | None:
    """The restore under way, or None. A record missing a key, or with a bad value, is none: only this
    runtime writes it."""
    record = _read(place.intent)
    if (
        record is None
        or not _INTENT_KEYS <= set(record)
        or not _is_id(record["id"])
        or not (isinstance(record["backup"], str) and backups.is_name(record["backup"]))
        or record["phase"] not in _PHASES
        or not _count(record["before"])
        or type(record["had_save"]) is not bool
        or not _count(record["objects"])
        or not (record["reason"] is None or isinstance(record["reason"], str))
    ):
        return None
    return Intent(
        id=cast(str, record["id"]),
        backup=record["backup"],
        phase=cast(Phase, record["phase"]),
        before=cast(int | None, record["before"]),
        had_save=record["had_save"],
        objects=cast(int | None, record["objects"]),
        reason=record["reason"],
    )


def write_intent(place: Place, intent: Intent) -> None:
    _write(place.intent, asdict(intent))


def read_done(place: Place) -> Done | None:
    """The newest restore finished, or None."""
    record = _read(place.done)
    if (
        record is None
        or not _DONE_KEYS <= set(record)
        or not _is_id(record["id"])
        or record["outcome"] not in _FINISHED
        or not (record["reason"] is None or isinstance(record["reason"], str))
    ):
        return None
    return Done(id=cast(str, record["id"]), outcome=cast(Finished, record["outcome"]), reason=record["reason"])


def finish(place: Place, done: Done) -> None:
    """Record the restore's outcome, then forget the restore under way: the done record goes first,
    so a crash between the two leaves the restore finished, not undone."""
    _write(place.done, asdict(done))
    place.intent.unlink(missing_ok=True)
    backups.sync(place.vhd)


def clear_staged(place: Place) -> None:
    shutil.rmtree(place.staged, ignore_errors=True)


def stage(place: Place, name: str, contracts: Path) -> int:
    """Copy the backup into the data volume and check it: its record keeps its contract, in the
    contracts folder, and names this world, each file has the size and SHA-256 the record gives,
    and the copy is a complete save in the format this runtime reads. Returns the objects the save
    holds. Raises RestoreFailed, leaving nothing staged."""
    source = place.backups / name
    if not (source / backups.RECORD).is_file():
        raise RestoreFailed(f"backup {name} no longer exists in {place.backups}")
    record = read_record(source, place.world, name, record_contract(contracts))
    clear_staged(place)
    try:
        place.staged.mkdir(parents=True)
        needed = sum(file["size"] for file in record.files)
        free = shutil.disk_usage(place.staged).free
        if free - needed < backups.RESERVE:
            raise RestoreFailed(
                f"the data volume has {free // backups.MIB} MiB free; backup {name} needs {needed // backups.MIB} MiB, "
                f"and {backups.RESERVE // backups.MIB} MiB must stay free"
            )
        objects = copy_checked(source, place.staged, name, record.number, record.files)
        backups.sync(place.staged)
    except RestoreFailed:
        clear_staged(place)
        raise
    except OSError as error:
        clear_staged(place)
        raise RestoreFailed(f"cannot copy backup {name} into the data volume: {error.strerror or error}") from error
    return objects


def copy_checked(source: Path, target: Path, name: str, number: int, files: list[dict[str, Any]]) -> int:
    """Copy the files a backup's record lists into target, and check them: each has the size and
    SHA-256 the record gives, and together they are the complete save the record names. Returns the
    objects the save holds. Raises RestoreFailed, or OSError when a file cannot be copied; either
    leaves target for the caller to remove."""
    for file in files:
        try:
            copied = backups.copy_file(source / file["name"], target / file["name"], file["size"])
        except backups.Gone as error:
            raise RestoreFailed(f"backup {name} lacks {file['name']}, which its record lists") from error
        if copied["size"] != file["size"] or copied["sha256"] != file["sha256"]:
            raise RestoreFailed(f"backup {name}'s {file['name']} is not the file its record lists")
    try:
        save = backups.newest_complete(target)
    except backups.BackupFailed as error:
        raise RestoreFailed(f"backup {name} is not a complete save: {error}") from error
    if save is None or save.number != number or set(save.files) != {file["name"] for file in files}:
        raise RestoreFailed(f"backup {name} does not hold the save its record names")
    return save.objects


def swap(place: Place, had_save: bool) -> None:
    """Set the world's save aside and put the staged backup in its place. Safe to repeat: a swap cut
    short between its two renames is finished."""
    if not place.staged.exists():
        return
    if had_save and place.save.exists():
        place.save.rename(place.aside)
    place.save.parent.mkdir(parents=True, exist_ok=True)
    place.staged.rename(place.save)
    backups.sync(place.save.parent)
    backups.sync(place.aside.parent)


def unswap(place: Place, had_save: bool) -> None:
    """Put back the save the restore replaced, or, for a world that had none, remove the restored one.
    Safe to repeat: with nothing aside, a save that had one is already back."""
    if had_save:
        if not place.aside.exists():
            return
        shutil.rmtree(place.save, ignore_errors=True)
        place.aside.rename(place.save)
    else:
        shutil.rmtree(place.save, ignore_errors=True)
    backups.sync(place.save.parent)


def drop_aside(place: Place) -> None:
    """Forget the save the restore replaced, once the restored one passed world truth."""
    shutil.rmtree(place.aside, ignore_errors=True)


def record_contract(contracts: Path) -> Draft202012Validator:
    """The contract a backup's record keeps, from the contracts folder. Raises RestoreFailed when it
    cannot be read."""
    try:
        return generation.validator(contracts / RECORD_CONTRACT)
    except (OSError, ValueError) as error:
        raise RestoreFailed(f"cannot read the contract of a backup's record: {error}") from error


def read_record(source: Path, world: str, name: str, contract: Draft202012Validator) -> Record:
    """The save a backup's record names, once the record keeps its contract, names this world and
    lists each file once. Raises RestoreFailed."""
    record = _read(source / backups.RECORD)
    if record is None:
        raise RestoreFailed(f"backup {name}'s record cannot be read")
    breach = generation.breach(contract, record)
    if breach is not None:
        raise RestoreFailed(f"backup {name}'s record breaks its contract {breach}")
    if record["world"] != world:
        raise RestoreFailed(f"backup {name} is not a backup of {world}")
    files = cast(list[dict[str, Any]], record["files"])
    seen: set[str] = set()
    for file in files:
        # The contract's pattern also lets a name end in a newline, as Python's $ matches before one.
        if not SAVE_FILE.fullmatch(file["name"]):
            raise RestoreFailed(f"backup {name}'s record lists a file that is no part of a save")
        if file["name"] in seen:
            raise RestoreFailed(f"backup {name}'s record lists {file['name']} twice")
        seen.add(file["name"])
    return Record(cast(int, record["save"]), cast(int, record["written_ns"]), files)


def _is_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _count(value: object) -> bool:
    return value is None or (type(value) is int and value >= 0)


def _read(path: Path) -> dict[str, object] | None:
    """A JSON object of at most RECORD_MAX_BYTES, read without following a link, or None."""
    try:
        with backups.open_regular(path) as reader:
            data = reader.read(RECORD_MAX_BYTES + 1)
        record = json.loads(data) if len(data) <= RECORD_MAX_BYTES else None
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _write(path: Path, record: dict[str, object]) -> None:
    """Write whole and synced, then move into place and sync the move, so a reader never sees half
    a record, and the record holds before the step it guards is taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as writer:
        writer.write(json.dumps(record))
        writer.flush()
        os.fsync(writer.fileno())
    temporary.replace(path)
    backups.sync(path.parent)
