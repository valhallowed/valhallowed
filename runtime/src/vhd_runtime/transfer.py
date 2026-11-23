"""A world's import and export: the actions vhd runs through the game runtime's image, in a one-off
container that never starts the supervisor (Game runtimes, Restore, import and export share one
path).

An import takes a 1.0+ save the server manager put in the install folder, checks it is exactly one
complete save, and keeps it as a backup of its world, which a restore then swaps in like any other.
Each action prints its result as its last line on standard output, one JSON document as
contracts/game-runtime.import-export.schema.json defines it, and logs to standard error.

    /import                         the save folder, mounted read-only under its own name
    /backups                        the world's own folder of the install's backups
    /backups/.import-<id>/          an import under way, or one a kill cut short; a start of the
                                    world's runtime removes it once it is old
"""

import errno
import json
import logging
import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from vhd_runtime import backups, restore

# vhd mounts these into the one-off container; the supervisor never sees them, apart from the
# backups folder, which is the same mount as the supervisor's Paths.backups.
IMPORT_FOLDER = Path("/import")
BACKUPS_FOLDER = Path("/backups")
# The largest save an import takes, summed over its files before any is copied.
IMPORT_MAX_BYTES = 4 << 30
# How many times an import names its backup again when the runtime's own turn took the name first.
NAME_ATTEMPTS = 3
CONTRACT = 1
# The result contract's bound on a reason.
REASON_MAX = 500
# A world's save name, as the manifest schema's save_name defines it.
_SAVE_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,62}[A-Za-z0-9])?")
# A pre-1.0 save's files, the format an import refuses (Game facts).
_PRE_1_0 = (".db", ".fwl")

Action = Literal["import", "export"]

_log = logging.getLogger(__name__)


class Refused(Exception):
    """The action kept nothing. The message says why, in one line."""


@dataclass(frozen=True)
class Mounts:
    """Where the action finds what vhd mounts for it."""

    imported: Path = IMPORT_FOLDER
    backups: Path = BACKUPS_FOLDER


@dataclass(frozen=True)
class _Found:
    """The save the import folder holds: its folder, its number, each of its files with its size,
    and when the game completed it."""

    folder: Path
    number: int
    sizes: dict[str, int]
    written_ns: int


class SaveSummary(TypedDict):
    """The save a backup holds, as the result names it."""

    number: int
    objects: int
    written: str


class Result(TypedDict):
    """One action's result, as contracts/game-runtime.import-export.schema.json defines it."""

    contract: int
    action: Action
    world: str
    outcome: Literal["done", "refused"]
    reason: NotRequired[str]
    backup: NotRequired[str]
    save: NotRequired[SaveSummary]


def is_save_name(world: str) -> bool:
    return _SAVE_NAME.fullmatch(world) is not None


def run(action: Action, world: str, act: Callable[[], Result]) -> int:
    """Run one action and print its result as the last line on standard output: 0 when done, 1
    when refused. Any error is a refusal with its reason, never a traceback."""
    result: Result
    try:
        result = act()
    except Refused as error:
        _log.error("%s of %s refused: %s", action, world, error)
        result = _refused(action, world, str(error))
    except Exception as error:
        _log.exception("%s of %s stopped on an unexpected error", action, world)
        result = _refused(action, world, f"{action} stopped on an unexpected error: {type(error).__name__}: {error}")
    print(json.dumps(result), flush=True)
    return 0 if result["outcome"] == "done" else 1


def import_save(mounts: Mounts, world: str, now: Callable[[], datetime]) -> Result:
    """Keep the save in the import folder as a backup of the world, once it is exactly one complete
    1.0+ save and fits. Raises Refused, keeping nothing."""
    try:
        folder = _save_folder(mounts.imported, world)
        sizes = _save_files(folder)
        number = _one_save(folder, set(sizes))
        found = _Found(folder, number, sizes, (folder / f"_main.{number}.ok").lstat().st_mtime_ns)
    except OSError as error:
        raise Refused(f"cannot read the save: {error.strerror or error}") from error
    total = sum(sizes.values())
    if total > IMPORT_MAX_BYTES:
        raise Refused(
            f"the save holds {-(-total // backups.MIB)} MiB, more than the {IMPORT_MAX_BYTES // backups.MIB} MiB "
            "an import takes"
        )
    staging = mounts.backups / f"{backups.IMPORT_STAGING}{secrets.token_hex(16)}"
    try:
        free = shutil.disk_usage(mounts.backups).free
        if free - total < backups.RESERVE:
            raise Refused(
                f"the backup folder has {free // backups.MIB} MiB free; the save needs {total // backups.MIB} MiB, "
                f"and {backups.RESERVE // backups.MIB} MiB must stay free"
            )
        staging.mkdir()
    except OSError as error:
        raise Refused(f"cannot write into the backup folder: {error.strerror or error}") from error
    try:
        name, objects = _keep(found, staging, mounts.backups, world, now)
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise Refused(f"cannot copy the save into the backup folder: {error.strerror or error}") from error
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _log.info("imported save %d of world %s as backup %s, %d objects", number, world, name, objects)
    return _done("import", world, name, _summary(number, objects, found.written_ns))


def _save_folder(imported: Path, world: str) -> Path:
    """The one entry in the import folder, once it is a folder named for the world, not a link."""
    entries = sorted(imported.iterdir())
    for entry in entries:
        if entry.name.endswith(_PRE_1_0):
            raise Refused(f"the import folder holds {entry.name}, a pre-1.0 save; an import takes a 1.0+ save only")
    if len(entries) != 1:
        held = ", ".join(entry.name for entry in entries[:3]) or "nothing"
        raise Refused(f"the import folder holds {held}; it must hold exactly one entry, the save folder {world}")
    folder = entries[0]
    if folder.name != world:
        raise Refused(f"the import folder holds {folder.name}, not the save folder {world}")
    mode = folder.lstat().st_mode
    if not stat.S_ISDIR(mode):
        raise Refused(f"the import folder holds {world} as {_kind(mode)}, not a folder")
    return folder


def _save_files(folder: Path) -> dict[str, int]:
    """Each file in the save folder and its size, once each is a regular file a save is made of.
    Never follows a link."""
    sizes: dict[str, int] = {}
    for entry in sorted(folder.iterdir()):
        mode = entry.lstat()
        if not stat.S_ISREG(mode.st_mode):
            raise Refused(
                f"the save holds {entry.name} as {_kind(mode.st_mode)}, and an import takes regular files only"
            )
        if entry.name.endswith(_PRE_1_0):
            raise Refused(f"the save holds {entry.name}, a pre-1.0 save's file; an import takes a 1.0+ save only")
        if not restore._FILE.fullmatch(entry.name):
            raise Refused(f"the save holds {entry.name}, which is no file of a save")
        sizes[entry.name] = mode.st_size
    return sizes


def _one_save(folder: Path, present: set[str]) -> int:
    """The save's number, once the folder holds exactly one complete save, all of its files and
    nothing else."""
    numbers = sorted(int(match[1]) for name in present if (match := backups._OK.match(name)))
    if not numbers:
        raise Refused("the save folder holds no complete save: no _main.<n>.ok")
    if len(numbers) > 1:
        raise Refused(
            f"the save folder holds {len(numbers)} complete saves ({', '.join(map(str, numbers[:5]))}); the game picks "
            "the newest by modification time, which a copy may have lost, so an import takes exactly one"
        )
    number = numbers[0]
    main = {f"_main.{number}.{kind}" for kind in ("chunks", "db2", "fwl2", "ok")}
    lacks = sorted(main - present)
    if lacks:
        raise Refused(f"save {number} lacks {lacks[0]}")
    try:
        chunks, _ = backups._chunks(folder / f"_main.{number}.chunks", number)
    except backups.BackupFailed as error:
        raise Refused(str(error)) from error
    expected = main | set(chunks)
    lacks = sorted(expected - present)
    if lacks:
        raise Refused(f"save {number} lacks {lacks[0]}, which its .chunks file names")
    extra = sorted(present - expected)
    if extra:
        raise Refused(f"the save folder holds {extra[0]}, which is no file of save {number}")
    return number


def _keep(found: _Found, staging: Path, store: Path, world: str, now: Callable[[], datetime]) -> tuple[str, int]:
    """Copy the save into staging, check the copy, record it, and name it as a backup: the backup's
    name and the save's objects."""
    sizes, number = found.sizes, found.number
    files: list[dict[str, object]] = []
    # Chunks first and the .ok last, as the game writes a save.
    for name in sorted(sizes, key=lambda file: (file.startswith("_main."), file)):
        try:
            copied = backups.copy_file(found.folder / name, staging / name, sizes[name])
        except backups.Gone as error:
            raise Refused(f"the save's {name} went while it was copied") from error
        except OSError as error:
            if error.errno == errno.EFBIG:
                raise Refused(f"the save's {name} grew while it was copied") from error
            raise
        if copied["size"] != sizes[name]:
            raise Refused(f"the save's {name} changed while it was copied")
        files.append(copied)
    try:
        save = backups.newest_complete(staging)
    except backups.BackupFailed as error:
        raise Refused(f"the copy is not a complete save: {error}") from error
    if save is None or save.number != number or set(save.files) != set(sizes):
        raise Refused(f"the copy does not hold save {number}")
    record: dict[str, object] = {
        "world": world,
        "save": number,
        "written_ns": found.written_ns,
        "objects": save.objects,
        "taken": "",
        "source": "import",
        "files": files,
    }
    for _ in range(NAME_ATTEMPTS):
        taken_at = backups._name_time(store, now())
        name = taken_at.strftime(backups.NAME_FORMAT)
        record["taken"] = taken_at.strftime(backups.TAKEN_FORMAT)
        _write_record(staging, record)
        try:
            staging.rename(store / name)
        except OSError as error:
            # The runtime's own turn took the name meanwhile: the next is a second after it.
            if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
                continue
            raise
        # The backup is whole and in place; a sync that fails now loses at most its name in a power cut.
        try:
            backups.sync(store)
        except OSError:
            pass
        return name, save.objects
    raise Refused(f"the runtime's own backups took each name the import tried, {NAME_ATTEMPTS} times")


def _write_record(staging: Path, record: dict[str, object]) -> None:
    """Write the record whole and synced, replacing an earlier one, before its folder takes a
    backup's name."""
    temporary = staging / f".{backups.RECORD}.tmp"
    with temporary.open("w") as writer:
        writer.write(json.dumps(record, indent=2) + "\n")
        writer.flush()
        os.fsync(writer.fileno())
    temporary.replace(staging / backups.RECORD)
    backups.sync(staging)


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "a link"
    if stat.S_ISDIR(mode):
        return "a folder"
    if stat.S_ISREG(mode):
        return "a file"
    return "a special file"


def _summary(number: int, objects: int, written_ns: int) -> SaveSummary:
    """The save, as the result names it: when the game completed it is in UTC, to the second."""
    written = datetime.fromtimestamp(written_ns // 1_000_000_000, UTC).strftime(backups.TAKEN_FORMAT)
    return {"number": number, "objects": objects, "written": written}


def _done(action: Action, world: str, backup: str, save: SaveSummary) -> Result:
    return {"contract": CONTRACT, "action": action, "world": world, "outcome": "done", "backup": backup, "save": save}


def _refused(action: Action, world: str, why: str) -> Result:
    return {"contract": CONTRACT, "action": action, "world": world, "outcome": "refused", "reason": _reason(why)}


def _reason(text: str) -> str:
    """One line of printable text within the contract's bound: an error's text is not ours."""
    line = "".join(c if c.isprintable() else " " for c in text).strip()[:REASON_MAX]
    return line or "refused for no stated reason"
