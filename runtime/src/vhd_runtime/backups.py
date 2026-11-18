"""A world's backups: copies of its newest complete save, taken while the game runs (Game runtimes).

A 1.0+ save is complete once its _main.<n>.ok exists, and the game never rewrites a file of a
complete save in place: each chunk a save writes goes to a new file, and the previous save's files
are removed only once the next save is complete (Game facts). So a copy of the files a complete
save names is that save, unless the next save removes one of them first; the copy then starts
again from the newest complete save. A backup is copied into a partial folder and renamed into
place once whole, so a folder named as a backup always holds one.

    <backups>/<taken>/              one backup, named for when it was taken, in UTC: 20261118T204012Z
    <backups>/<taken>/backup.json   its record: the world, the save's number and objects, and each
                                    file's name, size and SHA-256, which a restore checks
    <backups>/<taken>/<file>        the save's files, named as in the world's save folder
    <backups>/.partial-<taken>/     a copy under way, or one a stop cut short; removed at each start
"""

import hashlib
import json
import re
import shutil
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

RECORD = "backup.json"
PARTIAL = ".partial-"
NAME_FORMAT = "%Y%m%dT%H%M%SZ"
NAME = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
# How many times a copy that lost a file to the next save starts again.
ATTEMPTS = 3
# A .chunks file names one chunk in 11 bytes, and a world has a few thousand chunks at most.
CHUNKS_MAX_BYTES = 1 << 20
COPY_BLOCK = 1 << 20

_OK = re.compile(r"^_main\.([0-9]+)\.ok$")
# The .chunks file, as the game writes it: little-endian 42, the save's objects, the chunks it
# names, then each chunk's index, size, version and objects (Game facts).
_HEADER = struct.Struct("<hii")
_ENTRY = struct.Struct("<HBIi")
_MAGIC = 42


class BackupFailed(Exception):
    """No backup was taken. The message says why, in one line."""


class _Gone(Exception):
    """A file of the save went while it was copied: the next save completed."""


@dataclass(frozen=True)
class Save:
    """A complete save: its number, its objects as its .chunks file counts them, and its files."""

    number: int
    objects: int
    files: tuple[str, ...]


@dataclass(frozen=True)
class Backup:
    name: str
    save: int


def world_folder(saves: Path, world: str) -> Path:
    """Where the game keeps a world's 1.0+ save (Game facts)."""
    return saves / "worlds_local" / world


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
        match = _OK.match(entry.name)
        if match is None:
            continue
        try:
            written.append((entry.stat().st_mtime_ns, int(match[1])))
        except FileNotFoundError:
            continue
    if not written:
        return None
    _, number = max(written)
    main = tuple(f"_main.{number}.{kind}" for kind in ("chunks", "db2", "fwl2", "ok"))
    chunks, objects = _chunks(folder / main[0], number)
    for name in main:
        if not (folder / name).is_file():
            raise BackupFailed(f"save {number} lacks {name}")
    for name in chunks:
        if not (folder / name).is_file():
            raise BackupFailed(f"save {number} lacks {name}, which its .chunks file names")
    return Save(number=number, objects=objects, files=(*sorted(chunks), *main))


def take(folder: Path, backups: Path, world: str, now: datetime, last: int | None = None) -> Backup | None:
    """Back up the world's newest complete save into backups, named for now: the new backup, or
    None when the world has not saved, or its newest save is the one backed up last. A copy that
    loses a file to the next save, or finds the save not whole as the next one completes, starts
    again from the newest complete save. Raises BackupFailed once it has tried ATTEMPTS times."""
    failure = "the world saved again during each copy"
    for _ in range(ATTEMPTS):
        try:
            save = newest_complete(folder)
        except BackupFailed as error:
            failure = str(error)
            continue
        if save is None or save.number == last:
            return None
        try:
            return _copy(folder, save, backups, world, now)
        except _Gone:
            continue
    raise BackupFailed(failure)


def _chunks(path: Path, number: int) -> tuple[list[str], int]:
    """The chunk files a save's .chunks file names, and the objects it counts. Fails closed on a
    file in any other format, as a save parser outside its validated range must (Game facts)."""
    try:
        with path.open("rb") as reader:
            data = reader.read(CHUNKS_MAX_BYTES + 1)
    except FileNotFoundError as error:
        raise BackupFailed(f"save {number} lacks _main.{number}.chunks") from error
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
    name = now.strftime(NAME_FORMAT)
    if (backups / name).exists():
        raise BackupFailed(f"a backup named {name} exists already")
    partial = backups / f"{PARTIAL}{name}"
    try:
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir()
        files = [_copy_file(folder / file, partial / file) for file in save.files]
        record = {
            "world": world,
            "save": save.number,
            "objects": save.objects,
            "taken": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files": files,
        }
        (partial / RECORD).write_text(json.dumps(record, indent=2) + "\n")
        partial.rename(backups / name)
    except _Gone:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    except OSError as error:
        shutil.rmtree(partial, ignore_errors=True)
        raise BackupFailed(f"cannot write backup {name} into {backups}: {error.strerror or error}") from error
    return Backup(name=name, save=save.number)


def _copy_file(source: Path, target: Path) -> dict[str, object]:
    """Copy one file of the save, and say its size and SHA-256 for the record."""
    try:
        reader = source.open("rb")
    except FileNotFoundError as error:
        raise _Gone(source.name) from error
    digest = hashlib.sha256()
    size = 0
    # An open file reads whole though the game removes it meanwhile.
    with reader, target.open("xb") as writer:
        while block := reader.read(COPY_BLOCK):
            digest.update(block)
            writer.write(block)
            size += len(block)
    return {"name": target.name, "size": size, "sha256": digest.hexdigest()}


def names(backups: Path) -> list[str]:
    """The backups in the folder, newest first. Anything else there is not one, and is left alone."""
    return sorted(
        (entry.name for entry in backups.iterdir() if NAME.match(entry.name) and (entry / RECORD).is_file()),
        reverse=True,
    )


def last_save(backups: Path, newest: str) -> int | None:
    """The save number a backup holds, from its record, or None if the record cannot be read."""
    try:
        record = json.loads((backups / newest / RECORD).read_text())
    except (OSError, ValueError):
        return None
    save = record.get("save") if isinstance(record, dict) else None
    return save if isinstance(save, int) else None


def prune(backups: Path, keep: int, keep_daily: int) -> list[str]:
    """Remove the backups the settings no longer keep, oldest first, and return those left, newest
    first. The newest keep stay, and the newest of each of the newest keep_daily days, in UTC; the
    newest backup is always among them."""
    held = names(backups)
    kept = set(held[:keep])
    days: dict[str, str] = {}
    for name in held:
        days.setdefault(name[:8], name)
    kept.update(list(days.values())[:keep_daily])
    for name in reversed(held):
        if name not in kept:
            shutil.rmtree(backups / name)
    return [name for name in held if name in kept]


def clear_partials(backups: Path) -> None:
    """Remove the copies a stop cut short: none of them is a backup."""
    for entry in backups.iterdir():
        if entry.name.startswith(PARTIAL):
            shutil.rmtree(entry, ignore_errors=True)


def taken(name: str) -> datetime:
    """When a backup was taken, from its name."""
    return datetime.strptime(name, NAME_FORMAT).replace(tzinfo=UTC)
