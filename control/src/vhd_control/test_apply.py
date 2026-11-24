import fcntl
import io
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from vhd_control import client, layout
from vhd_control.apply import MAKE_BACKUP_FOLDERS, WAIT_TIMEOUT_S, Apply, Refused
from vhd_control.docker import DockerError
from vhd_control.manifest import load
from vhd_control.reconcile import Restore, reconcile
from vhd_control.render import STOP_GRACE
from vhd_control.store import Store
from vhd_control.test_daemon import ASHWAKE, MEADOWFOLD, PASSWORD, SCHEMAS, TWO_WORLDS

IMAGES = {
    "control-plane": "ghcr.io/valhallowed/control-plane:0.1.0@sha256:" + "a" * 64,
    "game-runtime": "ghcr.io/valhallowed/game-runtime:0.1.0@sha256:" + "b" * 64,
    "web-map": "ghcr.io/valhallowed/web-map:0.1.0@sha256:" + "e" * 64,
    "data-layer": "ghcr.io/valhallowed/data-layer:0.1.0@sha256:" + "f" * 64,
}


class FakeDocker:
    def __init__(self) -> None:
        self.api = "1.55"
        self.running: list[str] = []
        self.mounts = {"/install": "/srv/vhd", "/var/run/docker.sock": "/var/run/docker.sock"}
        self.streamed: list[list[str]] = []
        # The volume and one-off container commands that prepare each world's map export directory
        # and backup folder, and whether a one-off container fails: any, or the backup folders'.
        self.prepared: list[list[str]] = []
        # How many of them had run when each Compose command did.
        self.compose_after_prepared: list[int] = []
        self.fail_prepare = False
        self.fail_backups = False
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
        if args[0] in ("volume", "run"):
            if self.fail_prepare and args[0] == "run":
                raise DockerError("docker run: mkdir: Permission denied")
            if self.fail_backups and MAKE_BACKUP_FOLDERS in args:
                raise DockerError("docker run: exited with 1")
            self.prepared.append(args)
            return ""
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
        if args[0] == "compose":
            self.compose_after_prepared.append(len(self.prepared))
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
        return {"contract": 2, "generation": 1, "worlds": self.worlds, "services": {}}

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
    assert services["web-map"]["ports"] == ["8456:8456"]


def test_each_world_gets_its_map_export_directory_made_by_the_runtime_image_and_user(setup: Setup) -> None:
    setup.apply()

    # Each world's volume and directory, then one run for every world's backup folder.
    assert [args[0] for args in setup.docker.prepared] == ["volume", "run", "volume", "run", "run"]
    # Before Compose runs, which cannot mount a subpath that is not there.
    assert setup.docker.compose_after_prepared == [5]
    volumes = [args for args in setup.docker.prepared if args[0] == "volume"]
    assert volumes == [
        [
            "volume", "create", "--label=com.docker.compose.project=valhallowed",
            f"--label=com.docker.compose.volume={cid}-data", f"valhallowed_{cid}-data",
        ]
        for cid in (ASHWAKE, MEADOWFOLD)
    ]  # fmt: skip
    runs = [args for args in setup.docker.prepared if args[0] == "run" and "mkdir" in args]
    assert runs[0] == [
        # No --user: the image's own USER makes the directory, so its uid is written in the Dockerfile alone.
        "run", "--rm", "--pull", "never", "--network", "none", "--entrypoint", "mkdir",
        "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
        "--volume", f"valhallowed_{ASHWAKE}-data:/data",
        IMAGES["game-runtime"], "-p", "/data/map-export",
    ]  # fmt: skip
    assert len(runs) == 2


def test_a_map_export_directory_that_cannot_be_made_refuses_the_apply_before_anything_changes(setup: Setup) -> None:
    setup.docker.fail_prepare = True
    with pytest.raises(Refused, match=r"could not make the map export directory of world 'Ashwake'.*apply again"):
        setup.apply()

    assert setup.docker.streamed == []
    assert not (setup.install / layout.RENDERED).exists()
    assert not (setup.install / layout.NEXT).exists()
    assert Store(setup.volume).load().applied is None


def test_a_refused_players_check_makes_no_map_export_calls(setup: Setup) -> None:
    setup.apply()
    setup.docker.prepared.clear()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 2)
    (setup.install / "secrets.toml").write_text(PASSWORD)

    with pytest.raises(Refused, match="players may be online"):
        setup.apply()
    assert setup.docker.prepared == []


def test_a_removed_world_gets_no_volume_and_no_map_export_directory(setup: Setup) -> None:
    setup.apply()
    setup.docker.prepared.clear()
    (setup.install / "manifest.toml").write_text("schema = 1\n\n[world.Meadowfold]\n")
    setup.apply()

    assert [args[-1] for args in setup.docker.prepared if args[0] == "volume"] == [f"valhallowed_{MEADOWFOLD}-data"]
    assert all(ASHWAKE not in " ".join(args) for args in setup.docker.prepared)
    assert len([args for args in setup.docker.prepared if args[0] == "run"]) == 2


def test_one_run_makes_every_worlds_backup_folder_as_the_runtime_user(setup: Setup) -> None:
    setup.apply()

    (run,) = [args for args in setup.docker.prepared if MAKE_BACKUP_FOLDERS in args]
    assert run == [
        # Root with only what mkdir and chown take; the image's own user by name, so its uid is
        # written in the Dockerfile alone.
        "run", "--rm", "--pull", "never", "--network", "none", "--user", "0", "--entrypoint", "sh",
        "--cap-drop=ALL", "--cap-add=CHOWN", "--cap-add=DAC_OVERRIDE", "--security-opt=no-new-privileges:true",
        "--volume", "/srv/vhd/backups:/backups",
        IMAGES["game-runtime"], "-c", MAKE_BACKUP_FOLDERS, "sh", ASHWAKE, MEADOWFOLD,
    ]  # fmt: skip
    assert '/backups/$d"' in MAKE_BACKUP_FOLDERS and "chown vhd:vhd" in MAKE_BACKUP_FOLDERS


def test_each_world_mounts_only_its_own_backup_folder(setup: Setup) -> None:
    setup.apply()
    services = setup.rendered()["services"]
    for cid in (MEADOWFOLD, ASHWAKE):
        backups = [v for v in services[cid]["volumes"] if v["target"] == "/backups"]
        assert backups == [{"type": "bind", "source": f"/srv/vhd/backups/{cid}", "target": "/backups"}]
    assert all(
        v.get("source", "").startswith("/srv/vhd/backups") is False
        for name, service in services.items()
        if name not in (MEADOWFOLD, ASHWAKE)
        for v in service.get("volumes", [])
    )
    assert Store(setup.volume).load().backups_applied == "backups"


def test_a_backups_folder_the_manifest_names_is_mounted_as_named(setup: Setup) -> None:
    (setup.install / "manifest.toml").write_text(TWO_WORLDS + '\n[backups]\npath = "/mnt/disk/../vhd-backups"\n')
    setup.apply()

    backups = [v for v in setup.rendered()["services"][MEADOWFOLD]["volumes"] if v["target"] == "/backups"]
    assert backups[0]["source"] == f"/mnt/vhd-backups/{MEADOWFOLD}"
    assert "/mnt/vhd-backups:/backups" in next(args for args in setup.docker.prepared if MAKE_BACKUP_FOLDERS in args)


def test_a_backup_folder_that_cannot_be_made_refuses_the_apply_before_anything_changes(setup: Setup) -> None:
    setup.docker.fail_backups = True
    with pytest.raises(Refused, match=r"could not make the worlds' backup folders in /srv/vhd/backups.*apply again"):
        setup.apply()

    assert setup.docker.streamed == []
    assert Store(setup.volume).load().applied is None


def test_a_backups_change_restarts_no_world_and_asks_nothing(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 2)
    (setup.install / "manifest.toml").write_text(TWO_WORLDS + '\n[backups]\nkeep = 48\n\n[updates]\nevery = "1h"\n')
    setup.apply()

    assert "restarting" not in setup.out.getvalue()
    assert setup.document(MEADOWFOLD)["backups"]["keep"] == 48
    assert "disconnect_players" not in setup.document(MEADOWFOLD)


def test_a_new_backups_folder_restarts_the_worlds_with_players_only_when_confirmed(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 2)
    (setup.install / "manifest.toml").write_text(TWO_WORLDS + '\n[backups]\npath = "/srv/backups"\n')

    with pytest.raises(Refused, match="players may be online"):
        setup.apply()
    setup.apply(disconnect_players=True)
    assert Store(setup.volume).load().backups_applied == "/srv/backups"


def test_the_web_map_mounts_each_worlds_map_export_read_only(setup: Setup) -> None:
    setup.apply()
    web_map = setup.rendered()["services"]["web-map"]

    for cid in (MEADOWFOLD, ASHWAKE):
        assert {
            "type": "volume", "source": f"{cid}-data", "target": f"/map-export/{cid}",
            "read_only": True, "volume": {"subpath": "map-export"},
        } in web_map["volumes"]  # fmt: skip
    # Only the map export: the runtime's /data is never mounted whole.
    assert not any(v["target"] == "/data" for v in web_map["volumes"])
    generation = json.loads((setup.volume / "web-map" / "generation.json").read_text())
    assert SCHEMAS.problem("contracts/web-map.generation.schema.json", generation) is None
    assert {w["map_export"] for w in generation["worlds"]} == {f"/map-export/{MEADOWFOLD}", f"/map-export/{ASHWAKE}"}
    assert Store(setup.volume).load().mounts_applied["web-map"] == tuple(sorted([MEADOWFOLD, ASHWAKE]))


def test_the_data_layer_runs_on_the_install_network_alone_with_its_own_documents(setup: Setup) -> None:
    setup.apply()
    services = setup.rendered()["services"]
    for name in ("database", "intake"):
        service = services[name]
        assert service["image"] == IMAGES["data-layer"]
        assert service["command"] == [name]
        assert service["networks"] == [layout.INSTALL_NETWORK]
        assert "ports" not in service
        assert {
            "type": "volume", "source": "generations", "target": "/generation",
            "read_only": True, "volume": {"subpath": name},
        } in service["volumes"]  # fmt: skip
    assert {"type": "volume", "source": "database-data", "target": "/data"} in services["database"]["volumes"]
    assert "database-data" in setup.rendered()["volumes"]

    web_map = services["web-map"]
    assert web_map["image"] == IMAGES["web-map"]
    assert "command" not in web_map
    assert web_map["networks"] == [layout.INSTALL_NETWORK]
    assert {
        "type": "volume", "source": "generations", "target": "/generation",
        "read_only": True, "volume": {"subpath": "web-map"},
    } in web_map["volumes"]  # fmt: skip

    state = Store(setup.volume).load()
    assert state.services_applied == {"database": None, "intake": None, "web-map": 8456}
    for name in ("database", "intake", "web-map"):
        assert state.services[name].number == state.generation.number  # type: ignore[union-attr]
        secrets = json.loads((setup.volume / name / "secrets.json").read_text())
        assert SCHEMAS.problem(f"contracts/{name}.secrets.schema.json", secrets, secret=True) is None
    intake = json.loads((setup.volume / "intake" / "generation.json").read_text())
    assert set(intake["worlds"]) == {MEADOWFOLD, ASHWAKE}


def test_the_web_map_takes_its_port_from_the_manifest(setup: Setup) -> None:
    (setup.install / "manifest.toml").write_text(TWO_WORLDS + "\n[web_map]\nport = 9100\n")
    setup.apply()
    assert setup.rendered()["services"]["web-map"]["ports"] == ["9100:8456"]
    assert Store(setup.volume).load().services_applied["web-map"] == 9100  # type: ignore[index]


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


def _stopping(setup: Setup) -> list[int | None]:
    """Record the staged generation's number at each stop."""
    at_stop: list[int | None] = []

    def hook(args: list[str]) -> None:
        if "stop" in args:
            generation = Store(setup.volume).load().generation
            at_stop.append(None if generation is None else generation.number)

    setup.docker.hook = hook
    return at_stop


def test_a_recreated_control_plane_is_stopped_before_the_new_generation_is_staged(setup: Setup) -> None:
    """Otherwise the previous release's daemon would reconcile over the new generation with its own code."""
    setup.apply()
    setup.docker.running = ["control-plane"]
    setup.docker.images["control-plane"] = "ghcr.io/valhallowed/control-plane:0.2.0@sha256:" + "d" * 64
    (setup.install / "secrets.toml").write_text(PASSWORD)
    at_stop = _stopping(setup)

    setup.apply()

    stops = [args for args in setup.docker.streamed if "stop" in args]
    rendered = str(setup.install / layout.RENDERED)
    assert stops == [["compose", "-p", "valhallowed", "-f", rendered, "stop", layout.CONTROL_PLANE]]
    assert at_stop == [1]
    assert setup.docker.streamed[-1][5] == "up"
    generation = Store(setup.volume).load().generation
    assert generation is not None and generation.number == 2


def test_an_apply_that_changes_only_a_world_leaves_the_control_plane_running(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE, "control-plane"]
    setup.players("Meadowfold", 0)
    setup.players("Ashwake", 0)
    setup.docker.images["game-runtime"] = "ghcr.io/valhallowed/game-runtime:0.2.0@sha256:" + "d" * 64
    (setup.install / "secrets.toml").write_text(PASSWORD)

    setup.apply()

    stops = [args for args in setup.docker.streamed if "stop" in args]
    assert stops == [["compose", "-p", "valhallowed", "-f", str(setup.install / layout.RENDERED), "stop", MEADOWFOLD]]


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
    (setup.volume / "broker").mkdir()
    setup.apply()
    setup.apply()
    assert (setup.volume / "broker").is_dir()


def test_every_service_on_the_install_network_runs_without_capabilities(setup: Setup) -> None:
    """The status listener's sender check needs it on the install network: no child may forge a
    source address. Every other service runs as locked down, and logs to a rotating driver."""
    setup.apply()
    services = setup.rendered()["services"]
    on_network = [name for name, service in services.items() if layout.INSTALL_NETWORK in service.get("networks", [])]
    assert len(on_network) == 6
    for name in on_network:
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
    with pytest.raises(DockerError, match="bringing back the install as it ran before failed too"):
        setup.apply()


def test_a_first_apply_that_fails_removes_what_it_created(setup: Setup) -> None:
    """A container whose start failed keeps no network, and the next Compose run would start it as it is."""
    rendered: list[dict[str, Any]] = []
    setup.docker.hook = lambda args: rendered.append(json.loads(Path(args[4]).read_text())) if "rm" in args else None
    setup.docker.fail_up = 1

    with pytest.raises(Refused, match="containers it created are removed"):
        setup.apply()

    up, rm = setup.docker.streamed
    assert rm[:5] == up[:5]
    assert rm[5:] == ["rm", "--force", "--stop"]
    assert rendered and set(rendered[0]["services"]) >= {MEADOWFOLD, ASHWAKE}
    assert not (setup.install / layout.NEXT).exists()
    assert not (setup.install / layout.RENDERED).exists()
    state = Store(setup.volume).load()
    assert state.generation is None and state.handed == {} and state.services == {}
    assert not (setup.volume / MEADOWFOLD).exists()
    assert not (setup.volume / "intake").exists()


def test_a_first_apply_whose_cleanup_fails_says_both_failed(setup: Setup) -> None:
    setup.docker.fail_up = 2
    with pytest.raises(DockerError, match="removing the containers it created failed too"):
        setup.apply()


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


def test_a_failed_compose_run_stops_the_new_control_plane_before_restoring_the_state(setup: Setup) -> None:
    """Otherwise the new release's daemon would reconcile over the state the previous one wrote."""
    setup.apply()
    setup.docker.running = ["control-plane"]
    setup.docker.images["control-plane"] = "ghcr.io/valhallowed/control-plane:0.2.0@sha256:" + "d" * 64
    (setup.install / "secrets.toml").write_text(PASSWORD)
    at_stop = _stopping(setup)
    stopping = setup.docker.hook

    def fail_the_new_up(args: list[str]) -> None:
        stopping(args)
        if args[4:6] == [str(setup.install / layout.NEXT), "up"]:
            raise DockerError("docker compose -p exited with 1")

    setup.docker.hook = fail_the_new_up

    with pytest.raises(Refused, match="back as it ran before"):
        setup.apply()

    stops = [args for args in setup.docker.streamed if "stop" in args]
    rendered = str(setup.install / layout.NEXT)
    assert stops[-1] == ["compose", "-p", "valhallowed", "-f", rendered, "stop", layout.CONTROL_PLANE]
    assert at_stop == [1, 2]
    assert setup.docker.streamed[-1][4:6] == [str(setup.install / layout.RENDERED), "up"]
    generation = Store(setup.volume).load().generation
    assert generation is not None and generation.number == 1


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


@pytest.mark.parametrize("live", ["", "\n[backups]\nkeep = 48\n"])
def test_a_runtime_waiting_for_an_empty_world_still_needs_confirmation_after_a_live_change(
    setup: Setup, live: str
) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    # Generation 2, a new password, was handed while the world looked empty; a player joined, and
    # the runtime still runs generation 1. Then the manifest changes what the runtime adopts in place.
    (setup.install / "secrets.toml").write_text(PASSWORD)
    store = Store(setup.volume)
    with store.locked():
        state = store.load()
        generation = reconcile(load(setup.install, SCHEMAS), state.generation)
        store.hand(state, MEADOWFOLD, generation.children[MEADOWFOLD], generation.number)
        state.generation = generation
        store.save(state)
    setup.players("Meadowfold", 2)
    (setup.install / "manifest.toml").write_text(TWO_WORLDS + live)

    with pytest.raises(Refused, match=r"restarts Meadowfold \(2 player\(s\)\)"):
        setup.apply()


def test_a_second_apply_while_one_runs_is_refused(setup: Setup) -> None:
    with (setup.volume / ".apply.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(Refused, match="another vhd apply, vhd import or vhd export is running"):
            setup.apply()


def test_removing_an_empty_world_needs_no_confirmation(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Ashwake", 0)
    (setup.install / "manifest.toml").write_text("schema = 1\n\n[world.Meadowfold]\n")
    setup.apply()
    assert ASHWAKE not in Store(setup.volume).load().handed


def test_the_install_env_quiets_the_orphan_warning_and_keeps_what_was_there(setup: Setup) -> None:
    (setup.install / ".env").write_text("VHD_DOCKER_SOCKET=/run/user/1000/docker.sock\n")
    setup.apply()
    setup.apply()
    assert (setup.install / ".env").read_text().splitlines() == [
        "VHD_DOCKER_SOCKET=/run/user/1000/docker.sock",
        "COMPOSE_IGNORE_ORPHANS=true",
    ]


def test_a_server_managers_own_orphan_setting_stands(setup: Setup) -> None:
    (setup.install / ".env").write_text("COMPOSE_IGNORE_ORPHANS=false\n")
    setup.apply()
    assert (setup.install / ".env").read_text() == "COMPOSE_IGNORE_ORPHANS=false\n"


RESTORE = Restore("a" * 32, "20261118T194011Z")
RESTORE_HANDED = {"id": RESTORE.id, "backup": RESTORE.backup}


def hold_restore(setup: Setup) -> None:
    """What the control plane leaves in the volume when a restore is asked for."""
    store = Store(setup.volume)
    with store.locked():
        state = store.load()
        state.restores[MEADOWFOLD] = RESTORE
        store.save(state)


def test_a_restore_the_control_plane_holds_reaches_the_generation_apply_writes(setup: Setup) -> None:
    setup.apply()
    hold_restore(setup)
    setup.apply()
    assert setup.document(MEADOWFOLD)["restore"] == RESTORE_HANDED
    assert "restore" not in setup.document(ASHWAKE)


def test_a_running_world_with_a_restore_its_runtime_has_not_acted_on_is_a_restart(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 2)
    hold_restore(setup)

    with pytest.raises(Refused, match=r"restarts Meadowfold \(2 player\(s\)\), where players may be online"):
        setup.apply()
    assert "restore" not in setup.document(MEADOWFOLD)

    setup.apply(disconnect_players=True)
    assert "restarting Meadowfold (2 player(s)), as asked" in setup.out.getvalue()
    assert setup.document(MEADOWFOLD)["restore"] == RESTORE_HANDED
    assert setup.document(MEADOWFOLD)["disconnect_players"] is True
    override = Store(setup.volume).load().override
    assert override is not None and override["worlds"] == {"Meadowfold": 2}


def test_an_empty_world_with_a_restore_needs_no_confirmation(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 0)
    hold_restore(setup)
    setup.apply()
    assert setup.document(MEADOWFOLD)["restore"] == RESTORE_HANDED
    assert "disconnect_players" not in setup.document(MEADOWFOLD)


def test_a_world_whose_runtime_reports_the_restore_is_not_restarted(setup: Setup) -> None:
    setup.apply()
    setup.docker.running = [MEADOWFOLD, ASHWAKE]
    setup.players("Meadowfold", 2)
    assert setup.worlds is not None
    setup.worlds["Meadowfold"]["report"]["restore"] = {"id": RESTORE.id, "outcome": "restoring"}
    hold_restore(setup)
    setup.apply()
    assert setup.document(MEADOWFOLD)["restore"] == RESTORE_HANDED
    assert "disconnect_players" not in setup.document(MEADOWFOLD)
    assert Store(setup.volume).load().override is None
