"""vhd import and vhd export: a save into a world's backups, or a backup out of them, through the
game runtime's image (Game runtimes, Restore, import and export share one path).

vhd runs the image's import or export action (runtime/src/vhd_runtime/transfer.py) in a one-off
container it starts with the server manager's access to Docker, sandboxed, since the action needs
nothing more: as the image's own user, with no capability and no network, a read-only root, bounded
memory and processes, and a timeout. The action's last line on standard output is its result (B13), which
vhd checks against its contract: output that breaks it, or none, counts as refused. An import's
backup is then restored like any other: vhd writes its restore into the control plane's state, as
the control API's restore does, so the running control plane hands it at its next pass, and a world
with no container yet gets it in its first generation, so its first start loads it.

    <install folder>/<path>/                    the folder given to vhd import, holding only the save
                                                folder, named for the world; mounted read-only
    <backups folder>/<child id>/                the world's backups: read-write for an import,
                                                read-only for an export
    <install folder>/exports/<world>/<when>/    a folder per export, which the export copies the
                                                backup into, as worlds_local/<world>/
"""

import csv
import io
import json
import os
import posixpath
import secrets
import shutil
import signal
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import FrameType
from typing import Any, Literal, NoReturn, TextIO

from jsonschema import Draft202012Validator

from vhd_control import layout, manifest
from vhd_control.apply import LOCKED_DOWN_FLAGS, inspect_host, make_backup_folders, only_run, pull_missing
from vhd_control.docker import DockerError, DockerTimeout, OneOff
from vhd_control.manifest import Settings
from vhd_control.naming import child_id
from vhd_control.reconcile import Restore
from vhd_control.render import GAME_RUNTIME_IMAGE, Host, backups_folder
from vhd_control.schemas import Schemas
from vhd_control.store import Store

Action = Literal["import", "export"]

RESULT_CONTRACT = "contracts/game-runtime.import-export.schema.json"
# Where the action finds what vhd mounts (runtime/src/vhd_runtime/transfer.py).
IMPORT_FOLDER = PurePosixPath("/import")
EXPORT_FOLDER = PurePosixPath("/export")
# In the install folder: a folder per world, holding a folder per export.
EXPORTS = "exports"
# Well within the hour after which a start, or the next import, removes what an import left
# (runtime/src/vhd_runtime/backups.py, IMPORT_STAGING_MAX_AGE_S), so no cleanup meets an import still
# copying: a 4 GiB save, the most an import takes, copies within it at about 2.4 MB/s.
TIMEOUT_S = 1800
# How long a stopped action has to remove what it wrote and print its refusal before Docker kills it.
STOP_GRACE_S = 60
# A copy in 1 MiB blocks and a contract check need little of either.
MEMORY = "512m"
PIDS = 64
# What docker run exits with when Docker itself fails, before the container runs.
DOCKER_FAILED = 125
# What stops vhd itself: a Ctrl-C, or the SIGTERM tini forwards when Docker stops vhd's container.
STOP_SIGNALS = {signal.SIGINT, signal.SIGTERM}
# What the image's own user is, as the image says: its uid, its primary gid, and every gid it holds.
WHO = "id -u && id -g && id -G"


class Refused(Exception):
    """vhd import or vhd export did not do what it was asked. The message says why, and what to do
    instead."""


@dataclass(frozen=True)
class _User:
    """The game runtime image's own user, by number."""

    uid: int
    gid: int
    gids: frozenset[int]


@dataclass
class Transfer:
    install: Path
    generations: Path
    schemas: Schemas
    docker: OneOff
    hostname: str
    out: TextIO

    def import_save(self, world: str, path: str) -> None:
        """Keep the save in the folder at path, relative to the install folder, as a backup of the
        world, and ask for its restore. Refuses an absolute path, a .., a link, and a save the game
        runtime's user cannot read, before anything is made."""
        settings = manifest.load(self.install, self.schemas)
        _in_manifest(settings, world)
        parts = _relative(path)
        folder = _folder(self.install, parts, path)
        host = inspect_host(self.docker, self.hostname, self.install, "import")
        source = posixpath.join(host.install, *parts)
        backups = backups_folder(settings.backups.path, host)
        if posixpath.commonpath([source, backups]) == backups:
            raise Refused(
                f"{path} is in the backups folder, which the worlds' runtimes write; move the save into a folder "
                "of its own in the install folder"
            )
        cid = child_id(world)
        store = Store(self.generations, host.owner)
        with _stopped_as_interrupted(), only_run(self.generations):
            user = self._user(host)
            try:
                unreadable = _unreadable(folder, user)
            except OSError as error:
                raise Refused(f"cannot read {path} in the install folder: {error.strerror or error}") from error
            if unreadable is not None:
                shown = posixpath.join(path, unreadable) if unreadable else path
                raise Refused(
                    f"the game runtime's user cannot read {shown}; make the folder and everything in it readable "
                    f"by others, as: chmod -R o+rX {path}"
                )
            try:
                make_backup_folders(self.docker, host, backups, [cid])
            except DockerError as error:
                raise Refused(
                    f"vhd import could not make {world}'s folder in {backups} ({error}). Nothing was imported. Check "
                    "that Docker can run the game runtime image, and that the world's folder there is no link or "
                    "file; then run vhd import again."
                ) from error
            mounts = [
                *_mount(source, IMPORT_FOLDER, read_only=True),
                *_mount(posixpath.join(backups, cid), layout.BACKUPS, read_only=False),
            ]
            result = self._act(host, "import", world, mounts, [])
            restore = Restore(secrets.token_hex(16), result["backup"])
            with store.locked():
                state = store.load()
                state.restores[cid] = restore
                store.save(state)
            started = cid in (state.applied or {})
        then = (
            "The world's runtime restores it once no players are online, or now with vhd apply --disconnect-players; "
            "vhd status shows while it waits, and how it ends."
            if started
            else "The world has no container yet: vhd apply starts it, and its first start loads the import."
        )
        print(
            f"vhd import: {world}'s save {_save(result)} is kept as backup {result['backup']}, and restore "
            f"{restore.id} of it is asked for. {then}",
            file=self.out,
        )

    def export_backup(self, world: str, backup: str | None) -> None:
        """Copy one of the world's backups, the newest unless one is named, into a folder of its own in
        the install folder, in the layout the game reads, and give it to the install folder's owner.
        Any world with a save name may have backups, one the manifest dropped among them: they stay
        where the world's container mounted them, which the manifest names only until vhd apply has
        created one."""
        if not self._fits("schema/manifest.schema.json", ("$defs", "save_name"), world):
            raise Refused(f"{world} is no save name: 1 to 64 letters, digits, spaces, hyphens and underscores")
        host = inspect_host(self.docker, self.hostname, self.install, "export")
        with _stopped_as_interrupted(), only_run(self.generations):
            mounted = Store(self.generations).load().backups_applied
            backups = backups_folder(mounted or manifest.load(self.install, self.schemas).backups.path, host)
            folder = posixpath.join(backups, child_id(world))
            seen = _in_view(self.install, host.install, folder)
            # Docker would only fail to mount it. One outside the install folder vhd cannot see.
            if seen is not None and not seen.is_dir():
                raise Refused(
                    f"there is no backup of {world} yet: the install folder holds no {seen.relative_to(self.install)}"
                )
            user = self._user(host)
            target, made = _export_folder(self.install, world, host.owner, user)
            try:
                mounts = [
                    *_mount(folder, layout.BACKUPS, read_only=True),
                    *_mount(posixpath.join(host.install, *target.relative_to(self.install).parts), EXPORT_FOLDER),
                ]
                result = self._act(host, "export", world, mounts, [] if backup is None else [backup])
            except BaseException:
                _remove(made)
                raise
            shown = posixpath.join(*target.relative_to(self.install).parts, "worlds_local", world)
            try:
                _give(target, host.owner)
            except OSError as error:
                raise Refused(
                    f"the backup is in {shown}, but vhd could not give it to the install folder's owner: "
                    f"{error.strerror or error}"
                ) from error
        print(
            f"vhd export: {world}'s backup {result['backup']}, save {_save(result)}, is in {shown}, in the layout "
            "the game reads.",
            file=self.out,
        )

    def _fits(self, document: str, at: tuple[str, ...], value: str) -> bool:
        """Whether value meets the schema at that place in a document the release ships."""
        schema: Any = self.schemas.document(document)
        for key in at:
            schema = schema[key]
        return Draft202012Validator(schema).is_valid(value)

    def _user(self, host: Host) -> _User:
        """The game runtime image's own user, as the image says, so its Dockerfile alone names the uid.
        The image is pulled first when the host lacks it, as before a first vhd apply."""
        image = host.images[GAME_RUNTIME_IMAGE]
        pull_missing(self.docker, [image])
        try:
            said = self.docker.output(
                [
                    "run", "--rm", "--pull", "never", "--network", "none", "--read-only", *LOCKED_DOWN_FLAGS,
                    "--entrypoint", "sh", image, "-c", WHO,
                ]
            )  # fmt: skip
            uid, gid, gids = said.splitlines()[:3]
            return _User(int(uid), int(gid), frozenset(int(each) for each in gids.split()))
        except (DockerError, ValueError) as error:
            raise Refused(
                f"cannot tell which user the game runtime image runs as ({error}); nothing was made"
            ) from error

    def _act(self, host: Host, action: Action, world: str, mounts: list[str], more: list[str]) -> dict[str, Any]:
        """Run the action in its sandbox and return its result, once it says done. A container that
        outlasts TIMEOUT_S, or whose run vhd's own stops, is stopped too, not only its docker CLI."""
        name = f"{host.project}-{action}-{secrets.token_hex(8)}"
        args = [
            "run", "--rm", "--name", name, "--pull", "never", "--network", "none", "--read-only",
            *LOCKED_DOWN_FLAGS, "--memory", MEMORY, "--pids-limit", str(PIDS),
            *mounts, host.images[GAME_RUNTIME_IMAGE], action, world, *more,
        ]  # fmt: skip
        try:
            ran = self.docker.run(args, TIMEOUT_S)
        except DockerTimeout as error:
            failed = self._stop(name)
            left = f"; stopping its container failed too: {'; '.join(failed)}" if failed else ""
            raise Refused(
                f"vhd {action} did not finish within {TIMEOUT_S} s, so vhd stopped it{_kept(action)}{left}"
            ) from error
        except BaseException:
            # Whatever stopped vhd, the container must not outlive it.
            self._stop(name)
            raise
        if ran.status == DOCKER_FAILED:
            # The container never ran, so the action kept nothing.
            why = "; ".join(ran.errors) or "it gave no reason"
            raise Refused(f"Docker could not start the game runtime image for vhd {action}: {why}")
        return self._result(action, world, ran.status, ran.output)

    def _stop(self, name: str) -> list[str]:
        """Stop the container: a SIGTERM, which the action answers by removing what it wrote and
        printing its refusal, and a kill after STOP_GRACE_S; then remove it, unless --rm already has.
        Each step is tried whatever the one before did, and one that failed counts only while the
        container is still there. Returns how each such step failed."""
        failed: list[str] = []
        # Held until both steps are tried: a second Ctrl-C must not leave the container behind.
        held = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
        try:
            for args in (["stop", "--timeout", str(STOP_GRACE_S), name], ["rm", "--force", name]):
                try:
                    self.docker.output(args)
                except DockerError as error:
                    if self._there(name):
                        failed.append(str(error))
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, held)
        return failed

    def _there(self, name: str) -> bool:
        """Whether the container exists, by its exact name; one Docker cannot say of counts as there."""
        try:
            listed = self.docker.output(["container", "ls", "--all", "--quiet", "--filter", f"name=^{name}$"])
        except DockerError:
            return True
        return bool(listed.strip())

    def _result(self, action: Action, world: str, status: int, output: str) -> dict[str, Any]:
        """The action's result, its last line of output, once it keeps its contract and says done
        with an exit status of 0. Raises Refused with the reason the result gives, or why it counts as
        refused (B13): an import's refusal then says it may have kept its backup all the same."""
        kept = _kept(action)
        lines = output.strip().splitlines()
        if not lines:
            raise Refused(f"the game runtime image gave vhd {action} no result, and exited with {status}{kept}")
        try:
            result: Any = json.loads(lines[-1])
        except ValueError as error:
            raise Refused(
                f"the game runtime image gave vhd {action} a result that is no JSON: {error}{kept}"
            ) from error
        breach = self.schemas.problem(RESULT_CONTRACT, result)
        if breach is not None:
            raise Refused(f"the game runtime image's result breaks its contract {breach}{kept}")
        if result["action"] != action or result["world"] != world:
            raise Refused(f"the game runtime image's result is for {result['action']} of {result['world']}{kept}")
        if result["outcome"] == "refused":
            raise Refused(str(result["reason"]))
        if status != 0:
            raise Refused(f"the game runtime image said vhd {action} was done, but exited with {status}{kept}")
        return dict(result)


def _kept(action: Action) -> str:
    """What a refusal of an import adds when the action's own word is missing or broken (B13)."""
    if action != "import":
        return ""
    return "; an import cut short after it named its backup may have kept it, which vhd backups then lists"


@contextmanager
def _stopped_as_interrupted() -> Iterator[None]:
    """A SIGTERM stops vhd as a Ctrl-C does, so what stops vhd stops the action's container too and
    removes what vhd made, rather than end vhd where it stands."""
    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _interrupt(signum: int, frame: FrameType | None) -> NoReturn:
    raise KeyboardInterrupt


def _in_manifest(settings: Settings, world: str) -> None:
    if world not in (each.name for each in settings.worlds):
        raise Refused(f"the manifest has no world {world}; add it as [world.{world}] first")


def _in_view(install: Path, host_install: str, host_path: str) -> Path | None:
    """Where a host path is in vhd's view of the install folder, or None outside it."""
    if posixpath.commonpath([host_path, host_install]) != host_install:
        return None
    return install.joinpath(*PurePosixPath(posixpath.relpath(host_path, host_install)).parts)


def _relative(path: str) -> tuple[str, ...]:
    """The folder's parts, below the install folder."""
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise Refused(f"{path} is an absolute path; name the folder relative to the install folder")
    if ".." in pure.parts:
        raise Refused(f"{path} holds .., which could leave the install folder; name the folder inside it")
    if not pure.parts:
        raise Refused("name the folder that holds the save folder, inside the install folder, not the folder itself")
    return pure.parts


def _folder(install: Path, parts: tuple[str, ...], path: str) -> Path:
    """The folder in vhd's view of the install folder, once no part of the way to it is a link,
    since Docker's mount would follow one wherever it points."""
    folder = install
    for part in parts:
        folder /= part
        try:
            mode = folder.lstat().st_mode
        except FileNotFoundError:
            raise Refused(f"the install folder holds no {path}") from None
        except OSError as error:
            raise Refused(f"cannot read {path} in the install folder: {error.strerror or error}") from error
        if stat.S_ISLNK(mode):
            raise Refused(f"{folder.relative_to(install)} in the install folder is a link; name the folder itself")
        if not stat.S_ISDIR(mode):
            raise Refused(f"{folder.relative_to(install)} in the install folder is no folder")
    return folder


def _unreadable(folder: Path, user: _User) -> str | None:
    """The first thing in the folder the user cannot read, relative to it, "" for the folder itself,
    or None. It looks as deep as a save folder's files, never through a link: what the action
    refuses anyway, it leaves to the action. Access lists are not read."""
    if not _may(folder.lstat(), user, folders=True):
        return ""
    for entry in sorted(folder.iterdir()):
        info = entry.lstat()
        if stat.S_ISREG(info.st_mode) and not _may(info, user, folders=False):
            return entry.name
        if not stat.S_ISDIR(info.st_mode):
            continue
        if not _may(info, user, folders=True):
            return entry.name
        for file in sorted(entry.iterdir()):
            inner = file.lstat()
            if stat.S_ISREG(inner.st_mode) and not _may(inner, user, folders=False):
                return posixpath.join(entry.name, file.name)
    return None


def _may(info: os.stat_result, user: _User, *, folders: bool) -> bool:
    """Whether the user may read a file, or list and enter a folder, by its mode."""
    if info.st_uid == user.uid:
        bits = info.st_mode >> 6
    elif info.st_gid in user.gids:
        bits = info.st_mode >> 3
    else:
        bits = info.st_mode
    needed = 0o5 if folders else 0o4
    return bits & needed == needed


def _export_folder(install: Path, world: str, owner: tuple[int, int], user: _User) -> tuple[Path, list[Path]]:
    """A new folder for this export, exports/<world>/<when>, which the image's user may write: the
    folder, and what vhd made for it, newest first, which a refusal removes."""
    made: list[Path] = []
    try:
        folder = install
        for part in (EXPORTS, world):
            folder /= part
            try:
                mode = folder.lstat().st_mode
            except FileNotFoundError:
                folder.mkdir()
                made.insert(0, folder)
                os.chown(folder, *owner)
                continue
            # A link is no folder: vhd would make the export, and give it away, wherever it points.
            if not stat.S_ISDIR(mode):
                raise Refused(f"{folder.relative_to(install)} in the install folder is no folder")
        when = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = folder / when
        for again in range(2, 100):
            try:
                target.mkdir(mode=0o755)
                break
            except FileExistsError:
                target = folder / f"{when}-{again}"
        else:
            raise Refused(f"{folder.relative_to(install)} already holds this second's exports; try again")
        made.insert(0, target)
        os.chown(target, user.uid, user.gid)
    except OSError as error:
        _remove(made)
        raise Refused(f"cannot make the export's folder in the install folder: {error.strerror or error}") from error
    except Refused:
        _remove(made)
        raise
    return target, made


def _remove(made: list[Path]) -> None:
    for folder in made:
        shutil.rmtree(folder, ignore_errors=True)


def _give(target: Path, owner: tuple[int, int]) -> None:
    """Give what the export wrote to the install folder's owner, never through a link."""
    os.lchown(target, *owner)
    for root, folders, files in os.walk(target):
        for name in (*folders, *files):
            os.lchown(os.path.join(root, name), *owner)


def _mount(source: str, target: PurePosixPath, *, read_only: bool = False) -> list[str]:
    """A bind mount, as docker run's --mount: unlike --volume it refuses a source that is not there
    rather than make it as root, and its fields are CSV, which carries any character a path holds."""
    fields = ["type=bind", f"source={source}", f"target={target}"]
    if read_only:
        fields.append("readonly")
    line = io.StringIO()
    csv.writer(line).writerow(fields)
    return ["--mount", line.getvalue().removesuffix("\r\n")]


def _save(result: dict[str, Any]) -> str:
    """The save a result names, for the server manager."""
    save = result["save"]
    return f"{save['number']} ({save['objects']} objects, completed {save['written']})"
