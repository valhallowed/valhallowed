import io
import json
from pathlib import Path
from typing import Any

import pytest

from vhd_control import client, layout
from vhd_control.apply import Apply, Refused
from vhd_control.naming import child_id
from vhd_control.render import STOP_GRACE
from vhd_control.store import Store
from vhd_control.test_daemon import SCHEMAS, TWO_WORLDS

IMAGES = {
    "control-plane": "ghcr.io/valhallowed/control-plane:0.1.0@sha256:" + "a" * 64,
    "game-runtime": "ghcr.io/valhallowed/game-runtime:0.1.0@sha256:" + "b" * 64,
}
MEADOWFOLD = child_id("Meadowfold")
ASHWAKE = child_id("Ashwake")
PASSWORD = 'schema = 1\n\n[world.Meadowfold]\npassword = "hearth-and-horn"\n'


class FakeDocker:
    def __init__(self) -> None:
        self.api = "1.55"
        self.running: list[str] = []
        self.mounts = {"/install": "/srv/vhd", "/var/run/docker.sock": "/var/run/docker.sock"}
        self.streamed: list[list[str]] = []

    def output(self, args: list[str]) -> str:
        if args[0] == "version":
            return self.api + "\n"
        if args[0] == "inspect":
            mounts = [{"Destination": d, "Source": s} for d, s in self.mounts.items()]
            return json.dumps([{"Mounts": mounts, "Config": {"Labels": {"com.docker.compose.project": "valhallowed"}}}])
        if args[:2] == ["compose", "-f"]:
            return json.dumps({"name": "valhallowed", "x-vhd": {"images": IMAGES}})
        if args[0] == "ps":
            return "".join(f"{service}\n" for service in self.running)
        raise AssertionError(f"unexpected docker {args}")

    def stream(self, args: list[str]) -> None:
        self.streamed.append(args)


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
            store=Store(self.volume),
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

    def handed(self, cid: str) -> tuple[int, str | None]:
        document = json.loads((self.volume / cid / "generation.json").read_text())
        secrets = json.loads((self.volume / cid / "secrets.json").read_text())
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
            "compose", "-p", "valhallowed", "-f", str(setup.install / layout.RENDERED),
            "up", "--detach", "--wait", "--remove-orphans",
        ]
    ]  # fmt: skip


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
        "read_only": True, "volume": {"subpath": MEADOWFOLD},
    }  # fmt: skip
    control_plane = services["control-plane"]
    owner = setup.install.stat()
    assert control_plane["user"] == f"{owner.st_uid}:{owner.st_gid}"
    assert {"type": "bind", "source": "/srv/vhd", "target": "/install", "read_only": True} in control_plane["volumes"]
    assert "ports" not in control_plane
    assert services["vhd"]["profiles"] == ["tools"]


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

    with pytest.raises(Refused, match="restarts Meadowfold, which may have players online"):
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
    assert "restarting Meadowfold with players possibly online" in setup.out.getvalue()


def test_a_restart_of_an_empty_world_needs_no_confirmation(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 0)
    (setup.install / "secrets.toml").write_text(PASSWORD)
    setup.apply()
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
