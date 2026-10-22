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
from vhd_runtime.generation import Generation, InvalidGeneration, Reader

LOAD_TIMEOUT_S = 900
STOP_TIMEOUT_S = 120
POLL_S = 1.0
GENERATION_POLL_S = 2.0
HELD_AFTER_OBJECTS = "the world loaded with no objects where it last held {last}; stopped before its first autosave"
HELD_OVER_SAVE = "the world loaded with no objects although a save existed; stopped before its first autosave"
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
    _invalid: str | None = field(default=None, init=False)
    _remembered: int | None = field(default=None, init=False)

    def request_stop(self, signum: int, frame: FrameType | None) -> None:
        """Signal handler: stop at the next poll, saving the world first."""
        self._stop = True

    def run(self) -> int:
        generation = self._wait_for_generation()
        if generation is None:
            return 0
        reporter = status.Reporter(generation.status_url, self.post)
        reporter.send(
            {"contract": 1, "generation": generation.number, "world": generation.world, "state": State.INSTALLING}
        )
        steamcmd = steam.ensure_steamcmd(self.paths.steamcmd, self.fetch)
        build = steam.ensure_server(steamcmd, self.paths.server, self.run_steamcmd)
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
            if self._newer_generation(generation.number):
                return self._shut_down(process, state, "a newer generation arrived")
            if state is not State.HELD:
                code = process.poll()
                if code is not None:
                    _log.error("the dedicated server exited with code %d", code)
                    return code
                world = status.read_world(self.paths.world_status, generation.world) or world
            previous = state
            if state in (State.LOADING, State.UNVERIFIED) and world is not None:
                held = self._loaded_empty(generation.world, world.objects, save_existed)
                if held is not None:
                    # Killed, not interrupted: SIGINT would save the empty world over the real one.
                    process.kill()
                    process.wait()
                    state, reason = State.HELD, held
                    _log.error("held: %s", reason)
                else:
                    state, reason = State.RUNNING, None
                    _log.info("world %s loaded with %d objects", generation.world, world.objects)
            if state is State.RUNNING and world is not None:
                self._remember_objects(generation.world, world.objects)
            elif state is State.LOADING and self.clock.now() - launched > LOAD_TIMEOUT_S:
                state, reason = State.UNVERIFIED, UNVERIFIED_REASON
                _log.warning("unverified: %s", reason)
            if state is not previous or self.clock.now() >= next_report:
                reporter.send(_status(generation, state, reason, build, world))
                next_report = self.clock.now() + generation.status_interval_s
            self.clock.sleep(POLL_S)

    def _loaded_empty(self, world: str, objects: int, save_existed: bool) -> str | None:
        """Why the world must be held, or None. An empty load fails world truth when the
        world held objects when last seen, or, with nothing remembered, when a save existed.
        A world last seen empty, such as one stopped moments after its creation, may load empty."""
        if objects > 0:
            return None
        last = self._last_objects(world)
        if last is None:
            return HELD_OVER_SAVE if save_existed else None
        return HELD_AFTER_OBJECTS.format(last=last) if last > 0 else None

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

    def _newer_generation(self, current: int) -> bool:
        generation = self._read_generation()
        return generation is not None and generation.number > current

    def _read_generation(self) -> Generation | None:
        """The valid generation on disk, or None. An invalid one is refused and logged once."""
        try:
            generation = self.reader.read()
        except InvalidGeneration as error:
            if str(error) != self._invalid:
                _log.error("refusing the generation: %s", error)
            self._invalid = str(error)
            return None
        self._invalid = None
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
