import fcntl
import io
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from vhd_control import client, layout
from vhd_control.apply import WAIT_TIMEOUT_S, Apply, Refused
from vhd_control.docker import DockerError
from vhd_control.manifest import load
from vhd_control.reconcile import reconcile
from vhd_control.render import STOP_GRACE
from vhd_control.store import Store
from vhd_control.test_daemon import ASHWAKE, MEADOWFOLD, PASSWORD, SCHEMAS, TWO_WORLDS

IMAGES = {
    "control-plane": "ghcr.io/valhallowed/control-plane:0.1.0@sha256:" + "a" * 64,
    "game-runtime": "ghcr.io/valhallowed/game-runtime:0.1.0@sha256:" + "b" * 64,
    "skeleton": "ghcr.io/valhallowed/skeleton:0.1.0@sha256:" + "e" * 64,
}


class FakeDocker:
    def __init__(self) -> None:
        self.api = "1.55"
        self.running: list[str] = []
        self.mounts = {"/install": "/srv/vhd", "/var/run/docker.sock": "/var/run/docker.sock"}
        self.streamed: list[list[str]] = []
        # Images the host lacks.
        self.missing: set[str] = set()
        # How many coming Compose runs fail, and whether the next is interrupted.
        self.fail_up = 0
        self.interrupt = False
        self.images = dict(IMAGES)
        # Called with each streamed command, before it runs.
        self.hook: Callable[[list[str]], None] = lambda args: None

    def output(self, args: list[str]) -> str:
        if args[0] == "version":
            return self.api + "\n"
        if args[0] == "inspect":
            mounts = [{"Destination": d, "Source": s} for d, s in self.mounts.items()]
            return json.dumps([{"Mounts": mounts, "Config": {"Labels": {"com.docker.compose.project": "valhallowed"}}}])
        if args[:2] == ["compose", "-f"]:
            return json.dumps({"name": "valhallowed", "x-vhd": {"images": self.images}})
        if args[0] == "ps":
            return "".join(f"{service}\n" for service in self.running)
        if args[:2] == ["image", "inspect"]:
            if args[-1] in self.missing:
                raise DockerError(f"No such image: {args[-1]}")
            return "sha256:" + "c" * 64 + "\n"
        raise AssertionError(f"unexpected docker {args}")

    def stream(self, args: list[str]) -> None:
        self.hook(args)
        self.streamed.append(args)
        if args[0] == "pull":
            self.missing.discard(args[1])
            return
        if self.interrupt:
            self.interrupt = False
            raise KeyboardInterrupt
        if self.fail_up > 0:
            self.fail_up -= 1
            raise DockerError("docker compose -p exited with 1")


class Setup:
    def __init__(self, root: Path) -> None:
        self.install = root / "install"
        self.volume = root / "generations"
        self.control = root / "control"
        for directory in (self.install, self.volume, self.control):
            directory.mkdir()
        (self.install / "manifest.toml").write_text(TWO_WORLDS)
        self.docker = FakeDocker()
        self.worlds: dict[str, Any] | None = {}
        self.out = io.StringIO()

    def status(self) -> dict[str, Any]:
        if self.worlds is None:
            raise client.Unreachable("the control plane does not answer")
        return {"contract": 1, "generation": 1, "worlds": self.worlds}

    def players(self, world: str, players: int) -> None:
        assert self.worlds is not None
        report = {"contract": 1, "generation": 1, "world": world, "state": "running", "players": players}
        self.worlds[world] = {"published": 1, "state": "running", "report": report, "report_age_s": 1}

    def apply(self, disconnect_players: bool = False) -> None:
        Apply(
            install=self.install,
            generations=self.volume,
            control_dir=self.control,
            schemas=SCHEMAS,
            docker=self.docker,
            status=self.status,
            hostname="c0ffee",
            out=self.out,
        ).run(disconnect_players)

    def rendered(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads((self.install / layout.RENDERED).read_text())
        return document

    def document(self, cid: str) -> dict[str, Any]:
        applied = Store(self.volume).load().applied
        assert applied is not None
        directory = Store(self.volume).directory(cid, applied[cid])
        document: dict[str, Any] = json.loads((directory / "generation.json").read_text())
        return document

    def handed(self, cid: str, port: int | None = None) -> tuple[int, str | None]:
        """What a world's container reads: by default, the documents for the port it was applied on."""
        if port is None:
            applied = Store(self.volume).load().applied
            assert applied is not None
            port = applied[cid]
        directory = Store(self.volume).directory(cid, port)
        document = json.loads((directory / "generation.json").read_text())
        secrets = json.loads((directory / "secrets.json").read_text())
        return document["generation"], secrets.get("password")


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


def test_a_first_apply_hands_every_world_and_starts_the_install(setup: Setup) -> None:
    setup.apply()

    assert setup.handed(MEADOWFOLD) == (1, None)
    assert setup.handed(ASHWAKE) == (1, None)
    assert Store(setup.volume).load().applied == {MEADOWFOLD: 2456, ASHWAKE: 2459}
    assert setup.docker.streamed == [
        [
            "compose", "-p", "valhallowed", "-f", str(setup.install / layout.NEXT),
            "up", "--detach", "--wait", "--wait-timeout", str(WAIT_TIMEOUT_S), "--remove-orphans",
        ]
    ]  # fmt: skip
    assert (setup.install / layout.RENDERED).exists()
    assert not (setup.install / layout.NEXT).exists()


def test_images_the_host_lacks_are_pulled_before_anything_restarts(setup: Setup) -> None:
    setup.docker.missing = {IMAGES["game-runtime"]}
    setup.apply()
    assert setup.docker.streamed[0] == ["pull", IMAGES["game-runtime"]]
    assert setup.docker.streamed[1][:2] == ["compose", "-p"]


def test_an_interrupted_apply_still_asks_before_the_next_restart(setup: Setup) -> None:
    setup.apply()
    before = setup.rendered()
    (setup.install / "manifest.toml").write_text(TWO_WORLDS.replace("2456", "2462"))
    setup.docker.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        setup.apply()
    assert setup.rendered() == before

    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 2)
    with pytest.raises(Refused, match="restarts Meadowfold"):
        setup.apply()


def test_the_rendered_install_mounts_each_world_only_its_own_generation(setup: Setup) -> None:
    setup.apply()
    services = setup.rendered()["services"]

    world = services[MEADOWFOLD]
    assert world["image"] == IMAGES["game-runtime"]
    assert world["ports"] == ["2456-2458:2456-2458/udp"]
    assert world["stop_grace_period"] == STOP_GRACE
    generation = next(v for v in world["volumes"] if v["target"] == "/generation")
    assert generation == {
        "type": "volume", "source": "generations", "target": "/generation",
        "read_only": True, "volume": {"subpath": f"{MEADOWFOLD}/2456"},
    }  # fmt: skip
    control_plane = services["control-plane"]
    owner = setup.install.stat()
    assert control_plane["user"] == f"{owner.st_uid}:{owner.st_gid}"
    assert {"type": "bind", "source": "/srv/vhd", "target": "/install", "read_only": True} in control_plane["volumes"]
    assert "ports" not in control_plane
    assert services["vhd"] == {"image": IMAGES["control-plane"], "profiles": ["tools"]}
    assert services["skeleton"]["ports"] == ["8456:8456"]


def test_the_skeleton_takes_the_web_maps_port_from_the_manifest(setup: Setup) -> None:
    (setup.install / "manifest.toml").write_text(TWO_WORLDS + "\n[web_map]\nport = 9100\n")
    setup.apply()
    assert setup.rendered()["services"]["skeleton"]["ports"] == ["9100:8456"]


def test_an_unchanged_apply_restarts_nothing_and_asks_nothing(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE, "control-plane"]
    setup.players("Meadowfold", 2)
    setup.apply()
    assert len(setup.docker.streamed) == 2


def test_a_restart_of_a_world_with_players_is_refused(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 2)
    (setup.install / "secrets.toml").write_text(PASSWORD)

    with pytest.raises(Refused, match=r"restarts Meadowfold \(2 player\(s\)\), where players may be online"):
        setup.apply()
    assert setup.handed(MEADOWFOLD) == (1, None)
    assert len(setup.docker.streamed) == 1


def test_disconnect_players_restarts_it_and_says_so(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 2)
    (setup.install / "secrets.toml").write_text(PASSWORD)

    setup.apply(disconnect_players=True)
    assert setup.handed(MEADOWFOLD) == (2, "hearth-and-horn")
    assert "restarting Meadowfold (2 player(s)), as asked" in setup.out.getvalue()
    assert setup.document(MEADOWFOLD)["disconnect_players"] is True
    assert Store(setup.volume).load().override == {"generation": 2, "worlds": {"Meadowfold": 2}}


def test_a_restart_of_an_empty_world_needs_no_confirmation(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 0)
    (setup.install / "secrets.toml").write_text(PASSWORD)
    setup.apply()
    assert setup.handed(MEADOWFOLD) == (2, "hearth-and-horn")
    assert "disconnect_players" not in setup.document(MEADOWFOLD)


def test_a_world_whose_container_and_documents_change_is_stopped_before_its_documents(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 0)
    setup.players("Ashwake", 0)
    setup.docker.images["game-runtime"] = "ghcr.io/valhallowed/game-runtime:0.2.0@sha256:" + "d" * 64
    (setup.install / "secrets.toml").write_text(PASSWORD)
    at_stop: list[tuple[int, str | None]] = []
    setup.docker.hook = lambda args: at_stop.append(setup.handed(MEADOWFOLD)) if "stop" in args else None

    setup.apply()

    stops = [args for args in setup.docker.streamed if "stop" in args]
    assert stops == [["compose", "-p", "valhallowed", "-f", str(setup.install / layout.RENDERED), "stop", MEADOWFOLD]]
    assert at_stop == [(1, None)]
    assert setup.handed(MEADOWFOLD) == (2, "hearth-and-horn")


def test_without_the_control_plane_a_running_world_counts_as_occupied(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD]
    setup.worlds = None
    (setup.install / "manifest.toml").write_text(TWO_WORLDS.replace("2456", "2462"))
    with pytest.raises(Refused, match="Meadowfold"):
        setup.apply()


def test_removing_a_world_with_players_is_refused(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Ashwake", 1)
    (setup.install / "manifest.toml").write_text("schema = 1\n\n[world.Meadowfold]\n")
    with pytest.raises(Refused, match="Ashwake"):
        setup.apply()


def test_an_engine_without_volume_subpaths_is_refused(setup: Setup) -> None:
    setup.docker.api = "1.44"
    with pytest.raises(Refused, match="Docker Engine 26"):
        setup.apply()


def test_vhd_must_run_through_compose_from_the_install_folder(setup: Setup) -> None:
    del setup.docker.mounts["/install"]
    with pytest.raises(Refused, match="docker compose run --rm vhd apply"):
        setup.apply()


def test_a_port_change_compose_cannot_apply_is_rolled_back(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 0)
    before = setup.rendered()
    (setup.install / "manifest.toml").write_text(TWO_WORLDS.replace("2456", "2462"))
    setup.docker.fail_up = 1

    with pytest.raises(Refused, match="back as it ran before"):
        setup.apply()

    assert setup.rendered() == before
    assert len(setup.docker.streamed) == 3
    state = Store(setup.volume).load()
    assert state.applied is not None
    assert state.applied[MEADOWFOLD] == 2456
    assert state.handed[MEADOWFOLD].number == 1
    assert setup.handed(MEADOWFOLD, port=2456) == (1, None)
    assert not (setup.volume / MEADOWFOLD / "2462").exists()


def test_a_port_change_moves_the_world_and_clears_its_old_directory(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 0)
    (setup.install / "manifest.toml").write_text(TWO_WORLDS.replace("2456", "2462"))

    setup.apply()

    state = Store(setup.volume).load()
    assert state.applied == {MEADOWFOLD: 2462, ASHWAKE: 2459}
    assert state.handed[MEADOWFOLD].number == 2
    assert [p.name for p in (setup.volume / MEADOWFOLD).iterdir()] == ["2462"]


def test_a_removed_world_loses_its_directory_once_compose_removes_it(setup: Setup) -> None:
    setup.apply()
    (setup.install / "manifest.toml").write_text("schema = 1\n\n[world.Meadowfold]\n")

    setup.apply()

    assert not (setup.volume / ASHWAKE).exists()
    assert ASHWAKE not in Store(setup.volume).load().handed


def test_a_directory_vhd_never_handed_is_left_alone(setup: Setup) -> None:
    (setup.volume / "intake").mkdir()
    setup.apply()
    setup.apply()
    assert (setup.volume / "intake").is_dir()


def test_every_service_on_the_install_network_runs_without_capabilities(setup: Setup) -> None:
    """The status listener's sender check needs it on the install network: no child may forge a
    source address. Every other service runs as locked down, and logs to a rotating driver."""
    setup.apply()
    services = setup.rendered()["services"]
    on_network = [name for name, service in services.items() if layout.INSTALL_NETWORK in service.get("networks", [])]
    assert len(on_network) == 3
    for name in [*on_network, "skeleton"]:
        assert services[name]["cap_drop"] == ["ALL"], name
        assert services[name]["security_opt"] == ["no-new-privileges:true"], name
        assert services[name]["logging"] == {"driver": "local"}, name


def test_a_world_whose_directory_went_missing_gets_it_back(setup: Setup) -> None:
    setup.apply()
    shutil.rmtree(setup.volume / MEADOWFOLD)

    setup.apply()

    assert setup.handed(MEADOWFOLD) == (1, None)


def test_a_failed_rollback_says_both_failed(setup: Setup) -> None:
    setup.apply()
    (setup.install / "manifest.toml").write_text(TWO_WORLDS.replace("2456", "2462"))
    setup.docker.fail_up = 2
    with pytest.raises(DockerError, match="bringing back the previous install failed too"):
        setup.apply()


def test_a_first_apply_that_fails_has_nothing_to_roll_back_to(setup: Setup) -> None:
    setup.docker.fail_up = 1
    with pytest.raises(DockerError, match="exited with 1"):
        setup.apply()
    assert len(setup.docker.streamed) == 1


def test_a_failed_compose_run_gives_same_port_worlds_back_their_documents(setup: Setup) -> None:
    """A runtime from the previous release may refuse the new documents, so it gets the old ones."""
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 0)
    setup.players("Ashwake", 0)
    setup.docker.images["game-runtime"] = "ghcr.io/valhallowed/game-runtime:0.2.0@sha256:" + "d" * 64
    (setup.install / "secrets.toml").write_text(PASSWORD)
    setup.docker.fail_up = 1

    with pytest.raises(Refused, match="back as it ran before"):
        setup.apply()

    assert setup.handed(MEADOWFOLD) == (1, None)
    state = Store(setup.volume).load()
    assert state.handed[MEADOWFOLD].number == 1
    assert state.generation is not None and state.generation.number == 1


def test_a_failure_after_worlds_stopped_brings_the_install_back(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 0)
    setup.players("Ashwake", 0)
    setup.docker.images["game-runtime"] = "ghcr.io/valhallowed/game-runtime:0.2.0@sha256:" + "d" * 64
    (setup.install / "secrets.toml").write_text(PASSWORD)
    publish = Store.publish
    calls: list[int] = []

    def full_disk(store: Store, *args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        publish(store, *args, **kwargs)

    monkeypatch.setattr(Store, "publish", full_disk)
    with pytest.raises(Refused, match="No space left on device"):
        setup.apply()

    assert any("stop" in args for args in setup.docker.streamed)
    assert setup.docker.streamed[-1][4] == str(setup.install / layout.RENDERED)
    assert setup.handed(MEADOWFOLD) == (1, None)
    assert not (setup.install / layout.NEXT).exists()


def test_disconnect_players_frees_a_runtime_waiting_for_an_empty_world(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    # The control plane handed generation 2 while the world looked empty; then a player joined,
    # and the runtime runs generation 1 still, waiting.
    (setup.install / "secrets.toml").write_text(PASSWORD)
    store = Store(setup.volume)
    with store.locked():
        state = store.load()
        generation = reconcile(load(setup.install, SCHEMAS), state.generation)
        store.hand(state, MEADOWFOLD, generation.children[MEADOWFOLD], generation.number)
        state.generation = generation
        store.save(state)
    setup.players("Meadowfold", 2)

    with pytest.raises(Refused, match=r"restarts Meadowfold \(2 player\(s\)\)"):
        setup.apply()
    setup.apply(disconnect_players=True)

    assert setup.document(MEADOWFOLD)["disconnect_players"] is True
    assert setup.handed(MEADOWFOLD) == (2, "hearth-and-horn")


def test_a_second_apply_while_one_runs_is_refused(setup: Setup) -> None:
    with (setup.volume / ".apply.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(Refused, match="another vhd apply is running"):
            setup.apply()


def test_removing_an_empty_world_needs_no_confirmation(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Ashwake", 0)
    (setup.install / "manifest.toml").write_text("schema = 1\n\n[world.Meadowfold]\n")
    setup.apply()
    assert ASHWAKE not in Store(setup.volume).load().handed
