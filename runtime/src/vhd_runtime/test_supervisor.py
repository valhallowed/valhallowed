import errno
import itertools
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.error import URLError

import pytest

from vhd_runtime import backups, generation, restore, status, steam, supervisor, transfer
from vhd_runtime.generation import Reader, validator
from vhd_runtime.supervisor import Clock, Paths, Supervisor
from vhd_runtime.test_backups import RECORD, write_save
from vhd_runtime.test_steam import APP_INFO, NO_CONNECTION, STEAMCMD, Download

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
STATUS_URL = "http://control-plane:9457/status"
STATUS = validator(CONTRACTS / "game-runtime.status.schema.json")
# The wall clock when the fake one reads 0.
START = datetime(2026, 11, 18, 20, 0, 0, tzinfo=UTC)
T = TypeVar("T")
RESTORE_ID = "5f0c9a3e1b7d4c2a8e6f0b1d3c5a7e9f"


class Deferred(Executor):
    """Runs a task only when its result is asked for, so the loop sees a copy still under way."""

    def __init__(self) -> None:
        self.pending: list[Future[Any]] = []

    def submit(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> Future[T]:
        executor = self

        class Waiting(Future[T]):
            def result(self, timeout: float | None = None) -> T:
                if not self.done():
                    executor.pending.remove(self)
                    try:
                        self.set_result(fn(*args, **kwargs))
                    except Exception as error:
                        self.set_exception(error)
                return super().result(timeout)

        future = Waiting()
        self.pending.append(future)
        return future


class Immediate(Executor):
    """Runs each task as it is submitted, so a backup copied off the loop lands in fake time."""

    def submit(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> Future[T]:
        future: Future[T] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as error:
            future.set_exception(error)
        return future


class FakeProcess:
    def __init__(self) -> None:
        self.code: int | None = None
        self.signals: list[int] = []
        self.killed = False
        self.wait_times_out = False
        # What the game does on SIGINT before it exits, such as writing its last save.
        self.on_interrupt: Callable[[], None] = lambda: None

    def poll(self) -> int | None:
        return self.code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if sig == signal.SIGINT:
            self.on_interrupt()

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
            backups=root / "backups",
        )
        for directory in (
            self.paths.backups,
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
        # Steam's public game build, as a check finds it: the installed one unless a test says otherwise,
        # and None for a Steam SteamCMD cannot reach. When each check ran, and what it could ask about a stop.
        self.public: str | None = "25909431"
        self.checks: list[float] = []
        self.check_stopping: list[steam.Stopping] = []

    def generation(
        self,
        number: int = 1,
        password: str | None = None,
        secrets: bool = True,
        disconnect: bool = False,
        *,
        keep: int = 24,
        keep_daily: int = 14,
        every_s: int = 3600,
        restore: str | None = None,
        restore_id: str = RESTORE_ID,
        policy: str = "when-empty",
        update_every_s: int = 900,
    ) -> None:
        """Write a generation, and unless told not to, its secrets, as the control plane does."""
        if secrets:
            self.secrets(number, password)
        document: dict[str, object] = {
            "contract": 4,
            "generation": number,
            "world": {"name": "Meadowfold", "port": 2456},
            "plugin": {"status_interval_s": 5, "intake_url": "http://intake:9458/events"},
            "backups": {"every_s": every_s, "keep": keep, "keep_daily": keep_daily},
            "updates": {"policy": policy, "every_s": update_every_s},
            "status_url": STATUS_URL,
        }
        if disconnect:
            document["disconnect_players"] = True
        if restore is not None:
            document["restore"] = {"id": restore_id, "backup": restore}
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

    def game_saves(self, number: int, *, missing: tuple[str, ...] = ()) -> None:
        """The game writing save number now, its one chunk at version number, as a 1.0+ save."""
        chunk = {"chunk": 258, "size": 64, "version": number, "objects": 100 + number}
        write_save(
            self.paths.saves / "worlds_local" / "Meadowfold",
            number,
            [chunk],
            missing=missing,
            seconds_ago=-self.time.now,
        )

    def now(self) -> datetime:
        """The wall clock, as the supervisor reads it."""
        return START + timedelta(seconds=self.time.now)

    def backed_up(self) -> list[int]:
        """The save each backup holds, oldest first."""
        return [
            json.loads((self.paths.backups / name / "backup.json").read_text())["save"]
            for name in reversed(backups.names(self.paths.backups, self.now()))
        ]

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

    def supervisor(self, run_steamcmd: steam.Run | None = None, fetch: steam.Fetch | None = None) -> Supervisor:
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
            query_steamcmd=self.query_steamcmd,
            fetch=fetch or steam.fetch,
            jitter=lambda low, high: high,
            post=post,
            clock=Clock(now=lambda: self.time.now, sleep=self.time.sleep),
            environ={},
            executor=Immediate(),
            checker=Immediate(),
            wall=self.now,
        )

    def run_steamcmd(self, command: list[str], timeout: float, stopping: steam.Stopping) -> None:
        self.steamcmd_runs.append(command)

    def query_steamcmd(self, command: list[str], timeout: float, stopping: steam.Stopping) -> str:
        self.checks.append(self.time.now)
        self.check_stopping.append(stopping)
        return NO_CONNECTION if self.public is None else APP_INFO.replace("{build}", self.public)

    def updates(self) -> list[object]:
        """The update each report names, a repeated one counted once."""
        seen: list[object] = []
        for report in self.reports:
            update = report.get("update")
            if not seen or seen[-1] != update:
                seen.append(update)
        return seen

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

    def backup_of(self, number: int, *, hours_ago: int = 2) -> str:
        """A backup of save number, taken hours ago from a copy of the world as it was then."""
        then = self.paths.data / "then" / "Meadowfold"
        write_save(then, number, [{"chunk": 258, "size": 64, "version": number, "objects": 100 + number}])
        taken = backups.take(
            backups.Place(then, self.paths.backups, "Meadowfold", RECORD), self.now() - timedelta(hours=hours_ago)
        )
        assert taken is not None
        shutil.rmtree(then.parent)
        return taken.name

    def save_number(self) -> int | None:
        save = backups.newest_complete(self.paths.saves / "worlds_local" / "Meadowfold")
        return None if save is None else save.number

    def restores(self) -> list[object]:
        """The restore outcomes reported, a repeated one counted once."""
        outcomes: list[object] = []
        for report in self.reports:
            outcome = cast(dict[str, object], report.get("restore", {})).get("outcome")
            if outcome is not None and (not outcomes or outcomes[-1] != outcome):
                outcomes.append(outcome)
        return outcomes

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


def test_a_runtime_without_its_backup_records_contract_stops_before_it_installs_or_restores(
    world: World, tmp_path: Path
) -> None:
    contracts = tmp_path / "contracts"
    shutil.copytree(CONTRACTS, contracts)
    (contracts / restore.RECORD_CONTRACT).unlink()
    paths = Paths(
        generation=world.paths.generation,
        data=world.paths.data,
        contracts=contracts,
        world_status=world.paths.world_status,
        backups=world.paths.backups,
    )
    with pytest.raises(restore.RestoreFailed, match="cannot read the contract of a backup's record"):
        Supervisor(paths=paths, reader=Reader(paths.generation, CONTRACTS))


def test_a_loaded_world_runs_and_saves_on_stop(world: World) -> None:
    world.generation()
    world.save()
    sup = world.supervisor()
    world.plugin_running(48113)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.states() == ["installing", "loading", "running"]
    assert world.reports[-2]["objects"] == 48113
    # The last word after the stop: the plugin's counts went with the game.
    assert "objects" not in world.reports[-1] and "players" not in world.reports[-1]
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


def test_the_plugin_finds_its_map_export_directory_at_launch(world: World) -> None:
    world.generation()
    sup = world.supervisor()
    launch = sup.launch
    at_launch: list[tuple[bool, str]] = []

    def checking_launch(command: list[str], env: dict[str, str], cwd: Path) -> supervisor.Process:
        config = world.paths.server / "BepInEx" / "config" / "com.valhallowed.plugin.cfg"
        at_launch.append((world.paths.map_export.is_dir(), config.read_text()))
        return launch(command, env, cwd)

    sup.launch = checking_launch
    world.stop_at(60, sup)

    assert sup.run() == 0
    [(exists, config)] = at_launch
    assert exists
    assert f"[MapExport]\nDirectory = {world.paths.map_export}\n" in config


def test_an_earlier_map_export_is_kept_across_a_start(world: World) -> None:
    export = world.paths.map_export / "export"
    export.parent.mkdir(parents=True)
    export.write_bytes(b"map")
    world.generation()
    sup = world.supervisor()
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert len(world.launches) == 1
    assert export.read_bytes() == b"map"


def test_a_map_export_directory_that_cannot_be_made_does_not_stop_the_world(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.paths.map_export.parent.mkdir(parents=True, exist_ok=True)
    world.paths.map_export.write_bytes(b"not a directory")
    world.generation()
    sup = world.supervisor()
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert len(world.launches) == 1
    assert "the web map gets no export" in caplog.text


def test_the_map_export_directory_is_named_where_the_web_map_mounts_it() -> None:
    # The web map mounts this directory of the world's data volume by name (docs/architecture.md).
    assert Paths().map_export == Path("/data/map-export")


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


def test_a_failed_download_shows_why_and_is_retried_with_backoff(world: World) -> None:
    world.generation(1)
    (world.paths.steamcmd / "steamcmd.sh").unlink()
    tried: list[float] = []

    def fetch(url: str, timeout: float) -> Download:
        tried.append(world.time.now)
        if len(tried) < 3:
            raise URLError("[Errno -3] Temporary failure in name resolution")
        return Download(STEAMCMD)

    sup = world.supervisor(fetch=fetch)
    world.plugin_running(100, start=200)
    world.stop_at(400, sup)

    assert sup.run() == 0
    assert [round(b - a) for a, b in itertools.pairwise(tried)] == [supervisor.INSTALL_RETRY_FIRST_S, 60]
    reasons = {r.get("reason") for r in world.reports if r["state"] == "installing"}
    assert any("name resolution" in str(reason) for reason in reasons)
    assert len(world.launches) == 1
    assert "running" in world.states()


def test_a_stop_while_an_install_waits_to_retry_launches_nothing(world: World) -> None:
    world.generation(1)
    (world.paths.steamcmd / "steamcmd.sh").unlink()

    def fetch(url: str, timeout: float) -> Download:
        raise URLError("[Errno 101] Network is unreachable")

    sup = world.supervisor(fetch=fetch)
    world.stop_at(10, sup)

    assert sup.run() == 0
    assert world.launches == []
    assert world.time.now < supervisor.INSTALL_RETRY_FIRST_S


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


def test_a_newer_generation_that_changes_only_backups_runs_on_with_them(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(10))
    world.time.at(60, lambda: world.generation(2, keep=48))
    world.stop_at(120, sup)

    assert sup.run() == 0
    assert len(world.launches) == 1
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
    (world.paths.generation / "generation.json").write_text(json.dumps({"contract": 3}))
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


def test_a_running_world_backs_up_each_new_save_at_most_as_often_as_its_settings_say(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(10)
    world.time.at(100, lambda: world.game_saves(1))
    # Within the hour its settings allow, so save 2 is never backed up: once the hour has passed,
    # save 3 is the newest.
    world.time.at(1900, lambda: world.game_saves(2))
    world.time.at(2500, lambda: world.game_saves(3))
    world.stop_at(3750, sup)

    assert sup.run() == 0
    assert world.backed_up() == [1, 3]
    names = backups.names(world.paths.backups, world.now())
    assert names[-1].startswith("20261118T2001")
    assert world.reports[-1]["backups"] == {"names": names}


def test_a_report_names_every_backup_pruning_keeps_and_no_more_than_its_contract_takes() -> None:
    names = json.loads((CONTRACTS / "game-runtime.status.schema.json").read_text())["properties"]["backups"]
    manifest = json.loads((CONTRACTS.parent / "schema" / "manifest.schema.json").read_text())
    counts = manifest["properties"]["backups"]["properties"]
    # keep and keep_daily at their most, the backup a restore asks for, and the newest import.
    kept = counts["keep"]["maximum"] + counts["keep_daily"]["maximum"] + 2
    assert supervisor.BACKUP_NAMES_MAX == names["properties"]["names"]["maxItems"] == kept


def test_a_clean_stop_backs_up_the_save_the_game_stopped_with(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(10)
    world.time.at(100, lambda: world.game_saves(1))
    world.process.on_interrupt = lambda: world.game_saves(2)
    world.stop_at(600, sup)

    assert sup.run() == 0
    assert world.backed_up() == [1, 2]


def test_a_held_world_has_its_real_save_backed_up_at_start_and_nothing_at_its_stop(world: World) -> None:
    world.save()
    world.game_saves(5)
    world.remember("Meadowfold", 54)
    world.generation(1)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.stop_at(120, sup)

    assert sup.run() == 0
    assert world.held_reasons()
    # At start, before the world loads empty; the stop, a kill, saved nothing to back up.
    assert world.backed_up() == [5]
    assert world.reports[-1]["backups"] == {"names": backups.names(world.paths.backups, world.now())}


def test_a_start_clears_cut_short_copies_keeps_what_the_settings_say_and_backs_up_no_save_twice(
    world: World,
) -> None:
    for hours_ago, save in ((3, 3), (2, 4), (1, 5)):
        world.game_saves(save)
        backups.take(
            backups.Place(world.paths.saves / "worlds_local" / "Meadowfold", world.paths.backups, "Meadowfold", RECORD),
            START - timedelta(hours=hours_ago),
        )
    (world.paths.backups / ".partial-20261118T195959Z").mkdir()
    world.generation(1, keep=2)
    sup = world.supervisor()
    world.plugin_running(10)
    world.stop_at(600, sup)

    assert sup.run() == 0
    # The oldest went: the newest two stay, the 18th's newest is one of them, and save 5, backed
    # up an hour ago, is not backed up again, even by the stop.
    assert world.backed_up() == [4, 5]
    first = next(report for report in world.reports if "backups" in report)
    assert first["backups"] == {"names": ["20261118T190000Z", "20261118T180000Z"]}
    assert not (world.paths.backups / ".partial-20261118T195959Z").exists()


def test_a_failed_backup_says_why_until_one_succeeds(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(10)
    world.time.at(100, lambda: world.game_saves(1, missing=("01_02__64_1.chunk",)))
    world.time.at(400, lambda: world.game_saves(2))
    world.stop_at(600, sup)
    failures: list[object] = []
    world.time.at(300, lambda: failures.append(world.reports[-1]["backups"]))

    assert sup.run() == 0
    assert failures == [{"names": [], "failure": "save 1 lacks 01_02__64_1.chunk, which its .chunks file names"}]
    assert world.backed_up() == [2]
    assert world.reports[-1]["backups"] == {"names": backups.names(world.paths.backups, world.now())}


def test_a_stop_waits_for_a_copy_under_way_and_backs_up_no_save_twice(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    deferred = Deferred()
    sup.executor = deferred
    # The start's turn is still copying when the world runs; so is the turn that finds save 1.
    world.plugin_running(10)
    world.time.at(100, lambda: world.game_saves(1))
    world.stop_at(200, sup)

    assert sup.run() == 0
    assert world.backed_up() == [1]
    assert deferred.pending == []


def test_a_failed_stop_backup_is_the_last_word(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(10)
    world.process.on_interrupt = lambda: world.game_saves(2, missing=("01_02__64_2.chunk",))
    world.stop_at(200, sup)

    assert sup.run() == 0
    assert world.reports[-1]["backups"] == {
        "names": [],
        "failure": "save 2 lacks 01_02__64_2.chunk, which its .chunks file names",
    }


def test_a_backup_named_ahead_of_the_clock_delays_the_next_no_more_than_the_interval(world: World) -> None:
    world.game_saves(5)
    backups.take(
        backups.Place(world.paths.saves / "worlds_local" / "Meadowfold", world.paths.backups, "Meadowfold", RECORD),
        START + timedelta(hours=6),
    )
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(10)
    world.time.at(100, lambda: world.game_saves(6))
    world.stop_at(3700, sup)

    assert sup.run() == 0
    assert world.backed_up() == [5, 6]
    names = backups.names(world.paths.backups, world.now())
    assert names[0] > names[1]


def test_an_unexpected_error_in_the_backups_never_stops_the_world(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> backups.Tended:
        raise RuntimeError("a bug")

    monkeypatch.setattr(backups, "tend", broken)
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(10)
    world.stop_at(200, sup)

    assert sup.run() == 0
    assert world.process.signals == [signal.SIGINT]
    report = world.reports[-1]
    assert report["state"] == "running"
    assert report["backups"] == {"names": [], "failure": "backups stopped on an unexpected error: RuntimeError: a bug"}


def test_a_backup_that_keeps_failing_is_tried_again_less_often(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    tried: list[float] = []
    tend = backups.tend

    def counted(*args: Any, **kwargs: Any) -> backups.Tended:
        tried.append(world.time.now)
        return tend(*args, **kwargs)

    monkeypatch.setattr(backups, "tend", counted)
    world.game_saves(1, missing=("01_02__64_1.chunk",))
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(10)
    world.stop_at(1000, sup)

    assert sup.run() == 0
    gaps = [round(later - earlier) for earlier, later in itertools.pairwise(tried[1:])]
    # Doubling from a minute, each gap up to a poll longer as the loop notes the failed turn.
    assert all(wait <= gap <= wait + 2 for gap, wait in zip(gaps[:3], (120, 240, 480), strict=True))


def test_the_largest_report_fits_the_status_listener() -> None:
    sent: list[bytes] = []
    # control/src/vhd_control/listeners.py, MAX_BODY: the control plane refuses a longer report.
    max_body = 16 * 1024
    longest = "\U0001f6e1" * supervisor.REASON_MAX
    report = {
        "contract": 1,
        "generation": 2**31,
        "world": "M" * 64,
        "state": "unverified",
        "reason": longest,
        "game_build": "9" * 20,
        "update": {"build": "9" * 20, "failure": longest},
        "objects": 2**31,
        "players": 2**31,
        "backups": {"names": ["20261118T204012Z"] * supervisor.BACKUP_NAMES_MAX, "failure": longest},
        "restore": {"id": RESTORE_ID, "outcome": "rolled-back", "reason": longest},
    }
    STATUS.validate(report)
    status.Reporter(STATUS_URL, lambda url, body, timeout: sent.append(body)).send(cast(Any, report))
    assert len(sent[0]) <= max_body


def test_a_start_turn_that_fails_is_tried_again_as_one(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    starting: list[bool] = []
    tend = backups.tend

    def flaky(*args: Any, **kwargs: Any) -> backups.Tended:
        starting.append(kwargs["starting"])
        if len(starting) == 1:
            raise OSError(5, "Input/output error")
        return tend(*args, **kwargs)

    monkeypatch.setattr(backups, "tend", flaky)
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(10)
    world.stop_at(400, sup)

    assert sup.run() == 0
    assert starting[:3] == [True, True, False]


def test_a_shorter_interval_set_while_the_world_runs_holds_from_the_next_backup(world: World) -> None:
    world.generation(1, every_s=86400)
    sup = world.supervisor()
    world.plugin_running(10)
    world.time.at(100, lambda: world.game_saves(1))
    world.time.at(200, lambda: world.generation(2, every_s=3600))
    world.time.at(300, lambda: world.game_saves(2))
    # Late enough that a backup only at the stop would come well over the new hour after the first.
    world.stop_at(5000, sup)

    assert sup.run() == 0
    assert world.backed_up() == [1, 2]
    taken = [backups.taken(name) for name in reversed(backups.names(world.paths.backups, world.now()))]
    assert (taken[1] - taken[0]).total_seconds() < 3700


def place_of(world: World) -> restore.Place:
    return restore.Place(world.paths.saves, world.paths.vhd, world.paths.backups, "Meadowfold")


def restore_reasons(world: World) -> list[object]:
    return [cast(dict[str, object], r["restore"]).get("reason") for r in world.reports if "restore" in r]


def test_a_restore_checks_its_backup_with_the_record_contract_read_at_the_start(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.game_saves(5)
    world.remember("Meadowfold", 105)
    name = world.backup_of(3)
    world.generation(restore=name)
    sup = world.supervisor()

    def read_again(contracts: Path) -> object:
        raise AssertionError("read the record's contract again")

    load = generation.validator

    def loaded_again(path: Path) -> object:
        if path.name == restore.RECORD_CONTRACT:
            raise AssertionError("loaded the record's contract again")
        return load(path)

    monkeypatch.setattr(restore, "record_contract", read_again)
    monkeypatch.setattr(generation, "validator", loaded_again)
    world.plugin_running(103)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.restores() == ["restoring", "done"]


def test_a_restore_swaps_the_backup_in_before_the_install_and_world_truth_passes_it(world: World) -> None:
    world.game_saves(5)
    world.remember("Meadowfold", 105)
    name = world.backup_of(3)
    world.generation(restore=name)
    sup = world.supervisor()
    world.plugin_running(103)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.save_number() == 3
    assert world.states() == ["held", "installing", "loading", "running"]
    assert world.reports[0]["reason"] == f"restoring backup {name}"
    assert world.restores() == ["restoring", "done"]
    # The save it replaced was backed up first, and the restored one again once it launched, so the
    # newest backup holds the save the world runs on.
    assert world.backed_up() == [3, 5, 3]
    assert not place_of(world).aside.exists() and not place_of(world).intent.exists()


def test_a_restored_save_that_loads_empty_is_swapped_back_and_the_next_start_runs_the_old_one(
    world: World,
) -> None:
    world.game_saves(5)
    world.remember("Meadowfold", 105)
    name = world.backup_of(3)
    world.generation(restore=name)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.stop_at(600, sup)

    assert sup.run() == 0
    assert world.process.killed and world.process.signals == []
    assert world.save_number() == 5
    assert world.restores() == ["restoring", "rolled-back"]
    assert "loaded with no objects where it held 103; the save it replaced is back" in str(restore_reasons(world)[-1])
    assert json.loads(world.paths.last_objects.read_text())["objects"] == 105

    # Docker starts the container again: the same generation's restore is done, and not done twice.
    world.process, world.reports = FakeProcess(), []
    again = world.supervisor()
    world.plugin_running(105, start=int(world.time.now) + 30)
    world.stop_at(world.time.now + 60, again)
    assert again.run() == 0
    assert world.save_number() == 5
    assert "held" not in world.states()
    assert world.restores() == ["rolled-back"]


def test_a_load_the_plugin_never_reports_holds_the_restored_save_without_swapping_back(world: World) -> None:
    world.game_saves(5)
    world.remember("Meadowfold", 105)
    world.generation(restore=world.backup_of(3))
    sup = world.supervisor()
    world.stop_at(supervisor.LOAD_TIMEOUT_S + 60, sup)

    assert sup.run() == 0
    assert world.save_number() == 3 and place_of(world).aside.exists()
    assert world.restores() == ["restoring"]
    assert "world truth has not judged the restored save" in str(restore_reasons(world)[-1])
    intent = restore.read_intent(place_of(world))
    assert intent is not None and intent.phase == "swapped"


def test_a_backup_that_is_gone_is_refused_and_the_world_starts_on_its_own_save(world: World) -> None:
    world.game_saves(5)
    world.generation(restore="20261118T100000Z")
    sup = world.supervisor()
    world.plugin_running(105)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.save_number() == 5
    assert world.restores() == ["restoring", "failed"]
    assert "no longer exists" in str(restore_reasons(world)[-1])
    assert len(world.launches) == 1


def test_a_restore_refused_for_a_save_set_aside_names_where_it_is_in_the_data_volume(world: World) -> None:
    world.game_saves(5)
    name = world.backup_of(3)
    place_of(world).aside.mkdir(parents=True)
    world.generation(restore=name)
    sup = world.supervisor()
    world.plugin_running(105)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.restores() == ["failed"]
    assert restore_reasons(world)[-1] == (
        "a save an earlier restore set aside is still in the world's data volume, at vhd/restore/aside"
    )


def test_a_world_that_never_started_loads_the_restored_save_at_its_first_start(world: World) -> None:
    name = world.backup_of(3)
    world.generation(restore=name)
    sup = world.supervisor()
    world.plugin_running(103)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.save_number() == 3
    assert world.restores() == ["restoring", "done"]


def test_a_world_that_never_started_loads_an_imported_save_at_its_first_start(world: World) -> None:
    mounts = transfer.Mounts(imported=world.paths.data / "import", backups=world.paths.backups, contracts=CONTRACTS)
    write_save(mounts.imported / "Meadowfold", 41, [{"chunk": 258, "size": 64, "version": 41, "objects": 141}])
    name = transfer.import_save(mounts, "Meadowfold", world.now)["backup"]
    world.generation(restore=name)
    sup = world.supervisor()
    world.plugin_running(141)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.save_number() == 41 and len(world.launches) == 1
    assert world.states() == ["held", "installing", "loading", "running"]
    assert world.restores() == ["restoring", "done"]


def test_a_world_with_a_pre_1_0_save_is_not_restored(world: World) -> None:
    (world.paths.saves / "worlds_local").mkdir(parents=True)
    (world.paths.saves / "worlds_local" / "Meadowfold.db").write_bytes(b"pre-1.0")
    world.generation(restore=world.backup_of(3))
    sup = world.supervisor()
    world.plugin_running(40)
    world.stop_at(60, sup)

    assert sup.run() == 0
    # Refused before anything was touched, so no report ever said it was restoring.
    assert world.restores() == ["failed"]
    assert "pre-1.0" in str(restore_reasons(world)[-1])
    assert world.save_number() is None


def test_a_save_that_cannot_be_backed_up_is_kept_aside_until_world_truth_passes(world: World) -> None:
    world.game_saves(5, missing=("_main.5.db2",))
    world.remember("Meadowfold", 105)
    world.generation(restore=world.backup_of(3))
    sup = world.supervisor()
    aside: list[bool] = []
    world.time.at(20, lambda: aside.append(place_of(world).aside.exists()))
    world.plugin_running(103)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert aside == [True] and not place_of(world).aside.exists()
    assert world.restores() == ["restoring", "done"]
    assert any("could not be backed up" in str(r) for r in restore_reasons(world))
    assert world.backed_up() == [3, 3]


def test_a_running_world_restarts_for_a_new_restore_and_restores_at_its_next_start(world: World) -> None:
    world.game_saves(5)
    name = world.backup_of(3)
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(105)
    world.time.at(60, lambda: world.generation(2, restore=name))
    world.stop_at(600, sup)

    assert sup.run() == 0
    assert world.process.signals == [signal.SIGINT]
    assert world.save_number() == 5

    world.process = FakeProcess()
    again = world.supervisor()
    world.plugin_running(103, start=int(world.time.now) + 30)
    world.stop_at(world.time.now + 60, again)
    assert again.run() == 0
    assert world.save_number() == 3
    assert world.restores()[-1] == "done"


def test_a_generation_that_drops_a_done_restore_restarts_nothing(world: World) -> None:
    world.game_saves(5)
    restore.finish(place_of(world), restore.Done(RESTORE_ID, "done"))
    world.generation(1, restore=world.backup_of(3))
    sup = world.supervisor()
    world.plugin_running(105)
    world.time.at(60, lambda: world.generation(2))
    world.stop_at(120, sup)

    assert sup.run() == 0
    assert world.save_number() == 5
    assert len(world.launches) == 1 and world.reports[-1]["generation"] == 2
    assert world.restores() == ["done"]


def test_a_swap_a_crash_cut_short_is_finished_and_judged_at_the_next_start(world: World) -> None:
    world.game_saves(5)
    world.remember("Meadowfold", 105)
    name = world.backup_of(3)
    place = place_of(world)
    restore.stage(place, name, RECORD)
    place.save.rename(place.aside)
    restore.write_intent(place, restore.Intent(RESTORE_ID, name, "swapping", before=105, had_save=True, objects=103))
    world.generation(restore=name)
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.stop_at(600, sup)

    # Judged against the backup it restored, which held objects: an empty load swaps it back.
    assert sup.run() == 0
    assert world.save_number() == 5
    assert world.restores() == ["restoring", "rolled-back"]


def test_a_swap_back_a_crash_cut_short_is_finished_at_the_next_start(world: World) -> None:
    world.game_saves(5)
    name = world.backup_of(3)
    place = place_of(world)
    restore.stage(place, name, RECORD)
    restore.swap(place, had_save=True)
    restore.write_intent(
        place, restore.Intent(RESTORE_ID, name, "unswapping", before=105, had_save=True, objects=103, reason="empty")
    )
    world.generation(restore=name)
    sup = world.supervisor()
    world.plugin_running(105)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.save_number() == 5 and not place.aside.exists()
    assert world.restores() == ["rolled-back"]
    assert json.loads(world.paths.last_objects.read_text())["objects"] == 105


def test_a_newer_restore_puts_back_the_save_of_one_still_unjudged_first(world: World) -> None:
    world.game_saves(5)
    first, second = world.backup_of(3, hours_ago=3), world.backup_of(4)
    place = place_of(world)
    restore.stage(place, first, RECORD)
    restore.swap(place, had_save=True)
    restore.write_intent(place, restore.Intent("a" * 32, first, "swapped", before=105, had_save=True, objects=103))
    world.generation(restore=second)
    sup = world.supervisor()
    world.plugin_running(104)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.save_number() == 4
    # The save the first replaced was put back, then backed up before the second replaced it.
    assert world.backed_up() == [3, 4, 5, 4]
    assert world.restores() == ["restoring", "done"]
    done = restore.read_done(place)
    assert done is not None and (done.id, done.outcome) == (RESTORE_ID, "done")


class Slow(Executor):
    """Finishes one kind of task a while after it is submitted, in fake time, as a slow disk does,
    and every other task at once."""

    def __init__(self, time: FakeTime, seconds: float, slow: Callable[..., object] = restore.stage) -> None:
        self.time, self.seconds, self.slow = time, seconds, slow

    def submit(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> Future[T]:
        if fn is not self.slow:
            return Immediate().submit(fn, *args, **kwargs)
        future: Future[T] = Future()

        def finish() -> None:
            try:
                future.set_result(fn(*args, **kwargs))
            except Exception as error:
                future.set_exception(error)

        self.time.at(self.time.now + self.seconds, finish)
        return future


def test_status_keeps_reporting_the_world_held_while_a_slow_restore_copies(world: World) -> None:
    world.game_saves(5)
    name = world.backup_of(3)
    world.generation(restore=name)
    sup = world.supervisor()
    sup.executor = Slow(world.time, 20)
    world.plugin_running(103, start=60)
    world.stop_at(120, sup)

    assert sup.run() == 0
    restoring = [r for r in world.reports if r.get("reason") == f"restoring backup {name}"]
    # One report on every interval of the 20 seconds the copy takes.
    assert len(restoring) >= 20 // 5
    assert world.save_number() == 3


def test_a_restore_handed_during_the_install_is_done_before_any_launch(world: World) -> None:
    world.game_saves(5)
    name = world.backup_of(3)
    world.generation(1)

    def install(command: list[str], timeout: float, stopping: steam.Stopping) -> None:
        world.generation(2, restore=name)

    sup = world.supervisor(run_steamcmd=install)
    assert sup.run() == 0
    assert world.launches == [] and world.save_number() == 5

    again = world.supervisor()
    world.plugin_running(103, start=int(world.time.now) + 30)
    world.stop_at(world.time.now + 60, again)
    assert again.run() == 0
    assert world.save_number() == 3 and world.restores()[-1] == "done"


def test_the_stop_for_a_restore_keeps_the_backup_it_asks_for(world: World) -> None:
    world.game_saves(5)
    name = world.backup_of(3)
    world.generation(1, keep=2, keep_daily=0)
    sup = world.supervisor()
    world.plugin_running(105)
    world.process.on_interrupt = lambda: world.game_saves(6)
    world.time.at(60, lambda: world.generation(2, keep=2, keep_daily=0, restore=name))
    world.stop_at(600, sup)
    assert sup.run() == 0
    # The stop backed up save 6, and two kept would have pruned the backup the restore asks for.
    assert world.backed_up() == [3, 5, 6]

    world.process = FakeProcess()
    again = world.supervisor()
    world.plugin_running(103, start=int(world.time.now) + 30)
    world.stop_at(world.time.now + 60, again)
    assert again.run() == 0
    assert world.save_number() == 3 and world.restores()[-1] == "done"
    assert world.backed_up()[-1] == 3


def test_a_restored_save_that_ends_the_game_as_it_loads_is_swapped_back(world: World) -> None:
    world.game_saves(5)
    world.remember("Meadowfold", 105)
    world.generation(restore=world.backup_of(3))
    sup = world.supervisor()
    world.time.at(20, lambda: setattr(world.process, "code", 134))

    assert sup.run() == 134
    assert world.save_number() == 5
    # The game is gone, so the next start reports it.
    done = restore.read_done(place_of(world))
    assert done is not None and done.outcome == "rolled-back" and "exit with code 134" in str(done.reason)


def test_a_copy_a_crash_cut_short_is_dropped_and_the_restore_done_again(world: World) -> None:
    world.game_saves(5)
    name = world.backup_of(3)
    place = place_of(world)
    (place.staged).mkdir(parents=True)
    (place.staged / "_main.3.db2").write_bytes(b"half")
    restore.write_intent(place, restore.Intent(RESTORE_ID, name, "staging", before=105, had_save=True))
    world.generation(restore=name)
    sup = world.supervisor()
    world.plugin_running(103)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.save_number() == 3 and world.restores()[-1] == "done"


def test_a_judged_restore_a_crash_cut_short_is_finished_without_being_done_again(world: World) -> None:
    world.game_saves(3)
    place = place_of(world)
    (place.aside).mkdir(parents=True)
    restore.write_intent(place, restore.Intent(RESTORE_ID, "20261118T180000Z", "judged", had_save=True, objects=103))
    world.generation(restore="20261118T180000Z")
    sup = world.supervisor()
    world.plugin_running(103)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert not place.aside.exists() and world.save_number() == 3
    assert world.restores() == ["done"] and "held" not in world.states()


def test_a_crash_between_the_done_record_and_forgetting_the_intent_finishes_it_again(world: World) -> None:
    world.game_saves(5)
    place = place_of(world)
    restore.finish(place, restore.Done(RESTORE_ID, "rolled-back", "empty"))
    restore.write_intent(
        place, restore.Intent(RESTORE_ID, "20261118T180000Z", "unswapping", before=105, had_save=True, reason="empty")
    )
    world.generation(restore="20261118T180000Z")
    sup = world.supervisor()
    world.plugin_running(105)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.save_number() == 5 and restore.read_intent(place) is None
    assert world.restores() == ["rolled-back"]


def test_a_restore_the_generation_drops_before_world_truth_judges_it_is_swapped_back(world: World) -> None:
    world.game_saves(5)
    name = world.backup_of(3)
    place = place_of(world)
    restore.stage(place, name, RECORD)
    restore.swap(place, had_save=True)
    restore.write_intent(place, restore.Intent(RESTORE_ID, name, "swapped", before=105, had_save=True, objects=103))
    world.generation()
    sup = world.supervisor()
    world.plugin_running(105)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.save_number() == 5 and not place.aside.exists()
    assert world.restores() == ["rolled-back"]
    assert "dropped or replaced" in str(restore_reasons(world)[-1])


def test_a_newer_generation_with_the_restore_being_judged_restarts_nothing(world: World) -> None:
    world.game_saves(5)
    name = world.backup_of(3)
    world.generation(1, restore=name)
    sup = world.supervisor()
    world.time.at(10, lambda: world.generation(2, restore=name))
    world.plugin_running(103)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert len(world.launches) == 1 and world.process.signals == [signal.SIGINT]
    assert world.restores() == ["restoring", "done"]


def test_a_done_restore_is_not_done_again_at_the_next_start(world: World) -> None:
    world.game_saves(5)
    world.generation(restore=world.backup_of(3))
    sup = world.supervisor()
    world.plugin_running(103)
    world.stop_at(60, sup)
    assert sup.run() == 0

    held = world.backed_up()
    world.process, world.reports = FakeProcess(), []
    again = world.supervisor()
    world.plugin_running(103, start=int(world.time.now) + 30)
    world.stop_at(world.time.now + 60, again)
    assert again.run() == 0
    assert "held" not in world.states() and world.restores() == ["done"]
    # Nothing was set aside or copied again.
    assert world.backed_up() == held and not place_of(world).aside.exists()


def test_a_world_that_had_no_save_is_left_without_one_when_its_restore_loads_empty(world: World) -> None:
    world.generation(restore=world.backup_of(3))
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.stop_at(600, sup)

    assert sup.run() == 0
    assert not (world.paths.saves / "worlds_local" / "Meadowfold").exists()
    assert not world.paths.last_objects.exists()
    assert "the world has no save again" in str(restore_reasons(world)[-1])


def test_a_stop_before_the_copy_leaves_the_save_untouched_for_the_next_start(world: World) -> None:
    world.game_saves(5)
    world.generation(restore=world.backup_of(3))
    sup = world.supervisor()
    sup.executor = Slow(world.time, 20, backups.tend)
    world.stop_at(10, sup)

    assert sup.run() == 0
    assert world.launches == [] and world.save_number() == 5
    assert restore.read_intent(place_of(world)) is None and restore.read_done(place_of(world)) is None


def test_a_held_world_restarts_when_a_newer_generation_drops_its_unjudged_restore(world: World) -> None:
    world.game_saves(5)
    world.remember("Meadowfold", 105)
    world.generation(1, restore=world.backup_of(3))
    sup = world.supervisor()
    world.time.at(supervisor.LOAD_TIMEOUT_S + 60, lambda: world.generation(2))
    world.stop_at(5000, sup)

    assert sup.run() == 0
    assert world.time.now < 5000 and world.held_reasons()

    world.process = FakeProcess()
    again = world.supervisor()
    world.plugin_running(105, start=int(world.time.now) + 30)
    world.stop_at(world.time.now + 60, again)
    assert again.run() == 0
    assert world.save_number() == 5 and world.restores()[-1] == "rolled-back"


def test_a_world_still_loading_a_restore_a_newer_generation_drops_is_judged_first(world: World) -> None:
    world.game_saves(5)
    world.generation(1, restore=world.backup_of(3))
    sup = world.supervisor()
    world.time.at(10, lambda: world.generation(2))
    world.plugin_running(103)
    world.stop_at(120, sup)

    assert sup.run() == 0
    assert len(world.launches) == 1 and world.process.signals == [signal.SIGINT]
    assert world.save_number() == 3 and world.restores() == ["restoring", "done"]
    assert world.reports[-1]["generation"] == 2


OTHER_ID = "a" * 32


def held_without_save(world: World) -> list[str]:
    """The reasons reported for a world held because it has no save."""
    return [str(r) for r in world.held_reasons() if str(r).startswith("the world has no save")]


def test_a_failed_restore_on_a_world_with_no_save_holds_it_across_starts_and_makes_no_world(world: World) -> None:
    world.generation(1, restore="20261118T100000Z")
    sup = world.supervisor()
    # The failed outcome makes the control plane drop the restore: a newer generation without it.
    world.time.at(30, lambda: world.generation(2))
    world.stop_at(5000, sup)

    assert sup.run() == 0
    assert world.time.now < 5000
    assert world.launches == [] and world.steamcmd_runs == [] and "installing" not in world.states()
    assert world.restores() == ["restoring", "failed"]
    reasons = held_without_save(world)
    assert reasons and reasons[-1].startswith(f"the world has no save and restore {RESTORE_ID} failed, so no new world")
    assert "ask for another restore or an import, or remove the world's data volume" in reasons[-1]
    assert "no longer exists" in reasons[-1]
    # Reported on its interval while it waits.
    assert len([r for r in world.reports if r["state"] == "held"]) >= 5

    # Docker starts the container again on generation 2, which asks for no restore: still held.
    world.reports = []
    again = world.supervisor()
    world.stop_at(world.time.now + 60, again)
    assert again.run() == 0
    assert world.launches == [] and world.states() == ["held"]
    assert held_without_save(world) and world.restores() == ["failed"]
    assert not (world.paths.saves / "worlds_local" / "Meadowfold").exists()


def reported_backups(world: World) -> list[object]:
    """The backups each report named."""
    return [cast(dict[str, object], r.get("backups", {})).get("names") for r in world.reports]


def test_a_world_held_without_a_save_lists_a_backup_an_import_keeps_meanwhile(world: World) -> None:
    world.generation(1, restore="20261118T100000Z")
    sup = world.supervisor()
    mounts = transfer.Mounts(imported=world.paths.data / "import", backups=world.paths.backups, contracts=CONTRACTS)
    write_save(mounts.imported / "Meadowfold", 41, [{"chunk": 258, "size": 64, "version": 41, "objects": 141}])
    imported: list[str] = []
    world.time.at(30, lambda: imported.append(transfer.import_save(mounts, "Meadowfold", world.now)["backup"]))
    world.stop_at(30 + supervisor.BACKUP_CHECK_S + 2 * 5, sup)

    assert sup.run() == 0
    assert world.launches == [] and held_without_save(world)
    listed = reported_backups(world)
    assert [] in listed and imported and listed[-1] == imported


def test_a_new_restore_releases_a_world_held_without_a_save(world: World) -> None:
    world.generation(1, restore="20261118T100000Z")
    sup = world.supervisor()
    world.stop_at(60, sup)
    assert sup.run() == 0
    assert world.launches == [] and held_without_save(world)

    world.reports = []
    world.generation(2, restore=world.backup_of(3), restore_id=OTHER_ID)
    again = world.supervisor()
    world.plugin_running(103, start=int(world.time.now) + 30)
    world.stop_at(world.time.now + 90, again)
    assert again.run() == 0
    assert len(world.launches) == 1 and world.save_number() == 3
    assert world.restores() == ["restoring", "done"] and not held_without_save(world)


def test_a_world_with_a_save_runs_on_it_after_a_failed_restore_across_starts(world: World) -> None:
    world.game_saves(5)
    world.generation(1, restore="20261118T100000Z")
    sup = world.supervisor()
    world.plugin_running(105)
    world.stop_at(60, sup)
    assert sup.run() == 0

    world.process, world.reports = FakeProcess(), []
    world.generation(2)
    again = world.supervisor()
    world.plugin_running(105, start=int(world.time.now) + 30)
    world.stop_at(world.time.now + 60, again)
    assert again.run() == 0
    assert len(world.launches) == 2 and world.save_number() == 5
    assert world.restores() == ["failed"] and not held_without_save(world)


def test_a_rolled_back_restore_on_a_world_with_no_save_holds_it_at_the_next_start(world: World) -> None:
    world.generation(1, restore=world.backup_of(3))
    sup = world.supervisor()
    world.time.at(30, lambda: world.plugin_reports(0))
    world.stop_at(600, sup)
    assert sup.run() == 0
    assert world.restores() == ["restoring", "rolled-back"] and len(world.launches) == 1

    world.process, world.reports = FakeProcess(), []
    world.generation(2)
    again = world.supervisor()
    world.stop_at(world.time.now + 60, again)
    assert again.run() == 0
    assert len(world.launches) == 1 and world.states() == ["held"]
    reasons = held_without_save(world)
    assert reasons and reasons[-1].startswith(f"the world has no save and restore {RESTORE_ID} was rolled back")
    assert "loaded with no objects" in reasons[-1]
    # Status names the backups, so the server manager can ask for another restore.
    assert cast(dict[str, object], world.reports[-1]["backups"])["names"]


def test_the_reason_a_world_is_held_without_a_save_says_what_to_do_whole_and_cuts_only_the_why(
    world: World,
) -> None:
    restore.finish(place_of(world), restore.Done(RESTORE_ID, "failed", "w" * supervisor.REASON_MAX))
    world.generation()
    sup = world.supervisor()
    world.stop_at(30, sup)

    assert sup.run() == 0
    reasons = held_without_save(world)
    assert reasons and len(reasons[-1]) == supervisor.REASON_MAX
    assert reasons[-1].startswith(
        f"the world has no save and restore {RESTORE_ID} failed, so no new world is made; ask for another restore or "
        "an import, or remove the world's data volume to start a new world, which also removes its installed server, "
        "so its next start downloads it again. Why: www"
    )


@pytest.mark.parametrize("record", ["{", "[]", json.dumps({"id": RESTORE_ID, "outcome": "unknown", "reason": None})])
def test_a_world_with_no_save_whose_restore_record_cannot_be_read_is_held(world: World, record: str) -> None:
    place_of(world).vhd.mkdir(parents=True)
    place_of(world).done.write_text(record)
    world.generation()
    sup = world.supervisor()
    world.stop_at(30, sup)

    assert sup.run() == 0
    assert world.launches == [] and world.states() == ["held"]
    assert held_without_save(world)[-1].startswith(
        "the world has no save and the record of its newest restore, restore-done.json in its data volume, "
        "cannot be read, so no new world is made; ask for another restore or an import"
    )


def test_a_world_with_no_save_whose_data_volume_cannot_be_read_is_held(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = place_of(world).done
    lstat = os.lstat

    def unreadable(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if Path(path) == done:
            raise OSError(errno.EIO, "Input/output error")
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", unreadable)
    world.generation()
    sup = world.supervisor()
    world.stop_at(30, sup)

    assert sup.run() == 0
    assert world.launches == [] and world.states() == ["held"]
    assert held_without_save(world)[-1].startswith(
        "the world has no save and the record of its newest restore, restore-done.json in its data volume, "
        "cannot be read, so no new world is made"
    )


def test_a_world_with_a_pre_1_0_save_is_not_held_after_a_failed_restore(world: World) -> None:
    (world.paths.saves / "worlds_local").mkdir(parents=True)
    (world.paths.saves / "worlds_local" / "Meadowfold.db").write_bytes(b"pre-1.0")
    world.generation(restore="20261118T100000Z")
    sup = world.supervisor()
    world.plugin_running(40)
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.restores() == ["failed"]
    assert len(world.launches) == 1 and not held_without_save(world)


def test_a_world_whose_save_folder_is_a_file_is_held_after_a_failed_restore(world: World) -> None:
    (world.paths.saves / "worlds_local").mkdir(parents=True)
    (world.paths.saves / "worlds_local" / "Meadowfold").write_bytes(b"no save folder")
    world.generation(restore="20261118T100000Z")
    sup = world.supervisor()
    world.stop_at(60, sup)

    assert sup.run() == 0
    assert world.restores()[-1] == "failed"
    assert world.launches == [] and held_without_save(world)


def test_a_new_world_with_no_restore_is_not_held(world: World) -> None:
    world.generation()
    sup = world.supervisor()
    world.plugin_running(0)
    world.stop_at(60, sup)
    assert sup.run() == 0
    # It has no record of a restore: a missing record holds nothing.
    assert not place_of(world).done.exists()
    assert len(world.launches) == 1 and not held_without_save(world)


NEWER = "26266951"


def test_a_running_world_checks_steam_on_its_policys_interval_and_finds_nothing_newer(world: World) -> None:
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(10, players=lambda: 2)
    world.stop_at(2000, sup)

    assert sup.run() == 0
    assert [round(t) for t in world.checks] == [900, 1800]
    assert world.updates() == [None]


def test_no_check_runs_while_the_world_loads(world: World) -> None:
    world.generation(1, update_every_s=300)
    sup = world.supervisor()
    world.plugin_running(10, start=600)
    world.stop_at(700, sup)

    assert sup.run() == 0
    assert len(world.checks) == 1
    assert world.checks[0] >= 600


def test_a_newer_build_restarts_an_empty_world_after_a_save_it_backs_up(world: World) -> None:
    world.generation(1)
    world.public = NEWER
    sup = world.supervisor()
    world.plugin_running(10)
    world.time.at(100, lambda: world.game_saves(1))
    world.process.on_interrupt = lambda: world.game_saves(2)

    assert sup.run() == 0
    assert world.process.signals == [signal.SIGINT]
    assert 900 <= world.time.now < 910
    assert world.backed_up() == [1, 2]
    assert world.reports[-1]["game_build"] == "25909431"
    assert world.reports[-1]["update"] == {"build": NEWER}


def test_a_newer_build_waits_while_players_are_online(world: World) -> None:
    world.generation(1)
    world.public = NEWER
    online = [2]
    sup = world.supervisor()
    world.plugin_running(10, players=lambda: online[0])
    world.time.at(1500, lambda: online.__setitem__(0, 0))

    assert sup.run() == 0
    assert world.process.signals == [signal.SIGINT]
    assert 1500 <= world.time.now < 1510
    assert any(r.get("update") == {"build": NEWER} and r.get("players") == 2 for r in world.reports)


def test_a_generations_confirmation_never_updates_a_world_with_players_online(world: World) -> None:
    world.generation(1, disconnect=True)
    world.public = NEWER
    sup = world.supervisor()
    world.plugin_running(10, players=lambda: 2)
    world.stop_at(1500, sup)

    assert sup.run() == 0
    assert world.time.now >= 1500


def test_under_at_start_a_newer_build_is_named_and_waits_for_the_next_start(world: World) -> None:
    world.generation(1, policy="at-start")
    world.public = NEWER
    sup = world.supervisor()
    world.plugin_running(10)
    world.stop_at(2500, sup)

    assert sup.run() == 0
    assert world.time.now >= 2500
    assert world.updates() == [None, {"build": NEWER}]


def test_a_policy_changed_to_when_empty_restarts_a_world_a_build_waits_for(world: World) -> None:
    world.generation(1, policy="at-start")
    world.public = NEWER
    sup = world.supervisor()
    world.plugin_running(10)
    world.time.at(1200, lambda: world.generation(2, policy="when-empty"))

    assert sup.run() == 0
    assert world.process.signals == [signal.SIGINT]
    assert 1200 <= world.time.now < 1210


def test_an_unverified_world_never_restarts_for_a_build_with_its_players_unknown(world: World) -> None:
    world.generation(1)
    world.public = NEWER
    sup = world.supervisor()
    world.stop_at(2500, sup)

    assert sup.run() == 0
    assert world.time.now >= 2500
    assert world.states() == ["installing", "loading", "unverified"]
    assert world.reports[-1]["update"] == {"build": NEWER}


def test_a_failed_check_says_why_and_keeps_the_build_found_before_until_one_succeeds(world: World) -> None:
    world.generation(1, policy="at-start", update_every_s=300)
    world.public = NEWER
    sup = world.supervisor()
    world.plugin_running(10)
    world.time.at(400, lambda: setattr(world, "public", None))
    # Steam takes the newer build back: the installed one is the public one again.
    world.time.at(700, lambda: setattr(world, "public", "25909431"))
    world.stop_at(1000, sup)

    assert sup.run() == 0
    failed = "SteamCMD named no public game build: FAILED (No Connection)"
    assert world.updates() == [None, {"build": NEWER}, {"build": NEWER, "failure": failed}, None]


def test_a_check_is_told_to_stop_once_the_watch_ends_for_a_restart(world: World) -> None:
    world.generation(1, update_every_s=300)
    sup = world.supervisor()
    world.plugin_running(10)
    during: list[bool] = []
    world.time.at(400, lambda: during.append(world.check_stopping[0]()))
    world.time.at(500, lambda: world.generation(2, password="hearth-and-horn"))

    assert sup.run() == 0
    assert during == [False]
    assert world.check_stopping[0]()


def test_the_app_info_cache_is_in_steamcmds_steam_folder_in_its_home_the_data_volume() -> None:
    assert Paths().app_info_cache == Path("/data/Steam/appcache/appinfo.vdf")


def missed(build: str, attempts: int, at: datetime) -> dict[str, object]:
    """A record of a restart to update, as the runtime writes it."""
    return {"build": build, "attempts": attempts, "at": at.strftime("%Y-%m-%dT%H:%M:%SZ")}


def test_a_restart_to_update_records_the_build_it_is_for(world: World) -> None:
    world.generation(1)
    world.public = NEWER
    sup = world.supervisor()
    world.plugin_running(10)

    assert sup.run() == 0
    taken = json.loads(world.paths.update_record.read_text())
    assert taken == missed(NEWER, 1, START + timedelta(seconds=world.time.now // 1))


def test_a_build_the_last_restart_did_not_install_is_restarted_for_again_after_its_wait(world: World) -> None:
    world.paths.update_record.parent.mkdir(parents=True)
    world.paths.update_record.write_text(json.dumps(missed(NEWER, 1, START - timedelta(minutes=40))))
    world.generation(1, update_every_s=300)
    world.public = NEWER
    sup = world.supervisor()
    world.plugin_running(10)

    assert sup.run() == 0
    assert 1200 <= world.time.now < 1210
    assert json.loads(world.paths.update_record.read_text())["attempts"] == 2
    waits = (
        f"the restart to update at 2026-11-18 19:20 UTC did not install game build {NEWER}; the world tries again "
        "once empty after 2026-11-18 20:20 UTC"
    )
    assert world.updates() == [None, {"build": NEWER, "failure": waits}]


def test_each_restart_that_misses_the_build_doubles_the_wait_up_to_a_day() -> None:
    waits = [
        (supervisor.Missed(NEWER, attempts, START).next_at() - START).total_seconds() / 3600 for attempts in range(1, 8)
    ]
    assert waits == [1, 2, 4, 8, 16, 24, 24]


def test_a_build_after_the_one_missed_is_restarted_for_at_once(world: World) -> None:
    world.paths.update_record.parent.mkdir(parents=True)
    world.paths.update_record.write_text(json.dumps(missed(NEWER, 3, START)))
    world.generation(1)
    world.public = "26300000"
    sup = world.supervisor()
    world.plugin_running(10)

    assert sup.run() == 0
    assert 900 <= world.time.now < 910
    assert json.loads(world.paths.update_record.read_text())["attempts"] == 1


def test_a_start_that_installed_the_build_forgets_the_record(world: World) -> None:
    world.paths.update_record.parent.mkdir(parents=True)
    world.paths.update_record.write_text(json.dumps(missed("25909431", 1, START)))
    world.generation(1)
    sup = world.supervisor()
    world.plugin_running(10)
    world.stop_at(100, sup)

    assert sup.run() == 0
    assert not world.paths.update_record.exists()


def test_steam_going_back_to_the_installed_build_forgets_the_record(world: World) -> None:
    world.paths.update_record.parent.mkdir(parents=True)
    world.paths.update_record.write_text(json.dumps(missed(NEWER, 1, START)))
    world.generation(1, policy="at-start")
    sup = world.supervisor()
    world.plugin_running(10)
    world.stop_at(1000, sup)

    assert sup.run() == 0
    assert not world.paths.update_record.exists()


def test_under_at_start_a_missed_build_waits_for_the_next_start(world: World) -> None:
    world.paths.update_record.parent.mkdir(parents=True)
    world.paths.update_record.write_text(json.dumps(missed(NEWER, 1, START)))
    world.generation(1, policy="at-start")
    world.public = NEWER
    sup = world.supervisor()
    world.plugin_running(10)
    world.stop_at(1000, sup)

    assert sup.run() == 0
    waits = (
        f"the restart to update at 2026-11-18 20:00 UTC did not install game build {NEWER}; the world takes it at "
        "its next start"
    )
    assert world.updates() == [None, {"build": NEWER, "failure": waits}]


def test_a_record_time_ahead_of_the_clock_delays_the_next_restart_no_more_than_the_wait(world: World) -> None:
    world.paths.update_record.parent.mkdir(parents=True)
    world.paths.update_record.write_text(json.dumps(missed(NEWER, 1, START + timedelta(days=30))))
    world.generation(1, update_every_s=900)
    world.public = NEWER
    sup = world.supervisor()
    world.plugin_running(10)
    # The plugin's reports end at 3030 s; keep the world counted empty until the wait has passed.
    for when in range(3030, 4000, 5):
        world.time.at(when, lambda: world.plugin_reports(10))

    assert sup.run() == 0
    assert 3600 <= world.time.now < 3610


def test_a_restart_to_update_that_cannot_be_recorded_is_not_made(world: World) -> None:
    world.paths.update_record.mkdir(parents=True)
    world.generation(1)
    world.public = NEWER
    sup = world.supervisor()
    world.plugin_running(10)
    world.stop_at(1500, sup)

    assert sup.run() == 0
    assert world.time.now >= 1500
    assert world.process.signals == [signal.SIGINT]
    update = cast(dict[str, str], world.reports[-1]["update"])
    assert update["build"] == NEWER
    assert update["failure"].startswith("cannot record a restart to update in the data volume")
    assert not world.paths.update_record.with_suffix(".tmp").exists()


def test_a_record_that_could_not_be_written_is_tried_again_after_the_next_check(world: World) -> None:
    world.paths.update_record.mkdir(parents=True)
    world.generation(1)
    world.public = NEWER
    sup = world.supervisor()
    world.plugin_running(10)
    world.time.at(1000, world.paths.update_record.rmdir)

    assert sup.run() == 0
    assert 1800 <= world.time.now < 1810
    assert json.loads(world.paths.update_record.read_text())["build"] == NEWER


def test_a_check_that_raises_unexpectedly_says_why_and_the_world_runs_on(world: World) -> None:
    world.generation(1, update_every_s=300)
    sup = world.supervisor()

    def query(command: list[str], timeout: float, stopping: steam.Stopping) -> str:
        raise RuntimeError("no room for the answer")

    sup.query_steamcmd = query
    world.plugin_running(10)
    world.stop_at(1000, sup)

    assert sup.run() == 0
    assert world.time.now >= 1000
    failed = "the check stopped on an unexpected error: RuntimeError: no room for the answer"
    assert world.updates() == [None, {"failure": failed}]


def test_a_stop_ends_a_check_under_way_before_the_game_has_stopped(world: World) -> None:
    world.generation(1, update_every_s=300)
    sup = world.supervisor()
    sup.checker = ThreadPoolExecutor(max_workers=1)
    asked, stopped = threading.Event(), threading.Event()
    seen_asked: list[bool] = []

    def query(command: list[str], timeout: float, stopping: steam.Stopping) -> str:
        asked.set()
        deadline = time.monotonic() + 10
        while not stopping() and time.monotonic() < deadline:
            time.sleep(0.01)
        if stopping():
            stopped.set()
        raise steam.Stopped("stop requested while SteamCMD ran")

    sup.query_steamcmd = query
    world.plugin_running(10)
    # The loop keeps watching the world while the check is under way.
    world.time.at(305, lambda: seen_asked.append(asked.wait(10)))
    world.stop_at(400, sup)
    # The game's stop comes before the watch ends, so only the stop request can have ended the check by then.
    seen_at_stop: list[bool] = []
    world.process.on_interrupt = lambda: seen_at_stop.append(stopped.wait(10))

    assert sup.run() == 0
    sup.checker.shutdown(wait=True)
    assert world.time.now >= 400
    assert seen_asked == [True]
    assert seen_at_stop == [True]
    assert world.updates() == [None]
