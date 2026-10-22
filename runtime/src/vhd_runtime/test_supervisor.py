import json
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from vhd_runtime import supervisor
from vhd_runtime.generation import Reader
from vhd_runtime.supervisor import Clock, Paths, Supervisor

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
STATUS_URL = "http://control-plane:9457/status"


class FakeProcess:
    def __init__(self) -> None:
        self.code: int | None = None
        self.signals: list[int] = []
        self.killed = False
        self.wait_times_out = False

    def poll(self) -> int | None:
        return self.code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def kill(self) -> None:
        self.killed = True
        self.code = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.wait_times_out and not self.killed:
            raise subprocess.TimeoutExpired("valheim_server.x86_64", timeout or 0)
        self.code = 0 if self.code is None else self.code
        return self.code


class FakeTime:
    """A clock that only moves when the supervisor sleeps, running scheduled events as it passes them."""

    def __init__(self) -> None:
        self.now = 0.0
        self.events: list[tuple[float, Callable[[], None]]] = []

    def at(self, when: float, event: Callable[[], None]) -> None:
        self.events.append((when, event))

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        due = [e for e in self.events if e[0] <= self.now]
        self.events = [e for e in self.events if e[0] > self.now]
        for _, event in due:
            event()
        if self.now > 100_000:
            raise AssertionError("the supervisor never finished")


class World:
    """One test world: paths, an installed server, a generation, and fakes for launch and status posts."""

    def __init__(self, root: Path) -> None:
        self.paths = Paths(
            generation=root / "generation",
            data=root / "data",
            pack=root / "pack",
            plugin=root / "plugin" / "Valhallowed.Plugin.dll",
            contracts=CONTRACTS,
            world_status=root / "run" / "world.json",
        )
        for directory in (
            self.paths.generation,
            self.paths.steamcmd,
            self.paths.plugin.parent,
            self.paths.world_status.parent,
        ):
            directory.mkdir(parents=True)
        (self.paths.steamcmd / "steamcmd.sh").write_text("#!/bin/sh\n")
        (self.paths.server / "steamapps").mkdir(parents=True)
        (self.paths.server / "steamapps" / "appmanifest_896660.acf").write_text('"buildid"\t\t"25909431"\n')
        (self.paths.pack / "BepInEx" / "core").mkdir(parents=True)
        (self.paths.pack / "doorstop_libs").mkdir()
        (self.paths.pack / "doorstop_libs" / "libdoorstop_x64.so").write_bytes(b"\x7fELF")
        self.paths.plugin.write_bytes(b"MZ")
        self.time = FakeTime()
        self.process = FakeProcess()
        self.launches: list[list[str]] = []
        self.reports: list[dict[str, object]] = []

    def generation(self, number: int = 1) -> None:
        document = {
            "contract": 1,
            "generation": number,
            "world": {"name": "Meadowfold", "port": 2456},
            "plugin": {"status_interval_s": 5},
            "status_url": STATUS_URL,
        }
        (self.paths.generation / "generation.json").write_text(json.dumps(document))

    def save(self) -> None:
        (self.paths.saves / "worlds_local" / "Meadowfold").mkdir(parents=True)

    def remember(self, name: str, objects: int) -> None:
        self.paths.last_objects.parent.mkdir(parents=True, exist_ok=True)
        self.paths.last_objects.write_text(json.dumps({"world": name, "objects": objects}))

    def plugin_reports(self, objects: int) -> None:
        self.paths.world_status.write_text(json.dumps({"world": "Meadowfold", "objects": objects, "players": 0}))

    def supervisor(self) -> Supervisor:
        def launch(command: list[str], env: dict[str, str], cwd: Path) -> FakeProcess:
            self.launches.append(command)
            return self.process

        def post(url: str, body: bytes, timeout: float) -> None:
            self.reports.append(json.loads(body))

        return Supervisor(
            paths=self.paths,
            reader=Reader(self.paths.generation, CONTRACTS),
            launch=launch,
            post=post,
            clock=Clock(now=lambda: self.time.now, sleep=self.time.sleep),
            environ={},
        )

    def states(self) -> list[object]:
        """The reported states, a repeated report of the same state counted once."""
        states: list[object] = []
        for report in self.reports:
            if not states or states[-1] != report["state"]:
                states.append(report["state"])
        return states


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_a_loaded_world_runs_and_saves_on_stop(world: World) -> None:
    world.generation()
    world.save()
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(48113))
    world.time.at(60, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert world.states() == ["installing", "loading", "running"]
    assert world.reports[-1]["objects"] == 48113
    assert world.process.signals == [signal.SIGINT]
    assert not world.process.killed


def test_an_empty_world_over_a_save_is_held_without_saving(world: World) -> None:
    world.generation()
    world.save()
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.time.at(600, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert world.process.killed
    assert world.process.signals == []
    held = [r for r in world.reports if r["state"] == "held"]
    assert held and held[0]["reason"] == supervisor.HELD_OVER_SAVE


def test_an_empty_load_of_a_world_that_held_objects_is_held(world: World) -> None:
    world.generation()
    world.save()
    world.remember("Meadowfold", 120)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.time.at(600, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert world.process.killed
    held = [r for r in world.reports if r["state"] == "held"]
    assert held and held[0]["reason"] == supervisor.HELD_AFTER_OBJECTS.format(last=120)


def test_a_world_last_seen_empty_may_load_empty(world: World) -> None:
    world.generation()
    world.save()
    world.remember("Meadowfold", 0)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.time.at(60, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert not world.process.killed
    assert world.states() == ["installing", "loading", "running"]


def test_a_count_remembered_for_another_world_does_not_count(world: World) -> None:
    world.generation()
    world.save()
    world.remember("Ashwake", 0)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.time.at(600, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert world.process.killed


def test_remembers_the_count_while_running(world: World) -> None:
    world.generation()
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(29))
    world.time.at(40, lambda: world.plugin_reports(54))
    world.time.at(60, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert json.loads(world.paths.last_objects.read_text()) == {"world": "Meadowfold", "objects": 54}


def test_a_new_world_may_start_empty(world: World) -> None:
    world.generation()
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.time.at(60, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert "running" in world.states()
    assert not world.process.killed


def test_a_report_from_an_earlier_run_is_ignored(world: World) -> None:
    world.generation()
    world.save()
    world.plugin_reports(0)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(48113))
    world.time.at(60, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert not world.process.killed
    assert "held" not in world.states()


def test_no_report_for_a_new_world_leaves_it_unverified_but_running(world: World) -> None:
    world.generation()
    sup = world.supervisor()
    world.time.at(supervisor.LOAD_TIMEOUT_S + 10, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert "unverified" in world.states()
    assert world.process.signals == [signal.SIGINT]


def test_no_report_over_a_save_is_held_without_saving(world: World) -> None:
    world.generation()
    world.save()
    sup = world.supervisor()
    world.time.at(supervisor.LOAD_TIMEOUT_S + 60, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert world.process.killed
    assert world.process.signals == []
    held = [r for r in world.reports if r["state"] == "held"]
    assert held and held[0]["reason"] == supervisor.HELD_UNREPORTED


def test_no_report_for_a_world_that_held_objects_is_held(world: World) -> None:
    world.generation()
    world.remember("Meadowfold", 120)
    sup = world.supervisor()
    world.time.at(supervisor.LOAD_TIMEOUT_S + 60, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert world.process.killed
    assert world.process.signals == []


def test_no_report_for_a_world_last_seen_empty_leaves_it_unverified(world: World) -> None:
    world.generation()
    world.save()
    world.remember("Meadowfold", 0)
    sup = world.supervisor()
    world.time.at(supervisor.LOAD_TIMEOUT_S + 10, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert "unverified" in world.states()
    assert not world.process.killed


def test_a_newer_generation_stops_the_server_with_a_save(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(10))
    world.time.at(60, lambda: world.generation(2))

    assert sup.run() == 0
    assert world.process.signals == [signal.SIGINT]


def test_the_servers_own_exit_ends_the_runtime(world: World) -> None:
    world.generation()
    sup = world.supervisor()

    def crash() -> None:
        world.process.code = 134

    world.time.at(30, crash)
    assert sup.run() == 134


def test_waits_for_a_generation(world: World) -> None:
    sup = world.supervisor()
    world.time.at(20, world.generation)
    world.time.at(60, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert len(world.launches) == 1


def test_an_invalid_generation_is_refused_and_nothing_launches(world: World) -> None:
    (world.paths.generation / "generation.json").write_text(json.dumps({"contract": 2}))
    sup = world.supervisor()
    world.time.at(60, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert world.launches == []


def test_a_server_that_will_not_stop_is_killed(world: World) -> None:
    world.generation()
    world.process.wait_times_out = True
    sup = world.supervisor()
    world.time.at(30, lambda: sup.request_stop(signal.SIGTERM, None))

    assert sup.run() == 0
    assert world.process.signals == [signal.SIGINT]
    assert world.process.killed
