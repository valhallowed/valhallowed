"""The runtime's state machine: generation, install, launch, world truth, stop and hold."""

import json
import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Protocol

from vhd_runtime import server, status, steam
from vhd_runtime.generation import Generation, IncompleteGeneration, InvalidGeneration, Reader

# Well below the game's autosave interval (30 minutes unless changed), so a
# world held at this point has not saved.
LOAD_TIMEOUT_S = 900
STOP_TIMEOUT_S = 120
POLL_S = 1.0
GENERATION_POLL_S = 2.0
# A world status file the plugin has not rewritten for this many of its intervals says nothing:
# a plugin that throws after a game patch leaves its last count behind.
WORLD_STALE_AFTER = 3
HELD_AFTER_OBJECTS = "the world loaded with no objects where it last held {last}; stopped before its first autosave"
HELD_OVER_SAVE = "the world loaded with no objects although a save existed; stopped before its first autosave"
HELD_UNREPORTED = (
    "the plugin has not reported the loaded world, which held objects before; stopped before its first autosave"
)
UNVERIFIED_REASON = "the plugin has not reported the loaded world"

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
    def last_objects(self) -> Path:
        """The object count the runtime last saw its world hold, kept beside the world's save."""
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
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    _stop: bool = field(default=False, init=False)
    _problem: str | None = field(default=None, init=False)
    _remembered: int | None = field(default=None, init=False)
    # The newest write of the world status file, and when the supervisor first saw it.
    _world_stamp: tuple[int, int] | None = field(default=None, init=False)
    _world_seen: float = field(default=0.0, init=False)
    # The generation a restart waits on, so the wait is logged once.
    _waiting: int | None = field(default=None, init=False)

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
        server.install_mods(
            self.paths.pack, self.paths.plugin, self.paths.server, self.paths.world_status, generation.status_interval_s
        )
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

    def _install(self, generation: Generation, reporter: status.Reporter) -> str | None:
        """SteamCMD and the dedicated server, installed or updated: the server's build id, or
        None when a stop arrived first, so nothing launches. Status keeps reporting the
        install, which can take many minutes, so the control plane never takes it for silence."""
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

        try:
            steamcmd = steam.ensure_steamcmd(self.paths.steamcmd, self.fetch)
            if not self._stop:
                return steam.ensure_server(steamcmd, self.paths.server, self.run_steamcmd, stopping)
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
                return self._shut_down(process, state, "stop requested")
            if state is not State.HELD:
                code = process.poll()
                if code is not None:
                    _log.error("the dedicated server exited with code %d", code)
                    return code
                world = self._read_world(generation.world) or world
            fresh = world if self._fresh(world, generation) else None
            generation, newer = self._follow(generation)
            if newer is not None and self._may_restart(state, newer, fresh):
                return self._shut_down(process, state, "a newer generation changes this world's settings")
            previous = state
            if state in (State.LOADING, State.UNVERIFIED) and fresh is not None:
                state, reason = self._on_report(process, generation.world, fresh.objects, save_existed)
            if state is State.RUNNING and fresh is not None:
                self._remember_objects(generation.world, fresh.objects)
            elif state is State.LOADING and self.clock.now() - launched > LOAD_TIMEOUT_S:
                state, reason = self._on_load_timeout(process, generation.world, save_existed)
            if state is not previous or self.clock.now() >= next_report:
                reporter.send(_status(generation, state, reason, build, fresh))
                next_report = self.clock.now() + generation.status_interval_s
            self.clock.sleep(POLL_S)

    def _read_world(self, world: str) -> status.WorldStatus | None:
        """The plugin's report, noting when a new write of it first appeared."""
        read = status.read_world(self.paths.world_status, world)
        if read is not None and read.stamp != self._world_stamp:
            self._world_stamp, self._world_seen = read.stamp, self.clock.now()
        return read

    def _fresh(self, world: status.WorldStatus | None, generation: Generation) -> bool:
        if world is None or self._world_stamp is None:
            return False
        return self.clock.now() - self._world_seen <= WORLD_STALE_AFTER * generation.status_interval_s

    def _may_restart(self, state: State, newer: Generation, world: status.WorldStatus | None) -> bool:
        """The final players check, made here because only the runtime sees the count at the
        moment it would stop the game. A held world has no game; a confirmed generation goes
        now; anything else waits for a fresh report of no players. A load is never cut short."""
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
        if self._expects_objects(world, save_existed):
            return self._hold(process, HELD_UNREPORTED)
        _log.warning("unverified: %s", UNVERIFIED_REASON)
        return State.UNVERIFIED, UNVERIFIED_REASON

    def _hold(self, process: Process, reason: str) -> tuple[State, str]:
        # Killed, not interrupted: SIGINT would save whatever loaded over the real world.
        process.kill()
        process.wait()
        _log.error("held: %s", reason)
        return State.HELD, reason

    def _expects_objects(self, world: str, save_existed: bool) -> bool:
        """Whether the world held objects when last seen, or, with nothing remembered, had a save."""
        last = self._last_objects(world)
        return save_existed if last is None else last > 0

    def _loaded_empty(self, world: str, objects: int, save_existed: bool) -> str | None:
        """Why the world must be held, or None. An empty load fails world truth when the
        world held objects when last seen, or, with nothing remembered, when a save existed.
        A world last seen empty, such as one stopped moments after its creation, may load empty."""
        if objects > 0 or not self._expects_objects(world, save_existed):
            return None
        last = self._last_objects(world)
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

    def _shut_down(self, process: Process, state: State, why: str) -> int:
        _log.info("stopping: %s", why)
        if state is State.HELD or process.poll() is not None:
            return 0
        if state is State.LOADING:
            # World truth has not passed, and SIGINT would save whatever has loaded.
            process.kill()
            process.wait()
            return 0
        # The dedicated server saves the world on SIGINT.
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _log.error("the dedicated server did not stop within %d s; killing it", STOP_TIMEOUT_S)
            process.kill()
            process.wait()
        return 0

    def _wait_for_generation(self) -> Generation | None:
        while not self._stop:
            generation = self._read_generation()
            if generation is not None:
                return generation
            self.clock.sleep(GENERATION_POLL_S)
        return None

    def _follow(self, current: Generation) -> tuple[Generation, Generation | None]:
        """The generation to run on, and a newer one that takes a restart, if any. A newer
        generation with the same settings is adopted in place."""
        newer = self._read_generation()
        if newer is None or newer.number <= current.number:
            return current, None
        if newer.same_settings(current):
            _log.info("generation %d has the settings of %d; running on", newer.number, current.number)
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
