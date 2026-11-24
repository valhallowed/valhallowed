"""The runtime's state machine: generation, restore, install, launch, world truth, backups, updates, stop and
hold."""

import contextlib
import json
import logging
import os
import random
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Protocol

from jsonschema import Draft202012Validator

from vhd_runtime import backups, restore, server, status, steam
from vhd_runtime.generation import Generation, IncompleteGeneration, InvalidGeneration, Reader, Restore

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
# What the server manager can do for a world held with no save.
HELD_UNSAVED_ASK = (
    "ask for another restore or an import, or remove the world's data volume to start a new world, which also "
    "removes its installed server, so its next start downloads it again"
)
# A failed install is retried after a wait that starts here and doubles up to the cap, each wait
# jittered, so the worlds of one install do not all go back to Steam at once.
INSTALL_RETRY_FIRST_S = 30
INSTALL_RETRY_MAX_S = 600
# The runtime status contract's bound on a reason.
REASON_MAX = 500
# How often a running world looks for a save newer than its last backup, once its settings' interval
# has passed; and how long it waits to try again after a backup failed, doubling each time up to
# that interval, so a full disk is not filled again every minute.
BACKUP_CHECK_S = 15
BACKUP_RETRY_S = 60
# The runtime status contract's bound on the backups it names: what pruning keeps at most, the counts'
# 100 each, the backup a restore asks for and the newest import.
BACKUP_NAMES_MAX = 202
# After a restart to update whose start did not install its build, the next restart for that build waits
# this long, doubling after each that misses it again, up to the cap: a download that keeps failing never
# restarts an empty world every check, and one that failed once, as when a release overloads Steam, is
# tried again the same day.
UPDATE_RETRY_FIRST_S = 3600
UPDATE_RETRY_MAX_S = 86400
# How the record of a restart to update writes its time.
RECORD_TIME = "%Y-%m-%dT%H:%M:%SZ"

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
    def app_info_cache(self) -> Path:
        """Where SteamCMD keeps app info, in its Steam folder in its home, which the image makes the
        data volume (images/game-runtime/Dockerfile)."""
        return self.data / "Steam" / "appcache" / "appinfo.vdf"

    @property
    def vhd(self) -> Path:
        """The runtime's own records in the data volume."""
        return self.data / "vhd"

    @property
    def update_record(self) -> Path:
        """The game build the runtime last restarted its world to update to, kept in the data volume."""
        return self.vhd / "update.json"

    @property
    def last_objects(self) -> Path:
        """The object count the runtime last saw its world hold, kept in the data volume."""
        return self.vhd / "last-objects.json"


@dataclass(frozen=True)
class Missed:
    """A restart to update whose start did not install its build: that build, how many restarts have
    tried it, and when the last began."""

    build: str
    attempts: int
    at: datetime

    def next_at(self) -> datetime:
        """When the next restart for the build may begin."""
        wait = UPDATE_RETRY_FIRST_S * 2 ** min(self.attempts - 1, 10)
        return self.at + timedelta(seconds=min(wait, UPDATE_RETRY_MAX_S))


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
    query_steamcmd: steam.Query = steam.query
    post: status.Post = status.post
    clock: Clock = field(default_factory=Clock)
    jitter: Callable[[float, float], float] = random.uniform
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    # Copies a backup off the supervisor's loop, which keeps reporting and watching the world.
    executor: Executor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=1))
    # Checks Steam's public game build off the loop, apart from the backups, so neither waits on the other.
    checker: Executor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=1))
    wall: Callable[[], datetime] = lambda: datetime.now(UTC)
    _stop: bool = field(default=False, init=False)
    _problem: str | None = field(default=None, init=False)
    _remembered: int | None = field(default=None, init=False)
    # The newest write of the world status file, and when the supervisor first saw it.
    _world_stamp: tuple[int, int] | None = field(default=None, init=False)
    _world_seen: float = field(default=0.0, init=False)
    # The generation a restart waits on, so the wait is logged once.
    _waiting: int | None = field(default=None, init=False)
    # The turn of the world's backups under way off the loop; the save the newest backup holds; when
    # the newest was taken, on the loop's clock; when the loop next tends the backups, and how long
    # it waits after a failure; the backups the folder holds, newest first, None until listed; and
    # why the runtime could not take or keep its backups.
    _tending: Future[backups.Tended] | None = field(default=None, init=False)
    _backed_up: backups.Saved | None = field(default=None, init=False)
    _backup_at: float | None = field(default=None, init=False)
    _next_backup: float = field(default=0.0, init=False)
    # Until a start's turn completes, every turn is one: it clears cut-short copies and reads the
    # newest backup's record before it backs up.
    _starting: bool = field(default=True, init=False)
    _retry_s: float = field(default=BACKUP_RETRY_S, init=False)
    _backup_names: list[str] | None = field(default=None, init=False)
    _backup_failure: str | None = field(default=None, init=False)
    # The last report the loop sent, for the last word after a stop.
    _last_report: status.RuntimeStatus | None = field(default=None, init=False)
    # A restored save world truth has not judged yet; the newest restore acted on, for status; and
    # the id of the newest restore finished, which a generation's restore never costs a restart for.
    _judging: restore.Intent | None = field(default=None, init=False)
    _restore_report: status.Restore | None = field(default=None, init=False)
    _done_id: str | None = field(default=None, init=False)
    # The backup a restore asks for, which no turn of the backups prunes until the restore has it.
    _spare: str | None = field(default=None, init=False)
    # The check of Steam's public game build under way off the loop; when the newest began, on the loop's
    # clock; the public build checks found, when other than the installed one; and why the newest check
    # failed, or why the world has not restarted for the build it found.
    _checking: Future[str] | None = field(default=None, init=False)
    _checked_at: float = field(default=0.0, init=False)
    _update_build: str | None = field(default=None, init=False)
    _update_failure: str | None = field(default=None, init=False)
    # The restart to update this start's install did not reach, which the next for its build waits on; why
    # a restart to update could not be recorded, which status keeps saying until a record is written; and
    # whether a check since asks for the write to be tried again.
    _missed: Missed | None = field(default=None, init=False)
    _unrecorded: str | None = field(default=None, init=False)
    _record_again: bool = field(default=False, init=False)
    # Set once the watch of the game ends, so a check under way ends its SteamCMD rather than hold up the exit.
    _watched: bool = field(default=False, init=False)
    # The contract a backup's record keeps, read from the image at the start, as the reader reads
    # the generation's.
    _record: Draft202012Validator = field(init=False)

    def __post_init__(self) -> None:
        self._record = restore.record_contract(self.paths.contracts)

    def request_stop(self, signum: int, frame: FrameType | None) -> None:
        """Signal handler: stop at the next poll, saving the world first."""
        self._stop = True

    def run(self) -> int:
        generation = self._wait_for_generation()
        if generation is None:
            return 0
        reporter = status.Reporter(generation.status_url, self.post)
        self._restore(generation, reporter)
        unsaved = self._unsaved(generation)
        if unsaved is not None:
            return self._hold_unsaved(generation, reporter, unsaved)
        build = self._install(generation, reporter)
        if build is None:
            return 0
        # The control plane may have handed a newer generation while the server installed.
        generation = self._read_generation() or generation
        if self._due(generation) is not None or self._drops_judged(generation):
            # A restore replaces the save, or puts one back, only before an install: the next start does it.
            _log.info("generation %d changes the restore; starting again to act on it first", generation.number)
            return 0
        reporter = status.Reporter(generation.status_url, self.post)
        self._map_export_directory()
        # Off the loop, so a slow or hung backups disk never holds up the world's launch.
        self._tend(generation)
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
        try:
            return self._watch(process, generation, reporter, save_existed, build)
        finally:
            self._watched = True

    def _unsaved(self, generation: Generation) -> str | None:
        """Why the world is held rather than launched, if it is: it has no save, its newest restore
        did not end done, and the generation asks for no restore the runtime has not done. The game
        would make an empty world in place of the one the restore was for, and the restore's
        outcome makes the control plane drop it, so this holds across starts until the server
        manager asks for another restore or an import. A record of that restore that cannot be read,
        or a data volume that cannot say whether it holds one, holds the world too, as the record may
        be of a restore that did not end done."""
        if server.save_exists(self.paths.saves, generation.world) or self._due(generation) is not None:
            return None
        place = self._place(generation.world)
        done = restore.read_done(place)
        if done is None and _may_exist(place.done):
            return _reason(
                f"the world has no save and the record of its newest restore, {place.done.name} in its data volume, "
                f"cannot be read, so no new world is made; {HELD_UNSAVED_ASK}"
            )
        if done is None or done.outcome == "done":
            return None
        ended = "failed" if done.outcome == "failed" else "was rolled back"
        # The instruction ahead of the why, so the bound on a reason cuts only the why.
        return _reason(
            f"the world has no save and restore {done.id} {ended}, so no new world is made; {HELD_UNSAVED_ASK}. "
            f"Why: {done.reason or 'unknown'}"
        )

    def _hold_unsaved(self, generation: Generation, reporter: status.Reporter, why: str) -> int:
        """Install and launch nothing, reporting the world held, until a stop or a newer generation,
        which the next start acts on."""
        _log.error("held: %s", why)
        holding: status.RuntimeStatus = {
            "contract": 1,
            "generation": generation.number,
            "world": generation.world,
            "state": State.HELD,
            "reason": why,
        }
        # A start's turn of the backups, so status names them for the next restore, then a turn each
        # BACKUP_CHECK_S, or later after a failure: with no save it takes nothing, but lists a backup
        # an import kept meanwhile.
        self._tend(generation)
        next_report = self.clock.now()
        while not self._stop:
            self._back_up(generation, running=False)
            if self._tending is None and self.clock.now() >= self._next_backup:
                self._tend(generation)
            if self.clock.now() >= next_report:
                self._report(reporter, holding.copy())
                next_report = self.clock.now() + generation.status_interval_s
            newer = self._read_generation()
            if newer is not None and newer.number > generation.number:
                _log.info("generation %d arrived while held; starting again to act on it", newer.number)
                return 0
            self.clock.sleep(POLL_S)
        _log.info("stopping: stop requested while held")
        return 0

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
        self._report(reporter, installing.copy())
        next_report = self.clock.now() + generation.status_interval_s

        def stopping() -> bool:
            nonlocal next_report
            if self.clock.now() >= next_report:
                self._report(reporter, installing.copy())
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
                    self._report(reporter, installing.copy())
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
        # The start's install has just updated the server, so the first check waits a whole interval.
        self._checked_at = launched
        self._missed = self._read_missed(build)
        state, reason = State.LOADING, None
        world: status.WorldStatus | None = None
        next_report = launched
        while True:
            if self._stop:
                return self._shut_down(process, state, "stop requested", generation, reporter)
            if state is not State.HELD:
                code = process.poll()
                if code is not None:
                    _log.error("the dedicated server exited with code %d", code)
                    if self._judging is not None:
                        self._swap_back(self._judging, generation, f"made the dedicated server exit with code {code}")
                    return code
                world = self._read_world(generation.world) or world
            fresh = world if self._seen_within(WORLD_STALE_AFTER, generation) else None
            current = world if self._seen_within(EMPTY_WITHIN, generation) else None
            generation, newer = self._follow(generation)
            due = self._due(newer) if newer is not None else None
            # Until the restart, the stop's backup and every turn before it keep the backup asked for.
            self._spare = due.backup if due is not None else None
            self._check_update(generation, build, running=state in (State.RUNNING, State.UNVERIFIED))
            why = self._restart_now(state, generation, newer, current, build)
            if why is not None:
                return self._shut_down(process, state, why, generation, reporter)
            previous = state
            if state in (State.LOADING, State.UNVERIFIED) and fresh is not None:
                state, reason = self._on_report(process, generation.world, fresh.objects, save_existed)
            if state is State.RUNNING and fresh is not None:
                self._remember_objects(generation.world, fresh.objects)
            elif state is State.LOADING and self.clock.now() - launched > LOAD_TIMEOUT_S:
                state, reason = self._on_load_timeout(process, generation.world, save_existed)
            judging = self._judging
            if judging is not None and state is not previous:
                if state is State.HELD and reason != HELD_UNREPORTED:
                    self._swap_back(judging, generation, f"loaded with no objects where it held {judging.objects}")
                    self._report(reporter, _status(generation, state, reason, build, fresh))
                    return 0
                self._judged(judging, state, generation)
            self._back_up(generation, running=state in (State.RUNNING, State.UNVERIFIED))
            if state is not previous or self.clock.now() >= next_report:
                self._report(reporter, _status(generation, state, reason, build, fresh))
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
        if state is State.HELD or newer.disconnect_players or _empty(state, world):
            return True
        if self._waiting != newer.number:
            players = "unknown" if world is None or state is State.LOADING else str(world.players)
            _log.info("generation %d waits for an empty world; players %s", newer.number, players)
            self._waiting = newer.number
        return False

    def _check_update(self, generation: Generation, build: str, *, running: bool) -> None:
        """Note a check of Steam's public game build that has finished, and while the game runs start
        the next once the update policy's interval, as it stands now, has passed since the last began.
        Checks run under either policy; only when-empty restarts for what one finds."""
        if self._checking is not None:
            if not self._checking.done():
                return
            self._checked(self._checking, generation, build)
        if running and self.clock.now() >= self._checked_at + generation.updates.every_s:
            self._checked_at = self.clock.now()
            self._checking = self.checker.submit(
                steam.public_build,
                self.paths.steamcmd / steam.STEAMCMD_SCRIPT,
                self.paths.app_info_cache,
                self.query_steamcmd,
                lambda: self._stop or self._watched,
            )

    def _checked(self, checking: Future[str], generation: Generation, build: str) -> None:
        """Take in a finished check. A check is an observation: one that fails says why in status, keeps
        the build an earlier one found, and never stops the world."""
        self._checking = None
        try:
            public = checking.result()
        except steam.Stopped:
            return
        except steam.CheckFailed as error:
            self._set_update_failure(str(error))
            return
        except Exception as error:
            self._set_update_failure(f"the check stopped on an unexpected error: {type(error).__name__}: {error}")
            return
        found = None if public == build else public
        # A record that could not be written is tried again at the next empty moment.
        self._record_again = True
        if found is not None and found != self._update_build:
            when = "once no players are online" if generation.updates.policy == "when-empty" else "at its next start"
            _log.info("game build %s is out, build %s is installed; the world updates %s", found, build, when)
        self._update_build = found
        if found is None and self._missed is not None:
            # Steam went back to the installed build: a restart for the other one need not wait.
            with contextlib.suppress(OSError):
                self.paths.update_record.unlink(missing_ok=True)
            self._missed = None
        self._set_update_failure(self._why_waiting(found, generation))

    def _why_waiting(self, found: str | None, generation: Generation) -> str | None:
        """Why the world has not restarted for the build a check found, beyond its players and policy."""
        if found is None:
            return None
        if self._unrecorded is not None:
            return self._unrecorded
        missed = self._missed
        if missed is None or missed.build != found:
            return None
        later = (
            f"tries again once empty after {missed.next_at():%Y-%m-%d %H:%M} UTC"
            if generation.updates.policy == "when-empty"
            else "takes it at its next start"
        )
        began = f"{missed.at:%Y-%m-%d %H:%M} UTC"
        return f"the restart to update at {began} did not install game build {found}; the world {later}"

    def _set_update_failure(self, why: str | None) -> None:
        why = None if why is None else _reason(why)
        if why is not None and why != self._update_failure:
            _log.warning("game update: %s", why)
        self._update_failure = why

    def _read_missed(self, build: str) -> Missed | None:
        """The restart to update this start's install did not reach, from its record; a record of the build
        installed is done with, and removed. A record that cannot be read names none, which costs at most
        one restart sooner than the wait."""
        record = self.paths.update_record
        try:
            document = json.loads(record.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(document, dict):
            return None
        wanted, attempts, at = document.get("build"), document.get("attempts"), document.get("at")
        if wanted == build:
            with contextlib.suppress(OSError):
                record.unlink(missing_ok=True)
            return None
        if not isinstance(wanted, str) or type(attempts) is not int or attempts < 1 or not isinstance(at, str):
            return None
        try:
            began = datetime.strptime(at, RECORD_TIME).replace(tzinfo=UTC)
        except ValueError:
            return None
        # No later than now, so a clock that ran ahead at the restart cannot stall the next past the wait.
        return Missed(wanted, attempts, min(began, self.wall()))

    def _restart_to_update(self, state: State, generation: Generation, world: status.WorldStatus | None) -> bool:
        """Whether to restart now for the build a check found: under the when-empty policy, by the final
        players check, for a current report of no players, and for a build an earlier restart did not
        install, once its wait has passed. The restart is recorded first, so the next start can tell
        whether it got the build; one that cannot be recorded is not made."""
        wanted = self._update_build
        if wanted is None or generation.updates.policy != "when-empty" or not _empty(state, world):
            return False
        if self._unrecorded is not None and not self._record_again:
            return False
        now = self.wall()
        attempts = 1
        missed = self._missed
        if missed is not None and missed.build == wanted:
            if now < missed.next_at():
                return False
            attempts = missed.attempts + 1
        record = self.paths.update_record
        temporary = record.with_suffix(".tmp")
        self._record_again = False
        try:
            record.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({"build": wanted, "attempts": attempts, "at": now.strftime(RECORD_TIME)}))
            temporary.replace(record)
        except OSError as error:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
            self._unrecorded = (
                "cannot record a restart to update in the data volume, so the world restarts to update only once one "
                f"can be, tried again after each check: {error.strerror or error}"
            )
            self._set_update_failure(self._unrecorded)
            return False
        self._unrecorded = None
        return True

    def _restart_now(
        self,
        state: State,
        generation: Generation,
        newer: Generation | None,
        world: status.WorldStatus | None,
        build: str,
    ) -> str | None:
        """Why the game restarts now, if it does: for a newer generation that takes a restart, by its
        gate; or for the build a check found, by _restart_to_update. A generation's confirmation is for
        that generation's change alone, never for an update."""
        if newer is not None and self._may_restart(state, newer, world):
            return self._why_restart(newer, self._due(newer))
        if self._restart_to_update(state, generation, world):
            return f"game build {self._update_build} is out; restarting to update from {build}"
        return None

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
        # Synced: a swap back relies on the count it puts back.
        with temporary.open("w") as writer:
            writer.write(json.dumps({"world": world, "objects": objects}))
            writer.flush()
            os.fsync(writer.fileno())
        temporary.replace(record)
        self._remembered = objects

    def _shut_down(
        self, process: Process, state: State, why: str, generation: Generation, reporter: status.Reporter
    ) -> int:
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
        # The last word: the stop's backup, or why there is none, which the next start forgets. The
        # plugin's counts are gone with the game.
        if self._last_report is not None:
            final = self._last_report.copy()
            final.pop("objects", None)
            final.pop("players", None)
            self._report(reporter, final)
        return 0

    def _tend(self, generation: Generation) -> Future[backups.Tended]:
        """Start a turn of the world's backups off the loop, which keeps reporting and watching."""
        place = backups.Place(
            backups.world_folder(self.paths.saves, generation.world),
            self.paths.backups,
            generation.world,
            self._record,
            self._spare,
        )
        self._tending = self.executor.submit(
            backups.tend, place, generation.backups, self.wall(), self._backed_up, starting=self._starting
        )
        return self._tending

    def _back_up(self, generation: Generation, *, running: bool) -> None:
        """Note a turn that has finished, and while the world runs start the next once due: once
        the settings' interval has passed since the newest backup, a save newer than it is backed
        up. The game is never paused."""
        if self._tending is not None:
            if not self._tending.done():
                return
            self._settle(self._tending, generation)
        # Due once the settings' interval, as they stand now, has passed since the newest backup.
        due = self._next_backup
        if self._backup_at is not None:
            due = max(due, self._backup_at + generation.backups.every_s)
        if running and self.clock.now() >= due:
            self._tend(generation)

    def _back_up_at_stop(self, generation: Generation) -> None:
        """Back up the save the game stopped with, once any turn still under way is done."""
        if self._tending is not None:
            self._settle(self._tending, generation)
        self._settle(self._tend(generation), generation)

    def _settle(self, tending: Future[backups.Tended], generation: Generation) -> None:
        """Take in a finished turn, waiting for one still under way. A turn that raised costs the
        world its backups, never the world itself."""
        self._tending = None
        now = self.clock.now()
        try:
            tended = tending.result()
        except OSError as error:
            self._backup_failed(f"cannot list the world's backups folder: {error.strerror or error}")
            self._next_backup = now + self._retry_s
            self._retry_s = min(self._retry_s * 2, generation.backups.every_s)
            return
        # A backup is never a reason to stop the world.
        except Exception as error:
            self._backup_failed(f"backups stopped on an unexpected error: {type(error).__name__}: {error}")
            self._next_backup = now + self._retry_s
            self._retry_s = min(self._retry_s * 2, generation.backups.every_s)
            return
        self._starting = False
        self._backup_names = tended.names
        if tended.backup is not None:
            backup = tended.backup
            _log.info("backed up save %d of world %s as %s", backup.saved[0], generation.world, backup.name)
            self._backup_at = now
        elif tended.names and self._backup_at is None:
            # The newest, from a run before this one: its age on the wall clock, kept within the
            # settings' interval, so a clock that has jumped can neither hurry nor stall the next.
            age = (self.wall() - backups.taken(tended.names[0])).total_seconds()
            self._backup_at = now - min(max(age, 0.0), generation.backups.every_s)
        self._backed_up = tended.newest
        if tended.failure is None:
            self._backup_failure = None
        else:
            self._backup_failed(tended.failure)
        if tended.take_failed:
            self._next_backup = now + self._retry_s
            self._retry_s = min(self._retry_s * 2, generation.backups.every_s)
            return
        self._retry_s = BACKUP_RETRY_S
        # Taken now, the interval governs the next; otherwise look again a little later.
        self._next_backup = now if tended.backup is not None else now + BACKUP_CHECK_S

    def _backup_failed(self, why: str) -> None:
        why = _reason(why)
        if why != self._backup_failure:
            _log.error("backups: %s", why)
        self._backup_failure = why

    def _backups_status(self) -> status.Backups | None:
        """The backups for the status report, None until the folder has been listed or has failed."""
        if self._backup_names is None and self._backup_failure is None:
            return None
        report: status.Backups = {"names": (self._backup_names or [])[:BACKUP_NAMES_MAX]}
        if self._backup_failure is not None:
            report["failure"] = self._backup_failure
        return report

    def _report(self, reporter: status.Reporter, report: status.RuntimeStatus) -> None:
        """Send a report, with the world's backups, its newest restore and what checks of Steam found."""
        backed_up = self._backups_status()
        if backed_up is not None:
            report["backups"] = backed_up
        if self._restore_report is not None:
            report["restore"] = self._restore_report
        update: status.Update = {}
        if self._update_build is not None:
            update["build"] = self._update_build
        if self._update_failure is not None:
            update["failure"] = self._update_failure
        if update:
            report["update"] = update
        self._last_report = report
        reporter.send(report)

    def _place(self, world: str) -> restore.Place:
        return restore.Place(self.paths.saves, self.paths.vhd, self.paths.backups, world)

    def _restore(self, generation: Generation, reporter: status.Reporter) -> None:
        """Before the game installs or launches, so its save is never replaced under it: carry forward
        or back a restore a stop or a crash cut short, then do the one the generation asks for, once
        for its id (Game runtimes, Restore). World truth judges a restored save at the launch."""
        place = self._place(generation.world)
        done = restore.read_done(place)
        intent = restore.read_intent(place)
        if done is not None:
            self._done_id = done.id
            self._restore_status(done.id, done.outcome, done.reason)
        if intent is not None:
            self._recover(place, intent, generation)
        asked = generation.restore
        if asked is None or asked.id == self._done_id or self._judging is not None:
            return
        try:
            self._begin(place, generation, asked, reporter)
        except restore.RestoreFailed as error:
            _log.error("restore %s of backup %s refused: %s", asked.id, asked.backup, error)
            self._finish(place, restore.Done(asked.id, "failed", _reason(str(error))))
        finally:
            self._spare = None

    def _recover(self, place: restore.Place, intent: restore.Intent, generation: Generation) -> None:
        """Carry a restore that a stop or a crash cut short to where it is safe: a copy is dropped and
        asked for again, a swap is finished, a swap back is finished, and a judged one is forgotten.
        A swapped save world truth has not judged is judged at this start, unless the generation no
        longer asks for that restore: then it is swapped back."""
        _log.info("carrying on restore %s of backup %s from %s", intent.id, intent.backup, intent.phase)
        if intent.phase == "staging":
            restore.clear_staged(place)
            place.intent.unlink(missing_ok=True)
            return
        if intent.phase == "unswapping":
            self._roll_back(place, intent, intent.reason or "the restored save failed world truth")
            return
        if intent.phase == "judged":
            restore.drop_aside(place)
            self._finish(place, restore.Done(intent.id, "done"))
            return
        if intent.phase == "swapping":
            restore.swap(place, intent.had_save)
            intent = replace(intent, phase="swapped")
            restore.write_intent(place, intent)
        if generation.restore is not None and generation.restore.id == intent.id:
            # A crash may have come before the count was written: judge against the backup's.
            if intent.objects is not None:
                self._remember_objects(generation.world, intent.objects)
            self._judging = intent
            self._restore_status(intent.id, "restoring", intent.reason)
        else:
            dropped = "a newer generation dropped or replaced the restore before world truth judged it"
            self._roll_back(place, intent, dropped)

    def _begin(self, place: restore.Place, generation: Generation, asked: Restore, reporter: status.Reporter) -> None:
        """Back up the save the restore replaces, as this start's turn of the backups, then copy and
        check the backup, and swap it in. Raises RestoreFailed while the world's save is as it was."""
        self._restore_status(asked.id, "restoring", None)
        holding: status.RuntimeStatus = {
            "contract": 1,
            "generation": generation.number,
            "world": generation.world,
            "state": State.HELD,
            "reason": f"restoring backup {asked.backup}",
        }
        _log.info("restoring backup %s of world %s", asked.backup, generation.world)
        if place.legacy_save.exists():
            raise restore.RestoreFailed(
                f"the world has a pre-1.0 save, {place.legacy_save.name}, which no backup replaces"
            )
        if place.aside.exists():
            where = place.aside.relative_to(self.paths.data)
            raise restore.RestoreFailed(
                f"a save an earlier restore set aside is still in the world's data volume, at {where}"
            )
        every = generation.status_interval_s
        self._spare = asked.backup
        turn = self._await(self._tend(generation), reporter, holding, every)
        kept = None
        try:
            tended = turn.result()
        except Exception as error:
            kept = f"{type(error).__name__}: {error}"
        else:
            if tended.take_failed:
                kept = tended.failure or "the backup failed"
        self._settle(turn, generation)
        if kept is not None:
            kept = _reason(
                f"the save it replaces could not be backed up ({kept}); it is kept aside until world truth passes"
            )
            _log.warning("restore: %s", kept)
        if self._stop:
            _log.info("stopping before the restore touches the save; the next start does it")
            return
        intent = restore.Intent(
            id=asked.id,
            backup=asked.backup,
            phase="staging",
            before=self._last_objects(generation.world),
            had_save=place.save.exists(),
            reason=kept,
        )
        restore.write_intent(place, intent)
        # A refusal leaves nothing staged, and its done record replaces this intent.
        staged = self.executor.submit(restore.stage, place, asked.backup, self._record)
        objects = self._await(staged, reporter, holding, every).result()
        intent = replace(intent, phase="swapping", objects=objects)
        restore.write_intent(place, intent)
        restore.swap(place, intent.had_save)
        intent = replace(intent, phase="swapped")
        restore.write_intent(place, intent)
        # The launch's turn backs the restored save up again, so the newest backup holds the save the
        # world runs on whatever pruning removes after; the backup restored from needs no spare now.
        self._spare = None
        # World truth judges the restored save against what its backup holds.
        self._remember_objects(generation.world, objects)
        self._judging = intent
        self._restore_status(intent.id, "restoring", kept)
        _log.info("restored backup %s of %s, %d objects, for world truth", asked.backup, generation.world, objects)

    def _await[T](
        self, future: Future[T], reporter: status.Reporter, holding: status.RuntimeStatus, every: int
    ) -> Future[T]:
        """Wait for work off the loop, reporting the world held meanwhile, so it never shows as unknown."""
        next_report = self.clock.now()
        while True:
            if self.clock.now() >= next_report:
                self._report(reporter, holding.copy())
                next_report = self.clock.now() + every
            if future.done():
                return future
            self.clock.sleep(POLL_S)

    def _judged(self, intent: restore.Intent, state: State, generation: Generation) -> None:
        """World truth on a restored save: a world that loaded it runs, and the save it replaced goes.
        A load the plugin never reported is no verdict on the save, so the restore waits on the next."""
        if state is State.HELD:
            waits = "world truth has not judged the restored save: " + HELD_UNREPORTED
            self._restore_status(intent.id, "restoring", waits)
            return
        if state not in (State.RUNNING, State.UNVERIFIED):
            return
        place = self._place(generation.world)
        restore.write_intent(place, replace(intent, phase="judged"))
        restore.drop_aside(place)
        self._finish(place, restore.Done(intent.id, "done"))
        self._judging = None
        _log.info("the restored backup %s passed world truth", intent.backup)

    def _swap_back(self, intent: restore.Intent, generation: Generation, failed: str) -> None:
        """A restored save that failed world truth, by loading empty or by ending the game while it
        loaded, which saved nothing: put back the save it replaced, and the next start launches that."""
        # A turn of the backups may still be reading the save folder.
        if self._tending is not None:
            self._settle(self._tending, generation)
        back = "the save it replaced is back" if intent.had_save else "the world has no save again"
        why = f"backup {intent.backup}'s save {failed}; {back}"
        self._roll_back(self._place(generation.world), intent, why)

    def _roll_back(self, place: restore.Place, intent: restore.Intent, why: str) -> None:
        _log.error("restore %s rolled back: %s", intent.id, why)
        restore.write_intent(place, replace(intent, phase="unswapping", reason=_reason(why)))
        restore.unswap(place, intent.had_save)
        if intent.before is None:
            self.paths.last_objects.unlink(missing_ok=True)
            self._remembered = None
        else:
            self._remember_objects(place.world, intent.before)
        self._finish(place, restore.Done(intent.id, "rolled-back", _reason(why)))
        self._judging = None

    def _finish(self, place: restore.Place, done: restore.Done) -> None:
        restore.finish(place, done)
        self._done_id = done.id
        self._restore_status(done.id, done.outcome, done.reason)

    def _restore_status(self, id: str, outcome: status.RestoreOutcome, reason: str | None) -> None:
        report: status.Restore = {"id": id, "outcome": outcome}
        if reason is not None:
            report["reason"] = _reason(reason)
        self._restore_report = report

    def _why_restart(self, newer: Generation, due: Restore | None) -> str:
        if due is not None:
            return f"a newer generation restores backup {due.backup}"
        if self._drops_judged(newer):
            return "a newer generation drops the restore world truth has not judged"
        return "a newer generation changes this world's settings"

    def _drops_judged(self, generation: Generation) -> bool:
        """Whether the generation no longer asks for the restore world truth has not judged yet,
        which the next start swaps back: a restart, gated like any other."""
        return self._judging is not None and generation.restore is None

    def _due(self, generation: Generation) -> Restore | None:
        """The restore the generation asks for, if it is neither done nor being judged: one that
        costs a restart."""
        asked = generation.restore
        if asked is None or asked.id == self._done_id:
            return None
        if self._judging is not None and self._judging.id == asked.id:
            return None
        return asked

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
        if newer.same_settings(current) and self._due(newer) is None and not self._drops_judged(newer):
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


def _empty(state: State, world: status.WorldStatus | None) -> bool:
    """Whether the game runs a world a current report says no player is on."""
    return state in (State.RUNNING, State.UNVERIFIED) and world is not None and world.players == 0


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


def _may_exist(path: Path) -> bool:
    """Whether a file is there, or may be, as one is on a volume that cannot be read."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return True


def _reason(text: str) -> str:
    """One line of printable text within the contract's bound: an error's text is not ours."""
    return "".join(c if c.isprintable() else " " for c in text)[:REASON_MAX]
