"""A world's backups: copies of its newest complete save, taken while the game runs (Game runtimes).

A 1.0+ save is complete once its _main.<n>.ok exists, and the game never rewrites a file of a
complete save in place: each chunk a save writes goes to a new file, and the previous save's files
are removed only once the next save is complete (Game facts). So a copy of the files a complete
save names is that save, unless the next save removes one of them first; the copy then starts
again from the newest complete save. A backup is copied into a partial folder, synced, and renamed
into place once whole, so a folder named as a backup always holds one.

    <backups>/<taken>/              one backup, named for when it was taken, in UTC: 20261118T204012Z
    <backups>/<taken>/backup.json   its record (contracts/game-runtime.backup.schema.json): the
                                    world, the save's number, when the game completed it, its
                                    objects, when the backup was taken, where it came from, and each
                                    file's name, size and SHA-256, for a restore to check
    <backups>/<taken>/<file>        the save's files, named as in the world's save folder
    <backups>/.partial-<taken>/     a copy under way, or a backup being removed, or either cut short;
                                    removed at each start
    <backups>/.import-<id>/         an import under way, in a container of its own, which holds a lock
                                    on it, or one a kill cut short; removed at a start, and by the
                                    next import, once IMPORT_STAGING_MAX_AGE_S old and no import holds it

Every function here may block on the backups folder's disk, so the supervisor runs them off its
loop, through tend.
"""

import errno
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator

from vhd_runtime import generation
from vhd_runtime.generation import Backups as Settings

RECORD = "backup.json"
PARTIAL = ".partial-"
# An import under way, which vhd runs in a container of its own (vhd_runtime.transfer).
IMPORT_STAGING = ".import-"
# An import's staging folder unmodified for longer than this, which no import holds a lock on, is
# one a kill cut short. The lock spares an import whose copy outlasts it; vhd stops an import well
# within it all the same.
IMPORT_STAGING_MAX_AGE_S = 3600
NAME_FORMAT = "%Y%m%dT%H%M%SZ"
# A time in UTC, to the second, as a record and an import's or an export's result write it.
TAKEN_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_NAME = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
# How many times a copy that lost a file to the next save starts again.
ATTEMPTS = 3
# How many times a backup is named again when another writer took its name first: an import, in
# a container of its own, or the runtime's own turn.
NAME_ATTEMPTS = 3
# A .chunks file names one chunk in 11 bytes, and a world has a few thousand chunks at most.
CHUNKS_MAX_BYTES = 1 << 20
COPY_BLOCK = 1 << 20
MIB = 1 << 20
# What a copy leaves free on the backups folder's disk, which it may share with the worlds and the
# database: a copy that would leave less is not started.
RESERVE = 1 << 30
# A backup named for more than this after now is no backup: one taken while the clock ran that far
# ahead would otherwise stay newest, and drag every later name after it.
AHEAD = timedelta(days=1)

OK_FILE = re.compile(r"^_main\.([0-9]+)\.ok$")
# The .chunks file, as the game writes it (Game facts): little-endian 42, the save's objects, the
# chunks it names, then each chunk's index, size, version and objects.
_HEADER = struct.Struct("<hii")
_ENTRY = struct.Struct("<HBIi")
_MAGIC = 42

# A save, as a backup knows it: its number, and when the game completed it, as its .ok's
# modification time in nanoseconds. Numbers repeat, after a restore or for a world made again.
Saved = tuple[int, int]


class BackupFailed(Exception):
    """No backup was taken. The message says why, in one line."""


class Gone(Exception):
    """A file of the save went while it was copied: the next save completed."""


class Changed(Exception):
    """A file changed while it was copied."""


@dataclass(frozen=True)
class Save:
    """A complete save: its number, when the game completed it, its objects as its .chunks file
    counts them, and its files."""

    number: int
    written_ns: int
    objects: int
    files: tuple[str, ...]

    @property
    def saved(self) -> Saved:
        return self.number, self.written_ns


@dataclass(frozen=True)
class Backup:
    name: str
    saved: Saved


@dataclass(frozen=True)
class Place:
    """A world's backups: where its saves are, where its backups go, its save name, and the backup
    a restore asks for, which no turn prunes."""

    saves: Path
    backups: Path
    world: str
    spare: str | None = None


@dataclass(frozen=True)
class Tended:
    """One turn of a world's backups: the backup it took, if any, the backups the folder holds,
    newest first, the save the newest holds, and why it could not take or keep its backups."""

    backup: Backup | None
    names: list[str]
    newest: Saved | None
    failure: str | None = None
    # Whether the failure was in taking a backup, so the next try waits longer each time.
    take_failed: bool = field(default=False)


def world_folder(saves: Path, world: str) -> Path:
    """Where the game keeps a world's 1.0+ save (Game facts)."""
    return saves / "worlds_local" / world


def tend(place: Place, settings: Settings, now: datetime, last: Saved | None, *, starting: bool = False) -> Tended:
    """One turn, off the supervisor's loop: back up the world's newest complete save unless the
    newest backup holds it, then keep what the settings say, and the backup a restore asks for.
    Starting, it first clears copies a stop cut short and the staging of imports a kill cut short, and takes the
    save the newest backup holds from its record, so a save the game stopped with that a stop never
    backed up is backed up now.
    Raises OSError only when the folder cannot be listed."""
    if starting:
        clear_partials(place.backups)
        clear_import_staging(place.backups, now)
        held = names(place.backups, now)
        last = last_saved(place.backups, held[0]) if held else None
    failure, take_failed = None, False
    try:
        backup = take(place.saves, place.backups, place.world, now, last)
    except (BackupFailed, OSError) as error:
        backup, failure, take_failed = None, str(error), True
    newest = backup.saved if backup is not None else last
    kept, pruned = prune(place.backups, settings.keep, settings.keep_daily, now, place.spare)
    return Tended(backup, kept, newest, failure or pruned or _ahead(place.backups, now), take_failed)


def newest_complete(folder: Path) -> Save | None:
    """The newest save in the world's folder that has its .ok, or None while the world has not
    saved. Save numbers wrap, so the newest is the newest .ok written, as the game reads it.
    Raises BackupFailed when that save lacks a file, or its .chunks file is not one this reads."""
    written: list[tuple[int, int]] = []
    try:
        entries = list(folder.iterdir())
    except FileNotFoundError:
        return None
    for entry in entries:
        match = OK_FILE.match(entry.name)
        if match is None:
            continue
        try:
            written.append((entry.stat().st_mtime_ns, int(match[1])))
        except FileNotFoundError:
            continue
    if not written:
        return None
    written_ns, number = max(written)
    main = tuple(f"_main.{number}.{kind}" for kind in ("chunks", "db2", "fwl2", "ok"))
    chunks, objects = read_chunks(folder / main[0], number)
    for name in main:
        if not (folder / name).is_file():
            raise BackupFailed(f"save {number} lacks {name}")
    for name in chunks:
        if not (folder / name).is_file():
            raise BackupFailed(f"save {number} lacks {name}, which its .chunks file names")
    return Save(number=number, written_ns=written_ns, objects=objects, files=(*sorted(chunks), *main))


def take(folder: Path, backups: Path, world: str, now: datetime, last: Saved | None = None) -> Backup | None:
    """Back up the world's newest complete save: the new backup, or None when the world has not
    saved, or its newest save is the one the newest backup holds. A copy that loses a file to
    the next save, or finds the save not whole, starts again from the newest complete save.
    Raises BackupFailed once it has tried ATTEMPTS times, or when the folder lacks the room, and
    OSError when the save folder cannot be read."""
    failure = "the world saved again during each copy"
    for _ in range(ATTEMPTS):
        try:
            save = newest_complete(folder)
        except BackupFailed as error:
            failure = str(error)
            continue
        if save is None or save.saved == last:
            return None
        try:
            return _copy(folder, save, backups, world, now)
        except Gone:
            continue
    raise BackupFailed(failure)


def read_chunks(path: Path, number: int) -> tuple[list[str], int]:
    """The chunk files a save's .chunks file names, and the objects it counts. Fails closed on a
    file in any other format, as a save parser outside its validated range must (Game facts), and on
    one that is no regular file."""
    try:
        with open_regular(path) as reader:
            data = reader.read(CHUNKS_MAX_BYTES + 1)
    except FileNotFoundError as error:
        raise BackupFailed(f"save {number} lacks _main.{number}.chunks") from error
    except OSError as error:
        raise BackupFailed(f"save {number}'s .chunks file cannot be read: {error.strerror or error}") from error
    if len(data) >= _HEADER.size:
        magic, objects, count = _HEADER.unpack_from(data)
        if magic == _MAGIC and objects >= 0 and count >= 0 and len(data) == _HEADER.size + count * _ENTRY.size:
            names = []
            for offset in range(_HEADER.size, len(data), _ENTRY.size):
                chunk, size, version, _ = _ENTRY.unpack_from(data, offset)
                names.append(f"{chunk >> 8:02x}_{chunk & 0xFF:02x}__{size}_{version}.chunk")
            return names, objects
    raise BackupFailed(f"save {number}'s .chunks file is not in the format this runtime reads")


def _copy(folder: Path, save: Save, backups: Path, world: str, now: datetime) -> Backup:
    taken_at = name_time(backups, now)
    name = taken_at.strftime(NAME_FORMAT)
    partial = backups / f"{PARTIAL}{name}"
    try:
        _room(folder, save, backups)
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir()
        files: list[dict[str, object]] = [copy_file(folder / file, partial / file) for file in save.files]
        record: dict[str, object] = {
            "world": world,
            "save": save.number,
            "written_ns": save.written_ns,
            "objects": save.objects,
            "taken": taken_at.strftime(TAKEN_FORMAT),
            "source": "runtime",
            "files": files,
        }
        named = name_backup(partial, backups, record, now)
    except Gone:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    except OSError as error:
        shutil.rmtree(partial, ignore_errors=True)
        raise BackupFailed(
            f"cannot write backup {name} into the world's backups folder: {error.strerror or error}"
        ) from error
    if named is None:
        shutil.rmtree(partial, ignore_errors=True)
        raise BackupFailed(f"imports took each name the runtime's backup tried, {NAME_ATTEMPTS} times")
    return Backup(name=named, saved=save.saved)


def name_backup(
    folder: Path, backups: Path, record: dict[str, object], now: datetime, contract: Draft202012Validator | None = None
) -> str | None:
    """Write the record into the folder, then rename the folder into the backups as a backup named
    for when it is taken, which the record says: the backup's name, or None when another writer
    took each name it tried, NAME_ATTEMPTS times. Raises BackupFailed when the record breaks the
    contract it is given, and OSError when it cannot be written or renamed."""
    for _ in range(NAME_ATTEMPTS):
        taken_at = name_time(backups, now)
        name = taken_at.strftime(NAME_FORMAT)
        record["taken"] = taken_at.strftime(TAKEN_FORMAT)
        breach = None if contract is None else generation.breach(contract, record)
        if breach is not None:
            raise BackupFailed(f"the backup's record breaks its contract {breach}")
        _write_record(folder, record)
        try:
            folder.rename(backups / name)
        except OSError as error:
            # Another writer took the name meanwhile: the next is a second after it.
            if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
                continue
            raise
        # The backup is whole and in place; a sync that fails now loses at most its name in a power cut.
        try:
            sync(backups)
        except OSError:
            pass
        return name
    return None


def _write_record(folder: Path, record: dict[str, object]) -> None:
    """Write the record whole and synced, replacing an earlier one, before its folder takes a
    backup's name."""
    temporary = folder / f".{RECORD}.tmp"
    with temporary.open("w") as writer:
        writer.write(json.dumps(record, indent=2) + "\n")
        writer.flush()
        os.fsync(writer.fileno())
    temporary.replace(folder / RECORD)
    sync(folder)


def name_time(backups: Path, now: datetime) -> datetime:
    """When a new backup counts as taken: now, or a second after the newest, if the clock has gone
    back since that was taken. Names order backups, so a new one always comes newest."""
    held = names(backups, now)
    newest = taken(held[0]) + timedelta(seconds=1) if held else now
    return max(now.replace(microsecond=0), newest)


def _room(folder: Path, save: Save, backups: Path) -> None:
    """Refuse a copy that would leave less than RESERVE free on the backups folder's disk, before
    it fills the disk the folder may share with the worlds and the database."""
    try:
        needed = sum((folder / file).stat().st_size for file in save.files)
    except FileNotFoundError as error:
        raise Gone(error.filename) from error
    free = shutil.disk_usage(backups).free
    if free - needed < RESERVE:
        raise BackupFailed(
            f"the world's backups folder has {free // MIB} MiB free; save {save.number} needs {needed // MIB} MiB, "
            f"and {RESERVE // MIB} MiB must stay free"
        )


def copy_file(source: Path, target: Path, limit: int | None = None, *, unchanged: bool = False) -> dict[str, object]:
    """Copy one file of the save to disk, and say its size and SHA-256 for the record. With a limit,
    as for a file a backup's record lists, it copies only a regular file, never through a link,
    and no more than limit bytes, raising OSError otherwise. Unchanged, as for a file no game
    writes, it raises Changed when the file's size, modification time or inode differ after the
    copy from before it."""
    try:
        reader = open_regular(source) if limit is not None else source.open("rb")
    except FileNotFoundError as error:
        raise Gone(source.name) from error
    digest = hashlib.sha256()
    size = 0
    # An open file reads whole though the game removes it meanwhile.
    with reader, target.open("xb") as writer:
        before = _identity(os.fstat(reader.fileno()))
        while block := reader.read(COPY_BLOCK):
            size += len(block)
            if limit is not None and size > limit:
                raise OSError(errno.EFBIG, f"{source.name} is larger than its record says")
            digest.update(block)
            writer.write(block)
        if unchanged and _identity(os.fstat(reader.fileno())) != before:
            raise Changed(source.name)
        writer.flush()
        os.fsync(writer.fileno())
    return {"name": target.name, "size": size, "sha256": digest.hexdigest()}


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_ino


def open_regular(source: Path) -> io.BufferedReader:
    """A regular file, opened without following a link, and without waiting on a pipe."""
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise OSError(errno.EINVAL, f"{source.name} is a link") from error
        raise
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError(errno.EINVAL, f"{source.name} is not a regular file")
    return os.fdopen(descriptor, "rb")


def sync(directory: Path) -> None:
    """Sync a directory's entries, so a rename into it survives a power cut. A file system that
    cannot sync a directory, as some network mounts cannot, costs that and nothing else."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise
    finally:
        os.close(descriptor)


def taken(name: str) -> datetime:
    """When a backup was taken, from its name."""
    return datetime.strptime(name, NAME_FORMAT).replace(tzinfo=UTC)


def is_name(name: str) -> bool:
    if not _NAME.match(name):
        return False
    try:
        taken(name)
    except ValueError:
        return False
    return True


def names(backups: Path, now: datetime) -> list[str]:
    """The backups in the folder, newest first: folders named for a time that exists and is not
    more than AHEAD after now, holding a record. Anything else there is not a backup, and is left
    alone."""
    return sorted(
        (
            entry.name
            for entry in backups.iterdir()
            if is_name(entry.name) and taken(entry.name) <= now + AHEAD and (entry / RECORD).is_file()
        ),
        reverse=True,
    )


def _ahead(backups: Path, now: datetime) -> str | None:
    """Why a folder named for a time well after now is left alone, if one is."""
    ahead = sorted(entry.name for entry in backups.iterdir() if is_name(entry.name) and taken(entry.name) > now + AHEAD)
    if not ahead:
        return None
    return (
        f"the world's backups folder holds {', '.join(ahead[:3])}, named for after now, which the runtime leaves "
        "alone as no backup"
    )


def last_saved(backups: Path, name: str) -> Saved | None:
    """The save a backup holds, from its record, or None if the record cannot be read."""
    try:
        record = json.loads((backups / name / RECORD).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    save, written_ns = record.get("save"), record.get("written_ns")
    if type(save) is not int or type(written_ns) is not int:
        return None
    return save, written_ns


def prune(
    backups: Path, keep: int, keep_daily: int, now: datetime, spare: str | None = None
) -> tuple[list[str], str | None]:
    """Remove the backups the settings no longer keep, oldest first: the newest keep stay, and the
    newest of each of the newest keep_daily days, in UTC, that hold one; the newest backup is
    always among them, and so is spare, the backup a restore asks for. Each is renamed out of the
    backups first, so one cut short is never left as a backup. Returns the backups left, newest
    first, and why one could not be removed."""
    held = names(backups, now)
    kept = set(held[:keep])
    if spare in held:
        kept.add(spare)
    days: dict[str, str] = {}
    for name in held:
        days.setdefault(name[:8], name)
    kept.update(list(days.values())[:keep_daily])
    failure = None
    for name in reversed(held):
        if name in kept:
            continue
        doomed = backups / f"{PARTIAL}{name}"
        try:
            (backups / name).rename(doomed)
            shutil.rmtree(doomed)
        except OSError as error:
            why = error.strerror or error
            failure = failure or f"cannot remove backup {name} from the world's backups folder: {why}"
    return names(backups, now), failure


def clear_partials(backups: Path) -> None:
    """Remove the copies and removals a stop cut short: none of them is a backup."""
    for entry in backups.iterdir():
        if entry.name.startswith(PARTIAL):
            shutil.rmtree(entry, ignore_errors=True)


def clear_import_staging(backups: Path, now: datetime) -> None:
    """Remove the staging folders of imports a kill cut short: those unmodified for longer than
    IMPORT_STAGING_MAX_AGE_S that no import holds a lock on. Any other may be an import still under
    way. Raises OSError only when the folder cannot be listed."""
    for entry in backups.iterdir():
        if not entry.name.startswith(IMPORT_STAGING):
            continue
        try:
            if now.timestamp() - entry.lstat().st_mtime <= IMPORT_STAGING_MAX_AGE_S:
                continue
            held = lock_folder(entry)
        except OSError:
            # Gone, no folder, or held by an import under way.
            continue
        try:
            shutil.rmtree(entry, ignore_errors=True)
        finally:
            os.close(held)


def lock_folder(folder: Path) -> int:
    """An exclusive lock on a folder, never through a link and without waiting for another holder:
    the descriptor that holds it until it is closed. Raises BlockingIOError when another holds it,
    and OSError when the folder cannot be opened."""
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor
