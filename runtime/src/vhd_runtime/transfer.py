"""A world's import and export: the actions vhd runs through the game runtime's image, in a one-off
container that never starts the supervisor (Game runtimes, Restore, import and export share one
path).

An import takes a 1.0+ save the server manager put in the install folder, checks it is exactly one
complete save, and keeps it as a backup of its world, which a restore then swaps in like any other.
An export copies one of the world's backups, checked as a restore checks it, into the install
folder in the layout the game reads. Each action prints its result as its last line on standard
output, one JSON document as contracts/game-runtime.import-export.schema.json defines it, and logs
to standard error.

    /import                         the folder given to vhd import, holding only the save folder,
                                    named for the world; vhd mounts it read-only
    /backups                        the world's own folder of the install's backups
    /backups/.import-<id>/          an import under way, which holds a lock on it until it is named
                                    as a backup, or one a kill cut short; a start of the world's
                                    runtime, and the next import, remove it once it is old and unheld,
                                    or, where the file system refuses any lock, once nothing in it
                                    changed for as long
    /export                         a folder with no worlds_local/<world> yet, which an export
                                    copies the backup into, as worlds_local/<world>/
"""

import errno
import json
import logging
import os
import re
import secrets
import shutil
import signal
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Literal, NoReturn, NotRequired, TypedDict

from jsonschema import Draft202012Validator

from vhd_runtime import backups, generation, restore
from vhd_runtime.supervisor import Paths

# vhd mounts these into the one-off container; the supervisor never sees them, apart from the
# backups folder, which is the same mount as the supervisor's Paths.backups.
IMPORT_FOLDER = Path("/import")
BACKUPS_FOLDER = Path("/backups")
EXPORT_FOLDER = Path("/export")
# The largest save an import takes, summed over its files before any is copied.
IMPORT_MAX_BYTES = 4 << 30
CONTRACT = 1
RESULT_CONTRACT = "game-runtime.import-export.schema.json"
# The result contract's bound on a reason.
REASON_MAX = 500
# A world's save name, as the manifest schema's save_name defines it.
_SAVE_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,62}[A-Za-z0-9])?")
# What vhd, or Docker on its behalf, sends to stop an action under way.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
# A pre-1.0 save's files, <world>.db and <world>.fwl, the format an import refuses (Game facts).
_PRE_1_0 = (".db", ".fwl")

Action = Literal["import", "export"]

_log = logging.getLogger(__name__)


class Refused(Exception):
    """The action kept nothing. The message says why, in one line."""


class Stopped(BaseException):
    """A signal stopped the action. A BaseException, as KeyboardInterrupt is, so nothing that handles
    the action's errors takes it for one, and the action's own cleanup runs."""


@dataclass(frozen=True)
class Mounts:
    """Where the action finds what vhd mounts for it, and the image's contracts."""

    imported: Path = IMPORT_FOLDER
    backups: Path = BACKUPS_FOLDER
    exported: Path = EXPORT_FOLDER
    contracts: Path = Paths.contracts


@dataclass(frozen=True)
class _Found:
    """The save the folder given to vhd import holds: its folder, its number, each of its files as
    the scan found it, and when the game completed it."""

    folder: Path
    number: int
    scanned: dict[str, os.stat_result]
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


def run(action: Action, world: str, contracts: Path, act: Callable[[], Result]) -> int:
    """Run one action and print its result, once it keeps its contract in the contracts folder, as
    the last line on standard output: 0 when done, 1 when refused. A SIGTERM or SIGINT stops the
    action while it copies, which removes what it wrote; one that comes before waits for the copy,
    and one that comes after waits for the result, so it never cuts short what the action keeps,
    its cleanup or its result. A stop, and anything else raised but SystemExit, is a refusal with
    its reason, never a traceback."""
    result: Result
    # Held first, so no stop lands before the handler or while the contract loads.
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    previous = {signum: signal.getsignal(signum) for signum in STOP_SIGNALS}
    try:
        for signum in STOP_SIGNALS:
            signal.signal(signum, _stop)
        try:
            contract = generation.validator(contracts / RESULT_CONTRACT)
        except Exception as error:
            _log.exception("%s of %s cannot read its result's contract", action, world)
            result = _refused(action, world, f"{action} cannot read its result's contract: {error}")
            print(json.dumps(result), flush=True)
            return 1
        result = _outcome(action, world, act)
        breach = _breach(action, contract, result)
        if breach is not None:
            _log.error("%s of %s refused: %s: %s", action, world, breach, result)
            result = _refused(action, world, breach)
        print(json.dumps(result), flush=True)
    finally:
        # A stop still held came once the action could no longer stop: its result answers it.
        try:
            for signum in STOP_SIGNALS:
                signal.signal(signum, signal.SIG_IGN)
        finally:
            # Set back even where no handler can be set, as outside the main thread.
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        for signum, handler in previous.items():
            # None: a handler not set from Python, which Python cannot set back, so the signal
            # stays ignored.
            if handler is not None:
                signal.signal(signum, handler)
    return 0 if result["outcome"] == "done" else 1


def _outcome(action: Action, world: str, act: Callable[[], Result]) -> Result:
    """The action's result, or its refusal with the reason."""
    try:
        return act()
    except Refused as error:
        _log.error("%s of %s refused: %s", action, world, error)
        return _refused(action, world, str(error))
    except Stopped as error:
        _log.error("%s of %s %s", action, world, error)
        return _refused(action, world, f"{action} {error}")
    except SystemExit:
        raise
    except BaseException as error:
        _log.exception("%s of %s stopped on an unexpected error", action, world)
        what = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
        return _refused(action, world, f"{action} stopped on an unexpected error: {what}")


@contextmanager
def _stoppable() -> Iterator[None]:
    """Where a stop stops the action: its copies, the one step that takes long. run holds a stop
    everywhere else."""
    held = signal.pthread_sigmask(signal.SIG_BLOCK, ())
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)


def _breach(action: Action, contract: Draft202012Validator, result: Result) -> str | None:
    """Why a result counts as refused: it breaks its contract, or cannot be checked against it."""
    try:
        breach = generation.breach(contract, result)
    except Exception as error:
        return f"{action}'s result cannot be checked against its contract: {error}"
    return None if breach is None else f"{action}'s result breaks its contract {breach}"


def _stop(signum: int, frame: FrameType | None) -> NoReturn:
    # Once: a second signal must not cut the action's cleanup short.
    for each in STOP_SIGNALS:
        signal.signal(each, signal.SIG_IGN)
    raise Stopped(f"stopped by {signal.Signals(signum).name} before it finished")


def import_save(mounts: Mounts, world: str, now: Callable[[], datetime]) -> Result:
    """Keep the save in the install folder as a backup of the world, once it is exactly one complete
    1.0+ save and fits, and its record keeps its contract. Raises Refused, keeping nothing."""
    contract = _record_contract(mounts)
    try:
        backups.clear_import_staging(mounts.backups, now())
    except OSError as error:
        raise Refused(f"cannot read the world's backups folder: {error.strerror or error}") from error
    try:
        folder = _save_folder(mounts.imported, world)
        scanned = _save_files(folder)
        number = _one_save(folder, set(scanned))
        found = _Found(folder, number, scanned, scanned[f"_main.{number}.ok"].st_mtime_ns)
    except OSError as error:
        raise Refused(f"cannot read the save you put in the install folder: {error.strerror or error}") from error
    breach = backups.save_breach(number, found.written_ns)
    if breach is not None:
        raise Refused(breach)
    total = sum(info.st_size for info in scanned.values())
    if total > IMPORT_MAX_BYTES:
        raise Refused(
            f"the save holds {-(-total // backups.MIB)} MiB, more than the {IMPORT_MAX_BYTES // backups.MIB} MiB "
            "an import takes"
        )
    staging = mounts.backups / f"{backups.IMPORT_STAGING}{secrets.token_hex(16)}"
    try:
        _room(mounts.backups, "the world's backups folder", "the save", total)
        staging.mkdir()
    except OSError as error:
        raise Refused(f"cannot write into the world's backups folder: {error.strerror or error}") from error
    held = None
    try:
        # Held until the backup is named, or the staging removed, so no cleanup removes it meanwhile,
        # however long the copy takes.
        held = backups.lock_folder(staging)
        name, objects = _keep(found, staging, mounts.backups, now, contract)
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise Refused(f"cannot copy the save into the world's backups folder: {error.strerror or error}") from error
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if held is not None:
            os.close(held)
    _log.info("imported save %d of world %s as backup %s, %d objects", number, world, name, objects)
    return _done("import", world, name, _summary(number, objects, found.written_ns))


def export_backup(mounts: Mounts, world: str, backup: str | None, now: Callable[[], datetime]) -> Result:
    """Copy one of the world's backups, the newest unless one is named, into the install folder
    in the layout the game reads, once its record names the world, the install folder's disk has room
    for it, and each file is the one the record lists. Raises Refused, removing what it wrote."""
    try:
        held = backups.names(mounts.backups, now())
    except OSError as error:
        raise Refused(f"cannot read the world's backups folder: {error.strerror or error}") from error
    if backup is None and not held:
        raise Refused(f"the world's backups folder holds no backup of {world}")
    name = held[0] if backup is None else backup
    if name not in held:
        raise Refused(f"backup {name} does not exist in the world's backups folder")
    source = mounts.backups / name
    contract = _record_contract(mounts)
    try:
        record = restore.read_record(source, world, name, contract)
    except restore.RestoreFailed as error:
        raise Refused(str(error)) from error
    # Before the copy, so a time the result cannot name leaves nothing in the install folder. The
    # record's contract already bounds the rest.
    if record.written_ns // 1_000_000_000 > backups.WRITTEN_S_MAX:
        raise Refused(f"backup {name}'s record says its save was completed after 9999, which no result can name")
    try:
        _room(mounts.exported, "the install folder", f"backup {name}", sum(file["size"] for file in record.files))
    except OSError as error:
        raise Refused(f"cannot read the install folder: {error.strerror or error}") from error
    target, written = _export_target(mounts.exported, world)
    try:
        with _stoppable():
            objects = restore.copy_checked(source, target, name, record.number, record.files)
        backups.sync(target)
        backups.sync(target.parent)
    except restore.RestoreFailed as error:
        shutil.rmtree(written, ignore_errors=True)
        raise Refused(str(error)) from error
    except OSError as error:
        shutil.rmtree(written, ignore_errors=True)
        raise Refused(f"cannot copy backup {name} into the install folder: {error.strerror or error}") from error
    except BaseException:
        shutil.rmtree(written, ignore_errors=True)
        raise
    _log.info("exported backup %s of world %s, save %d, %d objects", name, world, record.number, objects)
    return _done("export", world, name, _summary(record.number, objects, record.written_ns))


def _record_contract(mounts: Mounts) -> Draft202012Validator:
    try:
        return restore.record_contract(mounts.contracts)
    except restore.RestoreFailed as error:
        raise Refused(str(error)) from error


def _room(folder: Path, where: str, what: str, needed: int) -> None:
    """Refuse a copy that would leave less than backups.RESERVE free on the folder's disk, which it
    may share with the worlds and the database, before anything is written. Raises OSError when the
    disk cannot be read."""
    free = shutil.disk_usage(folder).free
    if free - needed < backups.RESERVE:
        raise Refused(
            f"{where} has {free // backups.MIB} MiB free; {what} needs {needed // backups.MIB} MiB, "
            f"and {backups.RESERVE // backups.MIB} MiB must stay free"
        )


def _export_target(exported: Path, world: str) -> tuple[Path, Path]:
    """Make the world's save folder in the install folder, in the layout the game reads: the folder,
    and what this export made, which a failure removes. Refuses a save folder already there."""
    layout = exported / "worlds_local"
    target = layout / world
    made_layout = not os.path.lexists(layout)
    try:
        if not made_layout and not stat.S_ISDIR(layout.lstat().st_mode):
            raise Refused("the install folder holds worlds_local, and it is no folder")
        if os.path.lexists(target):
            raise Refused(f"the install folder already holds worlds_local/{world}")
        layout.mkdir(exist_ok=True)
        target.mkdir()
    except OSError as error:
        if made_layout:
            shutil.rmtree(layout, ignore_errors=True)
        raise Refused(f"cannot write into the install folder: {error.strerror or error}") from error
    return target, layout if made_layout else target


def _save_folder(imported: Path, world: str) -> Path:
    """The one entry in the folder given to vhd import, once it is a folder named for the world, not a
    link."""
    entries = sorted(imported.iterdir())
    for entry in entries:
        if _is_pre_1_0(entry.name, world):
            raise Refused(
                f"the folder given to vhd import holds {entry.name}, a pre-1.0 save; an import takes a 1.0+ save only"
            )
    if len(entries) != 1:
        held = ", ".join(entry.name for entry in entries[:3]) or "nothing"
        raise Refused(
            f"the folder given to vhd import holds {held}; it must hold exactly one entry, the save folder {world}"
        )
    folder = entries[0]
    if folder.name != world:
        raise Refused(f"the folder given to vhd import holds {folder.name}, not the save folder {world}")
    mode = folder.lstat().st_mode
    if not stat.S_ISDIR(mode):
        raise Refused(f"the folder given to vhd import holds {world} as {_kind(mode)}, not a folder")
    return folder


def _is_pre_1_0(name: str, world: str) -> bool:
    return name in (f"{world}{suffix}" for suffix in _PRE_1_0)


def _save_files(folder: Path) -> dict[str, os.stat_result]:
    """Each file in the save folder as the scan finds it, once each is a regular file a save is made
    of. Never follows a link."""
    scanned: dict[str, os.stat_result] = {}
    for entry in sorted(folder.iterdir()):
        mode = entry.lstat()
        if not stat.S_ISREG(mode.st_mode):
            raise Refused(
                f"the save holds {entry.name} as {_kind(mode.st_mode)}, and an import takes regular files only"
            )
        if _is_pre_1_0(entry.name, folder.name):
            raise Refused(f"the save holds {entry.name}, a pre-1.0 save's file; an import takes a 1.0+ save only")
        if not restore.SAVE_FILE.fullmatch(entry.name):
            raise Refused(f"the save holds {entry.name}, which is no file of a save")
        scanned[entry.name] = mode
    return scanned


def _one_save(folder: Path, present: set[str]) -> int:
    """The save's number, once the folder holds exactly one complete save, all of its files and
    nothing else."""
    numbers = sorted(int(match[1]) for name in present if (match := backups.OK_FILE.match(name)))
    if not numbers:
        raise Refused("the save folder holds no complete save: no _main.<n>.ok")
    breach = backups.save_breach(numbers[-1])
    if breach is not None:
        raise Refused(breach)
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
        chunks, _ = backups.read_chunks(folder / f"_main.{number}.chunks", number)
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


def _keep(
    found: _Found, staging: Path, store: Path, now: Callable[[], datetime], contract: Draft202012Validator
) -> tuple[str, int]:
    """Copy the save into staging, check the copy, and name it as a backup of the world its folder is
    named for, once its record keeps its contract: the backup's name and the save's objects."""
    scanned, number = found.scanned, found.number
    files: list[dict[str, object]] = []
    # Chunks first and the .ok last, as the game writes a save.
    for name in sorted(scanned, key=lambda file: (file.startswith("_main."), file)):
        try:
            size = scanned[name].st_size
            with _stoppable():
                copied = backups.copy_file(found.folder / name, staging / name, size, unchanged=scanned[name])
        except backups.Gone as error:
            raise Refused(f"the save's {name} went after the import found it") from error
        except backups.Changed as error:
            raise Refused(f"the save's {name} changed after the import found it") from error
        except OSError as error:
            if error.errno == errno.EFBIG:
                raise Refused(f"the save's {name} grew while it was copied") from error
            raise
        if copied["size"] != size:
            raise Refused(f"the save's {name} changed after the import found it")
        files.append(copied)
    try:
        save = backups.newest_complete(staging)
    except backups.BackupFailed as error:
        raise Refused(f"the copy is not a complete save: {error}") from error
    if save is None or save.number != number or set(save.files) != set(scanned):
        raise Refused(f"the copy does not hold save {number}")
    record: dict[str, object] = {
        "world": found.folder.name,
        "save": number,
        "written_ns": found.written_ns,
        "objects": save.objects,
        "taken": "",
        "source": "import",
        "files": files,
    }
    try:
        named = backups.name_backup(staging, store, record, now(), contract)
    except backups.BackupFailed as error:
        raise Refused(str(error)) from error
    if named is None:
        raise Refused(f"the runtime's own backups took each name the import tried, {backups.NAME_ATTEMPTS} times")
    return named, save.objects


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "a link"
    if stat.S_ISDIR(mode):
        return "a folder"
    if stat.S_ISREG(mode):
        return "a file"
    return "a special file"


def _summary(number: int, objects: int, written_ns: int) -> SaveSummary:
    """The save, as the result names it: `written` is when the game completed it, in UTC, to the second."""
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
