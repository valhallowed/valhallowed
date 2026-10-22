import json
import os
from pathlib import Path

import pytest

from vhd_control.daemon import WAITS_FOR_APPLY, WAITS_FOR_EMPTY, Daemon
from vhd_control.manifest import load
from vhd_control.naming import child_id
from vhd_control.reconcile import reconcile
from vhd_control.schemas import Schemas
from vhd_control.status import StatusBook
from vhd_control.store import Store

REPO = Path(__file__).resolve().parents[3]
SCHEMAS = Schemas(REPO)
MEADOWFOLD = child_id("Meadowfold")
ASHWAKE = child_id("Ashwake")
TWO_WORLDS = "schema = 1\n\n[world.Meadowfold]\nport = 2456\n\n[world.Ashwake]\nport = 2459\n"


class Install:
    """An install folder and its generations volume, as vhd apply leaves them."""

    def __init__(self, root: Path) -> None:
        self.folder = root / "install"
        self.volume = root / "generations"
        self.folder.mkdir()
        self.volume.mkdir()
        self.store = Store(self.volume)
        self.book = StatusBook()
        self.write(TWO_WORLDS)
        self.apply()
        self.daemon = Daemon(self.folder, self.store, SCHEMAS, self.book)

    def write(self, manifest: str, secrets: str | None = None) -> None:
        """Edit the files, moving their modification times as an edit a second later would."""
        self.edits = getattr(self, "edits", 0) + 1
        files = {"manifest.toml": manifest} if secrets is None else {"manifest.toml": manifest, "secrets.toml": secrets}
        for name, text in files.items():
            path = self.folder / name
            path.write_text(text)
            os.utime(path, ns=(0, self.edits * 1_000_000_000))

    def apply(self) -> None:
        """What vhd apply does to the volume: hand every runtime its documents, record the ports."""
        with self.store.locked():
            state = self.store.load()
            generation = reconcile(load(self.folder, SCHEMAS), state.generation)
            for cid, child in generation.children.items():
                self.store.hand(state, cid, child, generation.number)
            state.generation = generation
            state.applied = {cid: child.port for cid, child in generation.children.items()}
            self.store.save(state)

    def players(self, world: str, players: int) -> None:
        self.book.record({"contract": 1, "generation": 1, "world": world, "state": "running", "players": players})

    def handed(self, cid: str) -> tuple[int, str | None]:
        """What the world's container reads: the documents for the port it was created with."""
        applied = self.store.load().applied
        assert applied is not None
        directory = self.store.directory(cid, applied[cid])
        document = json.loads((directory / "generation.json").read_text())
        secrets = json.loads((directory / "secrets.json").read_text())
        return document["generation"], secrets.get("password")


@pytest.fixture
def install(tmp_path: Path) -> Install:
    return Install(tmp_path)


PASSWORD = 'schema = 1\n\n[world.Meadowfold]\npassword = "hearth-and-horn"\n'


def test_nothing_changed_hands_nothing(install: Install) -> None:
    before = install.handed(MEADOWFOLD)
    install.daemon.step()
    assert install.handed(MEADOWFOLD) == before
    assert install.daemon.pending(MEADOWFOLD) is None


def test_a_restart_reaches_an_empty_world(install: Install) -> None:
    install.players("Meadowfold", 0)
    install.daemon.step()
    install.write(TWO_WORLDS, PASSWORD)
    install.daemon.step()
    assert install.handed(MEADOWFOLD) == (2, "hearth-and-horn")


def test_a_restart_waits_while_players_are_online(install: Install) -> None:
    install.players("Meadowfold", 2)
    install.daemon.step()
    install.write(TWO_WORLDS, PASSWORD)
    install.daemon.step()
    assert install.handed(MEADOWFOLD) == (1, None)
    assert install.daemon.pending(MEADOWFOLD) == (2, WAITS_FOR_EMPTY)

    install.players("Meadowfold", 0)
    install.daemon.step()
    assert install.handed(MEADOWFOLD) == (2, "hearth-and-horn")
    assert install.daemon.pending(MEADOWFOLD) is None


def test_a_restart_waits_while_status_cannot_vouch_for_the_world(install: Install) -> None:
    install.daemon.step()
    install.write(TWO_WORLDS, PASSWORD)
    install.daemon.step()
    assert install.daemon.pending(MEADOWFOLD) == (2, WAITS_FOR_EMPTY)


def test_another_world_takes_the_new_number_without_a_restart(install: Install) -> None:
    install.players("Meadowfold", 0)
    install.players("Ashwake", 3)
    install.write(TWO_WORLDS, PASSWORD)
    install.daemon.step()
    assert install.handed(ASHWAKE) == (2, None)


def test_a_new_port_waits_for_vhd_apply(install: Install) -> None:
    install.players("Meadowfold", 0)
    install.write(TWO_WORLDS.replace("2456", "2462"))
    install.daemon.step()
    assert install.handed(MEADOWFOLD) == (1, None)
    assert install.daemon.pending(MEADOWFOLD) == (2, WAITS_FOR_APPLY)


def test_a_refused_manifest_keeps_the_last_good_generation(install: Install) -> None:
    install.daemon.step()
    install.write("schema = 1\n\n[world.Meadowfold]\nprot = 2456\n")
    install.daemon.step()
    assert install.daemon.refused is not None
    assert "'prot' was unexpected" in install.daemon.refused
    assert install.handed(MEADOWFOLD) == (1, None)


def test_overlapping_ports_are_refused_and_change_nothing(install: Install) -> None:
    install.daemon.step()
    install.write(TWO_WORLDS.replace("2459", "2457"))
    install.daemon.step()
    assert install.daemon.refused is not None
    assert "share ports" in install.daemon.refused
    assert install.store.load().generation is not None
    assert install.store.load().generation.number == 1  # type: ignore[union-attr]
