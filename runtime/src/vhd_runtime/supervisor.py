"""The runtime's state machine: generation, install, launch, world truth, backups, stop and hold."""

import json
import logging
import os
import random
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Protocol

from vhd_runtime import backups, server, status, steam
from vhd_runtime.generation import Generation, IncompleteGeneration, InvalidGeneration, Reader

# Well below the game's autosave interval (30 minutes unless changed), so a
# world held at this point has not saved.
LOAD_TIMEOUT_S = 900
# vhd apply's stop grace for a world (render.STOP_GRACE) must exceed this, with room after it for
# a backup of the save the game stopped with.
STOP_TIMEOUT_S = 120
POLL_S = 1.0
GENERATION_POLL_S = 2.0
# A world status file the plugin has not rewritten for this many of its intervals says nothing:
# a plugin that throws after a game patch leaves its last count behind.
WORLD_STALE_AFTER = 3
# The final players check trusts a count only this many intervals old, as the control plane
# does: an older one may predate a player who joined since.
EMPTY_WITHIN = 1.5
HELD_AFTER_OBJECTS = "the world loaded with no objects where it last held {last}; stopped before its first autosave"
HELD_OVER_SAVE = "the world loaded with no objects although a save existed; stopped before its first autosave"
HELD_UNREPORTED = (
    "the plugin has not reported the loaded world, which held objects before; stopped before its first autosave"
)
UNVERIFIED_REASON = "the plugin has not reported the loaded world"
# A failed install is retried after a wait that starts here and doubles up to the cap, each wait
# jittered, so the worlds of one install do not all go back to Steam at once.
INSTALL_RETRY_FIRST_S = 30
INSTALL_RETRY_MAX_S = 600
# The runtime status contract's bound on a reason.
REASON_MAX = 500
# How often a running world looks for a save newer than its last backup, once its settings' interval
# has passed, and how long it waits to try again after a backup failed.
BACKUP_CHECK_S = 15
BACKUP_RETRY_S = 60
# The runtime status contract's bound on the backups it names.
BACKUP_NAMES_MAX = 200

_log = logging.getLogger(__name__)


class State(StrEnum):
    INSTALLING = "installing"
    LOADING = "loading"
    RUNNING = "running"
    UNVERIFIED = "unverified"
    HELD = "held"


@dataclass(frozen=True)
class Paths:
    generation: Path = Path("/generation")
    data: Path = Path("/data")
    pack: Path = Path("/opt/vhd/bepinex")
    plugin: Path = Path("/opt/vhd/plugin") / server.PLUGIN_FILE
    contracts: Path = Path("/opt/vhd/contracts")
    world_status: Path = Path("/run/vhd/world.json")
    # The world's own folder of the install's backups, which vhd apply makes and mounts here:
    # control/src/vhd_control/layout.py, BACKUPS.
    backups: Path = Path("/backups")

    @property
    def steamcmd(self) -> Path:
        return self.data / "steamcmd"

    @property
    def server(self) -> Path:
        return self.data / "server"

    @property
    def saves(self) -> Path:
        return self.data / "saves"

    @property
    def map_export(self) -> Path:
        """Where the plugin writes the world's map export (B5), in the data volume. The web map
        mounts it read-only by this name, map-export, under the volume (docs/architecture.md), and
        vhd apply makes it too: control/src/vhd_control/layout.py, MAP_EXPORT."""
        return self.data / "map-export"

    @property
    def last_objects(self) -> Path:
        """The object count the runtime last saw its world hold, kept in the data volume."""
        return self.data / "vhd" / "last-objects.json"


class Process(Protocol):
    def poll(self) -> int | None: ...
    def send_signal(self, sig: int) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


Launch = Callable[[list[str], dict[str, str], Path], Process]


def launch(command: list[str], env: dict[str, str], cwd: Path) -> Process:
    return subprocess.Popen(command, env=env, cwd=cwd)  # noqa: S603 - the server binary, with arguments from a validated generation


@dataclass
class Clock:
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Supervisor:
    paths: Paths
    reader: Reader
    launch: Launch = launch
    fetch: steam.Fetch = steam.fetch
    run_steamcmd: steam.Run = steam.run
    post: status.Post = status.post
    clock: Clock = field(default_factory=Clock)
    jitter: Callable[[float, float], float] = random.uniform
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    # Copies a backup off the supervisor's loop, which keeps reporting and watching the world.
    executor: Executor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=1))
    wall: Callable[[], datetime] = lambda: datetime.now(UTC)
    _stop: bool = field(default=False, init=False)
    _problem: str | None = field(default=None, init=False)
    _remembered: int | None = field(default=None, init=False)
    # The newest write of the world status file, and when the supervisor first saw it.
    _world_stamp: tuple[int, int] | None = field(default=None, init=False)
    _world_seen: float = field(default=0.0, init=False)
    # The generation a restart waits on, so the wait is logged once.
    _waiting: int | None = field(default=None, init=False)
    # The backup being copied; the save number the newest backup holds; when the loop next looks
    # for a newer save; the backups the folder holds, newest first, None until listed; and why the
    # last backup failed.
    _backup: Future[backups.Backup | None] | None = field(default=None, init=False)
    _backed_up: int | None = field(default=None, init=False)
    _next_backup: float = field(default=0.0, init=False)
    _backup_names: list[str] | None = field(default=None, init=False)
    _backup_failure: str | None = field(default=None, init=False)

    def request_stop(self, signum: int, frame: FrameType | None) -> None:
        """Signal handler: stop at the next poll, saving the world first."""
        self._stop = True

    def run(self) -> int:
        generation = self._wait_for_generation()
        if generation is None:
            return 0
        reporter = status.Reporter(generation.status_url, self.post)
        build = self._install(generation, reporter)
        if build is None:
            return 0
        # The control plane may have handed a newer generation while the server installed.
        generation = self._read_generation() or generation
        reporter = status.Reporter(generation.status_url, self.post)
        self._map_export_directory()
        self._start_backups(generation)
        config = server.plugin_config(generation, self.paths.world_status, self.paths.map_export)
        server.install_mods(self.paths.pack, self.paths.plugin, self.paths.server, config)
        save_existed = server.save_exists(self.paths.saves, generation.world)
        # A report from an earlier run of this container must not pass for this one.
        self.paths.world_status.unlink(missing_ok=True)
        _log.info(
            "launching world %s on port %d, save %s",
            generation.world,
            generation.port,
            "found" if save_existed else "absent: the game creates a new world",
        )
        process = self.launch(
            server.command(self.paths.server, self.paths.saves, generation),
            server.environment(self.paths.server, self.environ),
            self.paths.server,
        )
        return self._watch(process, generation, reporter, save_existed, build)

    def _map_export_directory(self) -> None:
        """The directory the plugin writes the map export into, made on every start. The export is
        an observation: a directory that cannot be made or written costs the web map its render,
        never the world its start."""
        directory = self.paths.map_export
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            _log.error("cannot make the map export directory %s, so the web map gets no export: %s", directory, error)
            return
        if not os.access(directory, os.W_OK):
            _log.error("the map export directory %s is not writable, so the web map gets no export", directory)

    def _install(self, generation: Generation, reporter: status.Reporter) -> str | None:
        """SteamCMD and the dedicated server, installed or updated: the server's build id, or
        None when a stop arrived first, so nothing launches. Status keeps reporting the
        install, which can take many minutes, so the control plane never takes it for silence.
        A failed install is retried until it succeeds or a stop arrives, its reason in status, so
        a host that cannot reach Steam shows why instead of restarting the container."""
        installing: status.RuntimeStatus = {
            "contract": 1,
            "generation": generation.number,
            "world": generation.world,
            "state": State.INSTALLING,
        }
        reporter.send(installing)
        next_report = self.clock.now() + generation.status_interval_s

        def stopping() -> bool:
            nonlocal next_report
            if self.clock.now() >= next_report:
                reporter.send(installing)
                next_report = self.clock.now() + generation.status_interval_s
            return self._stop

        retry_s: float = INSTALL_RETRY_FIRST_S
        try:
            while not self._stop:
                try:
                    steamcmd = steam.ensure_steamcmd(self.paths.steamcmd, self.fetch)
                    if not self._stop:
                        return steam.ensure_server(steamcmd, self.paths.server, self.run_steamcmd, stopping)
                except steam.InstallFailed as error:
                    wait = self.jitter(retry_s / 2, retry_s)
                    _log.error("the install failed, retrying in %d s: %s", wait, error)
                    installing["reason"] = _reason(f"the last install attempt failed, and is retried: {error}")
                    reporter.send(installing)
                    deadline = self.clock.now() + wait
                    while self.clock.now() < deadline and not stopping():
                        self.clock.sleep(POLL_S)
                    retry_s = min(retry_s * 2, INSTALL_RETRY_MAX_S)
        except steam.Stopped:
            pass
        _log.info("stopping: stop requested while installing")
        return None

    def _watch(
        self, process: Process, generation: Generation, reporter: status.Reporter, save_existed: bool, build: str
    ) -> int:
        launched = self.clock.now()
        state, reason = State.LOADING, None
        world: status.WorldStatus | None = None
        next_report = launched
        while True:
            if self._stop:
                return self._shut_down(process, state, "stop requested", generation)
            if state is not State.HELD:
                code = process.poll()
                if code is not None:
                    _log.error("the dedicated server exited with code %d", code)
                    return code
                world = self._read_world(generation.world) or world
            fresh = world if self._seen_within(WORLD_STALE_AFTER, generation) else None
            current = world if self._seen_within(EMPTY_WITHIN, generation) else None
            generation, newer = self._follow(generation)
            if newer is not None and self._may_restart(state, newer, current):
                return self._shut_down(process, state, "a newer generation changes this world's settings", generation)
            previous = state
            if state in (State.LOADING, State.UNVERIFIED) and fresh is not None:
                state, reason = self._on_report(process, generation.world, fresh.objects, save_existed)
            if state is State.RUNNING and fresh is not None:
                self._remember_objects(generation.world, fresh.objects)
            elif state is State.LOADING and self.clock.now() - launched > LOAD_TIMEOUT_S:
                state, reason = self._on_load_timeout(process, generation.world, save_existed)
            if state in (State.RUNNING, State.UNVERIFIED):
                self._back_up(generation)
            if state is not previous or self.clock.now() >= next_report:
                report = _status(generation, state, reason, build, fresh)
                backed_up = self._backups_status()
                if backed_up is not None:
                    report["backups"] = backed_up
                reporter.send(report)
                next_report = self.clock.now() + generation.status_interval_s
            self.clock.sleep(POLL_S)

    def _read_world(self, world: str) -> status.WorldStatus | None:
        """The plugin's report, noting when a new write of it first appeared."""
        read = status.read_world(self.paths.world_status, world)
        if read is not None and read.stamp != self._world_stamp:
            self._world_stamp, self._world_seen = read.stamp, self.clock.now()
        return read

    def _seen_within(self, intervals: float, generation: Generation) -> bool:
        """Whether the plugin rewrote its report within this many of its intervals."""
        if self._world_stamp is None:
            return False
        return self.clock.now() - self._world_seen <= intervals * generation.status_interval_s

    def _may_restart(self, state: State, newer: Generation, world: status.WorldStatus | None) -> bool:
        """The final players check, made here because only the runtime sees the count at the
        moment it would stop the game. A held world has no game. A generation the server manager
        confirmed goes now, cutting a load short by a kill, which saves nothing. Anything else
        waits for a current report of no players, so no load ends without that confirmation."""
        if state is State.HELD or newer.disconnect_players:
            return True
        if state in (State.RUNNING, State.UNVERIFIED) and world is not None and world.players == 0:
            return True
        if self._waiting != newer.number:
            players = "unknown" if world is None or state is State.LOADING else str(world.players)
            _log.info("generation %d waits for an empty world; players %s", newer.number, players)
            self._waiting = newer.number
        return False

    def _on_report(self, process: Process, world: str, objects: int, save_existed: bool) -> tuple[State, str | None]:
        """World truth on the plugin's first report: run the world, or hold it if it loaded empty."""
        held = self._loaded_empty(world, objects, save_existed)
        if held is not None:
            return self._hold(process, held)
        _log.info("world %s loaded with %d objects", world, objects)
        return State.RUNNING, None

    def _on_load_timeout(self, process: Process, world: str, save_existed: bool) -> tuple[State, str | None]:
        """World truth when the plugin never reported: what loaded is unknown, so a world with
        something to lose is held, and one with nothing to lose runs unverified."""
        if self._expects_objects(self._last_objects(world), save_existed):
            return self._hold(process, HELD_UNREPORTED)
        _log.warning("unverified: %s", UNVERIFIED_REASON)
        return State.UNVERIFIED, UNVERIFIED_REASON

    def _hold(self, process: Process, reason: str) -> tuple[State, str]:
        # Killed, not interrupted: SIGINT would save whatever loaded over the real world.
        _kill(process)
        _log.error("held: %s", reason)
        return State.HELD, reason

    @staticmethod
    def _expects_objects(last: int | None, save_existed: bool) -> bool:
        """Whether the world held objects when last seen, or, with nothing remembered, had a save."""
        return save_existed if last is None else last > 0

    def _loaded_empty(self, world: str, objects: int, save_existed: bool) -> str | None:
        """Why the world must be held, or None. An empty load fails world truth when the
        world held objects when last seen, or, with nothing remembered, when a save existed.
        A world last seen empty, such as one stopped moments after its creation, may load empty."""
        last = self._last_objects(world)
        if objects > 0 or not self._expects_objects(last, save_existed):
            return None
        return HELD_OVER_SAVE if last is None else HELD_AFTER_OBJECTS.format(last=last)

    def _last_objects(self, world: str) -> int | None:
        try:
            record = json.loads(self.paths.last_objects.read_text())
        except (OSError, ValueError):
            return None
        objects = record.get("objects") if isinstance(record, dict) and record.get("world") == world else None
        return objects if isinstance(objects, int) and objects >= 0 else None

    def _remember_objects(self, world: str, objects: int) -> None:
        if objects == self._remembered:
            return
        record = self.paths.last_objects
        record.parent.mkdir(parents=True, exist_ok=True)
        temporary = record.with_suffix(".tmp")
        temporary.write_text(json.dumps({"world": world, "objects": objects}))
        temporary.replace(record)
        self._remembered = objects

    def _shut_down(self, process: Process, state: State, why: str, generation: Generation) -> int:
        _log.info("stopping: %s", why)
        if state is State.HELD or process.poll() is not None:
            return 0
        if state is State.LOADING:
            # World truth has not passed, and SIGINT would save whatever has loaded.
            _kill(process)
            return 0
        # The dedicated server saves the world on SIGINT.
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _log.error("the dedicated server did not stop within %d s; killing it", STOP_TIMEOUT_S)
            _kill(process)
        self._back_up_at_stop(generation)
        return 0

    def _start_backups(self, generation: Generation) -> None:
        """List the world's backups, removing copies a stop cut short and those the settings no
        longer keep, and carry on from the newest: its save is backed up, and the next backup is
        due once the settings' interval has passed since it was taken. A folder that cannot be read
        or written costs the world its backups, never its start."""
        folder = self.paths.backups
        try:
            backups.clear_partials(folder)
            held = backups.prune(folder, generation.backups.keep, generation.backups.keep_daily)
        except OSError as error:
            self._backup_failed(f"cannot read the backup folder {folder}: {error.strerror or error}")
            return
        self._backup_names = held
        if not os.access(folder, os.W_OK):
            self._backup_failed(f"cannot write the backup folder {folder}")
        if held:
            self._backed_up = backups.last_save(folder, held[0])
            age = (self.wall() - backups.taken(held[0])).total_seconds()
            self._next_backup = self.clock.now() + max(0.0, generation.backups.every_s - age)

    def _back_up(self, generation: Generation) -> None:
        """While the world runs: once the settings' interval has passed since the last backup, back
        up a save newer than it, copied off this loop. The game is never paused."""
        if self._backup is not None:
            if not self._backup.done():
                return
            self._settle(self._backup, generation)
        if self.clock.now() < self._next_backup:
            return
        folder = backups.world_folder(self.paths.saves, generation.world)
        self._backup = self.executor.submit(
            backups.take, folder, self.paths.backups, generation.world, self.wall(), self._backed_up
        )

    def _back_up_at_stop(self, generation: Generation) -> None:
        """Back up the save the game stopped with, once any backup still copying is done."""
        if self._backup is not None:
            self._settle(self._backup, generation)
        folder = backups.world_folder(self.paths.saves, generation.world)
        try:
            backup = backups.take(folder, self.paths.backups, generation.world, self.wall(), self._backed_up)
        except (backups.BackupFailed, OSError) as error:
            self._backup_failed(str(error))
            return
        self._took(backup, generation)

    def _settle(self, copied: Future[backups.Backup | None], generation: Generation) -> None:
        """Note how a backup copied off the loop went; it waits for one still copying."""
        self._backup = None
        try:
            backup = copied.result()
        except (backups.BackupFailed, OSError) as error:
            self._backup_failed(str(error))
            self._next_backup = self.clock.now() + BACKUP_RETRY_S
            return
        self._took(backup, generation)

    def _took(self, backup: backups.Backup | None, generation: Generation) -> None:
        """Note a backup taken, and keep what the settings say; or, with none to take, look again
        a little later."""
        if backup is None:
            self._next_backup = self.clock.now() + BACKUP_CHECK_S
            return
        _log.info("backed up save %d of world %s as %s", backup.save, generation.world, backup.name)
        self._backed_up = backup.save
        self._backup_failure = None
        self._next_backup = self.clock.now() + generation.backups.every_s
        try:
            self._backup_names = backups.prune(
                self.paths.backups, generation.backups.keep, generation.backups.keep_daily
            )
        except OSError as error:
            self._backup_failed(f"cannot remove old backups from {self.paths.backups}: {error.strerror or error}")

    def _backup_failed(self, why: str) -> None:
        if why != self._backup_failure:
            _log.error("no backup: %s", why)
        self._backup_failure = _reason(why)

    def _backups_status(self) -> status.Backups | None:
        """The backups for the status report, None until the folder has been listed or has failed."""
        if self._backup_names is None and self._backup_failure is None:
            return None
        report: status.Backups = {"names": (self._backup_names or [])[:BACKUP_NAMES_MAX]}
        if self._backup_failure is not None:
            report["failure"] = self._backup_failure
        return report

    def _wait_for_generation(self) -> Generation | None:
        while not self._stop:
            generation = self._read_generation()
            if generation is not None:
                return generation
            self.clock.sleep(GENERATION_POLL_S)
        return None

    def _follow(self, current: Generation) -> tuple[Generation, Generation | None]:
        """The generation to run on, and a newer one that takes a restart, if any. A newer
        generation that changes only what the runtime adopts in place, its backups and updates,
        runs on without one."""
        newer = self._read_generation()
        if newer is None or newer.number <= current.number:
            return current, None
        if newer.same_settings(current):
            _log.info("generation %d changes nothing %d restarts for; running on", newer.number, current.number)
            return newer, None
        return current, newer

    def _read_generation(self) -> Generation | None:
        """The complete, valid generation on disk, or None. An invalid one is refused, and an
        incomplete one waited for, each logged once."""
        try:
            generation = self.reader.read()
        except (InvalidGeneration, IncompleteGeneration) as error:
            if str(error) != self._problem:
                if isinstance(error, InvalidGeneration):
                    _log.error("refusing the generation: %s", error)
                else:
                    _log.info("waiting: %s", error)
            self._problem = str(error)
            return None
        self._problem = None
        return generation


def _kill(process: Process) -> None:
    process.kill()
    process.wait()


def _status(
    generation: Generation, state: State, reason: str | None, build: str, world: status.WorldStatus | None
) -> status.RuntimeStatus:
    report: status.RuntimeStatus = {
        "contract": 1,
        "generation": generation.number,
        "world": generation.world,
        "state": state,
        "game_build": build,
    }
    if reason is not None:
        report["reason"] = reason
    if world is not None:
        report["objects"] = world.objects
        report["players"] = world.players
    return report


def _reason(text: str) -> str:
    """One line of printable text within the contract's bound: an error's text is not ours."""
    return "".join(c if c.isprintable() else " " for c in text)[:REASON_MAX]
