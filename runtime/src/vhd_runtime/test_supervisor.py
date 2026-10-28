import json
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from vhd_runtime import steam, supervisor
from vhd_runtime.generation import Reader, validator
from vhd_runtime.supervisor import Clock, Paths, Supervisor

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
STATUS_URL = "http://control-plane:9457/status"
STATUS = validator(CONTRACTS / "game-runtime.status.schema.json")


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
        self.install_server()
        (self.paths.pack / "BepInEx" / "core").mkdir(parents=True)
        (self.paths.pack / "doorstop_libs").mkdir()
        (self.paths.pack / "doorstop_libs" / "libdoorstop_x64.so").write_bytes(b"\x7fELF")
        self.paths.plugin.write_bytes(b"MZ")
        self.time = FakeTime()
        self.process = FakeProcess()
        self.launches: list[list[str]] = []
        self.reports: list[dict[str, object]] = []
        self.steamcmd_runs: list[list[str]] = []

    def generation(
        self, number: int = 1, password: str | None = None, secrets: bool = True, disconnect: bool = False
    ) -> None:
        """Write a generation, and unless told not to, its secrets, as the control plane does."""
        if secrets:
            self.secrets(number, password)
        document: dict[str, object] = {
            "contract": 2,
            "generation": number,
            "world": {"name": "Meadowfold", "port": 2456},
            "plugin": {"status_interval_s": 5},
            "status_url": STATUS_URL,
        }
        if disconnect:
            document["disconnect_players"] = True
        self.replace("generation.json", document)

    def secrets(self, number: int, password: str | None = None) -> None:
        document: dict[str, object] = {"contract": 1, "generation": number}
        if password is not None:
            document["password"] = password
        self.replace("secrets.json", document)

    def replace(self, name: str, document: object) -> None:
        """Replace a file whole, as the control plane does."""
        temporary = self.paths.generation / f".{name}.tmp"
        temporary.write_text(json.dumps(document))
        temporary.replace(self.paths.generation / name)

    def save(self) -> None:
        (self.paths.saves / "worlds_local" / "Meadowfold").mkdir(parents=True)

    def remember(self, name: str, objects: int) -> None:
        self.paths.last_objects.parent.mkdir(parents=True, exist_ok=True)
        self.paths.last_objects.write_text(json.dumps({"world": name, "objects": objects}))

    def plugin_reports(self, objects: int, players: int = 0) -> None:
        """One write of the world status file, replaced whole as the plugin does."""
        temporary = self.paths.world_status.with_suffix(".tmp")
        temporary.write_text(json.dumps({"world": "Meadowfold", "objects": objects, "players": players}))
        temporary.replace(self.paths.world_status)

    def plugin_running(self, objects: int, players: Callable[[], int] = lambda: 0, start: float = 30) -> None:
        """The plugin rewriting the file every interval from start, with the players of the moment."""
        for when in range(int(start), int(start) + 3000, 5):
            self.time.at(when, lambda: self.plugin_reports(objects, players()))

    def supervisor(self, run_steamcmd: steam.Run | None = None) -> Supervisor:
        def launch(command: list[str], env: dict[str, str], cwd: Path) -> FakeProcess:
            self.launches.append(command)
            return self.process

        def post(url: str, body: bytes, timeout: float) -> None:
            report = json.loads(body)
            # The control plane drops a report that breaks the contract, so none may.
            STATUS.validate(report)
            self.reports.append(report)

        return Supervisor(
            paths=self.paths,
            reader=Reader(self.paths.generation, CONTRACTS),
            launch=launch,
            run_steamcmd=run_steamcmd or self.run_steamcmd,
            post=post,
            clock=Clock(now=lambda: self.time.now, sleep=self.time.sleep),
            environ={},
        )

    def run_steamcmd(self, command: list[str], timeout: float, stopping: steam.Stopping) -> None:
        self.steamcmd_runs.append(command)

    def install_server(self) -> None:
        """An installed dedicated server, as SteamCMD leaves its app manifest."""
        (self.paths.server / "steamapps" / "appmanifest_896660.acf").write_text(
            '"StateFlags"\t\t"4"\n"buildid"\t\t"25909431"\n'
        )

    def uninstall_server(self) -> None:
        (self.paths.server / "steamapps" / "appmanifest_896660.acf").unlink()

    def stop_at(self, when: float, sup: Supervisor) -> None:
        """Send the supervisor SIGTERM's stop request at a time, as Docker does on a stop."""
        self.time.at(when, lambda: sup.request_stop(signal.SIGTERM, None))

    def held_reasons(self) -> list[object]:
        return [r["reason"] for r in self.reports if r["state"] == "held"]

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
    world.plugin_running(48113)
    world.stop_at(60, sup)

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
    world.stop_at(600, sup)

    assert sup.run() == 0
    assert world.process.killed
    assert world.process.signals == []
    assert world.held_reasons()[:1] == [supervisor.HELD_OVER_SAVE]


def test_an_empty_load_of_a_world_that_held_objects_is_held(world: World) -> None:
    world.generation()
    world.save()
    world.remember("Meadowfold", 120)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.stop_at(600, sup)

    assert sup.run() == 0
    assert world.process.killed
    assert world.held_reasons()[:1] == [supervisor.HELD_AFTER_OBJECTS.format(last=120)]


def test_a_world_last_seen_empty_may_load_empty(world: World) -> None:
    world.generation()
    world.save()
    world.remember("Meadowfold", 0)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert not world.process.killed
    assert world.states() == ["installing", "loading", "running"]


def test_a_count_remembered_for_another_world_does_not_count(world: World) -> None:
    world.generation()
    world.save()
    world.remember("Ashwake", 0)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.stop_at(600, sup)

    assert sup.run() == 0
    assert world.process.killed


def test_remembers_the_count_while_running(world: World) -> None:
    world.generation()
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(29))
    world.time.at(40, lambda: world.plugin_reports(54))
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert json.loads(world.paths.last_objects.read_text()) == {"world": "Meadowfold", "objects": 54}


def test_a_new_world_may_start_empty(world: World) -> None:
    world.generation()
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert "running" in world.states()
    assert not world.process.killed


def test_a_report_from_an_earlier_run_is_ignored(world: World) -> None:
    world.generation()
    world.save()
    world.plugin_reports(0)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(48113))
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert not world.process.killed
    assert "held" not in world.states()


def test_no_report_for_a_new_world_leaves_it_unverified_but_running(world: World) -> None:
    world.generation()
    sup = world.supervisor()
    world.stop_at(supervisor.LOAD_TIMEOUT_S + 10, sup)

    assert sup.run() == 0
    assert "unverified" in world.states()
    assert world.process.signals == [signal.SIGINT]


def test_no_report_over_a_save_is_held_without_saving(world: World) -> None:
    world.generation()
    world.save()
    sup = world.supervisor()
    world.stop_at(supervisor.LOAD_TIMEOUT_S + 60, sup)

    assert sup.run() == 0
    assert world.process.killed
    assert world.process.signals == []
    assert world.held_reasons()[:1] == [supervisor.HELD_UNREPORTED]


def test_no_report_for_a_world_that_held_objects_is_held(world: World) -> None:
    world.generation()
    world.remember("Meadowfold", 120)
    sup = world.supervisor()
    world.stop_at(supervisor.LOAD_TIMEOUT_S + 60, sup)

    assert sup.run() == 0
    assert world.process.killed
    assert world.process.signals == []


def test_no_report_for_a_world_last_seen_empty_leaves_it_unverified(world: World) -> None:
    world.generation()
    world.save()
    world.remember("Meadowfold", 0)
    sup = world.supervisor()
    world.stop_at(supervisor.LOAD_TIMEOUT_S + 10, sup)

    assert sup.run() == 0
    assert "unverified" in world.states()
    assert not world.process.killed


def test_a_newer_generation_that_changes_settings_stops_the_server_with_a_save(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(10)
    world.time.at(60, lambda: world.generation(2, password="hearth-and-horn"))

    assert sup.run() == 0
    assert world.process.signals == [signal.SIGINT]


def test_a_restart_waits_while_players_are_online(world: World) -> None:
    world.generation(1)
    online = [2]
    sup = world.supervisor()
    world.plugin_running(10, players=lambda: online[0])
    world.time.at(60, lambda: world.generation(2, password="hearth-and-horn"))
    world.time.at(300, lambda: online.__setitem__(0, 0))

    assert sup.run() == 0
    assert world.process.signals == [signal.SIGINT]
    assert world.time.now >= 300


def test_a_confirmed_restart_goes_with_players_online(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(10, players=lambda: 2)
    world.time.at(60, lambda: world.generation(2, password="hearth-and-horn", disconnect=True))

    assert sup.run() == 0
    assert world.process.signals == [signal.SIGINT]
    assert world.time.now < 70


def test_a_plugin_that_stopped_writing_vouches_for_no_one(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(10, players=0))
    world.time.at(60, lambda: world.generation(2, password="hearth-and-horn"))
    world.stop_at(600, sup)

    assert sup.run() == 0
    assert world.time.now >= 600
    assert "players" not in world.reports[-1]
    assert world.reports[-1]["state"] == "running"


def test_a_report_older_than_the_check_allows_holds_a_restart(world: World) -> None:
    """Fresh enough to report, too old to vouch that no one joined since."""
    world.generation(1)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(10, players=0))
    world.time.at(40, lambda: world.generation(2, password="hearth-and-horn"))
    world.stop_at(44, sup)

    assert sup.run() == 0
    assert world.process.signals == [signal.SIGINT]
    assert world.time.now >= 44
    assert world.reports[-1]["generation"] == 1


def test_a_newer_generation_never_cuts_a_load_short_unconfirmed(world: World) -> None:
    world.generation(1)
    world.save()
    sup = world.supervisor()
    world.time.at(10, lambda: world.generation(2, password="hearth-and-horn"))
    world.plugin_running(48113, start=60)

    assert sup.run() == 0
    assert world.states() == ["installing", "loading", "running"]
    assert world.process.signals == [signal.SIGINT]
    assert not world.process.killed


def test_a_confirmed_generation_ends_a_load_by_a_kill(world: World) -> None:
    world.generation(1)
    world.save()
    sup = world.supervisor()
    world.time.at(10, lambda: world.generation(2, password="hearth-and-horn", disconnect=True))

    assert sup.run() == 0
    assert world.process.killed
    assert world.process.signals == []


def test_a_held_world_takes_a_newer_generation_at_once(world: World) -> None:
    world.generation(1)
    world.save()
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.time.at(60, lambda: world.generation(2, password="hearth-and-horn"))

    assert sup.run() == 0
    assert world.held_reasons()[:1] == [supervisor.HELD_OVER_SAVE]
    assert world.time.now < 70


def test_an_unverified_world_waits_for_a_count_it_never_gets(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    world.time.at(supervisor.LOAD_TIMEOUT_S + 10, lambda: world.generation(2, password="hearth-and-horn"))
    world.stop_at(supervisor.LOAD_TIMEOUT_S + 120, sup)

    assert sup.run() == 0
    assert "unverified" in world.states()
    assert world.time.now >= supervisor.LOAD_TIMEOUT_S + 120


def test_a_generation_handed_during_the_install_is_the_one_launched(world: World) -> None:
    world.generation(1)
    world.uninstall_server()
    sup: Supervisor

    def run_steamcmd(command: list[str], timeout: float, stopping: steam.Stopping) -> None:
        world.generation(2, password="hearth-and-horn")
        world.install_server()

    sup = world.supervisor(run_steamcmd)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert len(world.launches) == 1
    assert "hearth-and-horn" in world.launches[0]


def test_a_stop_while_loading_kills_rather_than_saves(world: World) -> None:
    world.generation()
    world.save()
    sup = world.supervisor()
    world.stop_at(10, sup)

    assert sup.run() == 0
    assert world.process.killed
    assert world.process.signals == []


def test_a_newer_generation_with_the_same_settings_runs_on(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(10))
    world.time.at(60, lambda: world.generation(2))
    world.stop_at(120, sup)

    assert sup.run() == 0
    assert len(world.launches) == 1
    assert world.process.signals == [signal.SIGINT]
    assert world.reports[-1]["generation"] == 2


def test_a_generation_without_its_secrets_launches_nothing(world: World) -> None:
    world.generation(secrets=False)
    sup = world.supervisor()
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.launches == []


def test_secrets_for_another_generation_launch_nothing(world: World) -> None:
    world.secrets(1, password="hearth-and-horn")
    world.generation(2, secrets=False)
    sup = world.supervisor()
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.launches == []


def test_a_newer_generation_waits_for_its_secrets(world: World) -> None:
    world.generation(1, password="hearth-and-horn")
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(10))
    world.time.at(60, lambda: world.generation(2, password="hearth-and-horn", secrets=False))
    world.stop_at(120, sup)

    assert sup.run() == 0
    assert len(world.launches) == 1
    assert world.reports[-1]["generation"] == 1


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
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert len(world.launches) == 1


def test_an_invalid_generation_is_refused_and_nothing_launches(world: World) -> None:
    (world.paths.generation / "generation.json").write_text(json.dumps({"contract": 2}))
    sup = world.supervisor()
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.launches == []


def test_a_server_that_will_not_stop_is_killed(world: World) -> None:
    world.generation()
    world.process.wait_times_out = True
    sup = world.supervisor()
    world.plugin_running(10)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.process.signals == [signal.SIGINT]
    assert world.process.killed


def test_a_stop_during_the_install_launches_nothing(world: World) -> None:
    world.generation()
    world.uninstall_server()
    sup: Supervisor

    def run_steamcmd(command: list[str], timeout: float, stopping: steam.Stopping) -> None:
        sup.request_stop(signal.SIGTERM, None)
        if stopping():
            raise steam.Stopped("stop requested while SteamCMD ran")

    sup = world.supervisor(run_steamcmd)

    assert sup.run() == 0
    assert world.launches == []


def test_status_keeps_reporting_a_long_install(world: World) -> None:
    world.generation()

    def run_steamcmd(command: list[str], timeout: float, stopping: steam.Stopping) -> None:
        for _ in range(4):
            world.time.sleep(5)
            stopping()

    sup = world.supervisor(run_steamcmd)
    world.stop_at(60, sup)

    assert sup.run() == 0
    installing = [r for r in world.reports if r["state"] == "installing"]
    assert len(installing) >= 4
