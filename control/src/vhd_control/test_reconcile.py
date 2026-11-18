from collections.abc import Callable
from pathlib import Path

import pytest

from vhd_control.manifest import Backups, InvalidSettings, Settings, Updates, World, load
from vhd_control.naming import child_id
from vhd_control.reconcile import LIVE, STATUS_INTERVAL_S, STATUS_URL, Generation, Service, reconcile
from vhd_control.test_daemon import REPO, SCHEMAS

# The manifest schema's defaults.
BACKUPS = Backups(path="backups", every_s=3600, keep=24, keep_daily=14)
UPDATES = Updates(policy="when-empty", every_s=900)


def settings(*worlds: World, backups: Backups = BACKUPS, updates: Updates = UPDATES) -> Settings:
    return Settings(worlds=worlds, web_map_port=8456, backups=backups, updates=updates)


@pytest.mark.parametrize("example", sorted((REPO / "schema" / "examples" / "valid").glob("manifest-*.toml")))
def test_every_valid_manifest_reconciles_into_documents_runtimes_accept(tmp_path: Path, example: Path) -> None:
    (tmp_path / "manifest.toml").write_text(example.read_text())
    generation = reconcile(load(tmp_path, SCHEMAS), None)

    assert generation.number == 1
    for child in generation.children.values():
        document, secrets = child.numbered(generation.number)
        assert SCHEMAS.problem("contracts/game-runtime.generation.schema.json", document) is None
        assert SCHEMAS.problem("contracts/game-runtime.secrets.schema.json", secrets) is None
    for name, service in generation.services.items():
        document, secrets = service.numbered(generation.number)
        assert SCHEMAS.problem(f"contracts/{name}.generation.schema.json", document) is None
        assert SCHEMAS.problem(f"contracts/{name}.secrets.schema.json", secrets) is None


def test_a_password_reaches_only_its_world_secrets() -> None:
    generation = reconcile(settings(World("Ashwake", 2459, None), World("Meadowfold", 2456, "hearth-and-horn")), None)
    secrets = {c.world: c.secrets for c in generation.children.values()}
    assert secrets == {"Ashwake": {"contract": 1}, "Meadowfold": {"contract": 1, "password": "hearth-and-horn"}}
    assert all(c.generation["status_url"] == STATUS_URL for c in generation.children.values())


def test_unchanged_settings_keep_the_generation() -> None:
    first = reconcile(settings(World("Meadowfold", 2456, None)), None)
    assert reconcile(settings(World("Meadowfold", 2456, None)), first) is first


def test_a_change_to_any_world_moves_the_install_one_generation() -> None:
    first = reconcile(settings(World("Ashwake", 2459, None), World("Meadowfold", 2456, None)), None)
    second = reconcile(settings(World("Ashwake", 2459, None), World("Meadowfold", 2456, "hearth-and-horn")), first)
    assert second.number == 2
    ashwake = child_id("Ashwake")
    assert second.children[ashwake] == first.children[ashwake]


@pytest.mark.parametrize("ports", [(2456, 2456), (2456, 2458), (2458, 2456)])
def test_overlapping_world_ports_are_refused(ports: tuple[int, int]) -> None:
    with pytest.raises(InvalidSettings, match="share ports"):
        reconcile(settings(World("Ashwake", ports[0], None), World("Meadowfold", ports[1], None)), None)


def test_adjacent_world_ports_are_accepted() -> None:
    generation = reconcile(settings(World("Ashwake", 2456, None), World("Meadowfold", 2459, None)), None)
    assert len(generation.children) == 2


def test_two_worlds_that_would_share_a_service_name_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vhd_control.reconcile.child_id", lambda name: "world-same-000000")
    with pytest.raises(InvalidSettings, match="would share the service name"):
        reconcile(settings(World("Ashwake", 2456, None), World("Meadowfold", 2459, None)), None)


def tokens(*values: str) -> Callable[[], str]:
    """A token source that hands out values in order, and fails if asked for more."""
    remaining = iter(values)
    return lambda: next(remaining)


INTAKE_PASSWORD = "i" * 32
WEB_MAP_PASSWORD = "w" * 32


def test_the_database_holds_every_role_password_and_each_service_only_its_own() -> None:
    generation = reconcile(
        settings(World("Meadowfold", 2456, None)), None, token=tokens(INTAKE_PASSWORD, WEB_MAP_PASSWORD)
    )
    assert generation.services["database"].secrets == {
        "contract": 2,
        "passwords": {"intake": INTAKE_PASSWORD, "web_map": WEB_MAP_PASSWORD},
    }
    assert generation.services["intake"].secrets == {"contract": 1, "password": INTAKE_PASSWORD}
    assert generation.services["web-map"].secrets == {"contract": 2, "password": WEB_MAP_PASSWORD}
    assert INTAKE_PASSWORD not in repr(generation)
    assert WEB_MAP_PASSWORD not in repr(generation)


def test_passwords_carry_on_from_the_generation_before() -> None:
    token = tokens("a" * 32, "b" * 32, "c" * 32, "d" * 32)
    first = reconcile(settings(World("Meadowfold", 2456, None)), None, token=token)
    second = reconcile(settings(World("Meadowfold", 2457, None)), first, token=token)
    assert second.number == 2
    assert second.services["intake"].secrets["password"] == "a" * 32
    assert second.services["web-map"].secrets["password"] == "b" * 32
    # With the state lost, there is no generation before, and new passwords.
    lost = reconcile(settings(World("Meadowfold", 2456, None)), None, token=token)
    assert lost.services["intake"].secrets["password"] == "c" * 32
    assert lost.services["web-map"].secrets["password"] == "d" * 32


def test_a_generation_from_before_the_web_map_role_keeps_intake_and_gains_web_map() -> None:
    meadowfold = settings(World("Meadowfold", 2456, None))
    current = reconcile(meadowfold, None, token=tokens(INTAKE_PASSWORD, WEB_MAP_PASSWORD))
    reporting = {"status_url": STATUS_URL, "status_interval_s": STATUS_INTERVAL_S}
    # What a release before the web map's role left: the database holds intake's password alone.
    before = Generation(
        number=4,
        children=current.children,
        services={
            **current.services,
            "database": Service(
                name="database",
                generation={"contract": 1, **reporting},
                secrets={"contract": 1, "passwords": {"intake": INTAKE_PASSWORD}},
            ),
            "web-map": Service(
                name="web-map", generation={"contract": 1, **reporting}, secrets={"contract": 1}, port=8456
            ),
        },
    )

    after = reconcile(meadowfold, before, token=tokens("n" * 32))

    assert after.number == 5
    assert after.services["database"].secrets["passwords"] == {"intake": INTAKE_PASSWORD, "web_map": "n" * 32}
    assert after.services["intake"] == before.services["intake"]
    assert after.services["web-map"].secrets == {"contract": 2, "password": "n" * 32}


def test_the_web_map_lists_each_world_with_where_its_map_export_is_mounted() -> None:
    generation = reconcile(settings(World("Meadowfold", 2456, None), World("Ashwake", 2459, None)), None)

    web_map = generation.services["web-map"]
    assert web_map.generation["contract"] == 3
    assert web_map.generation["worlds"] == [
        {"name": "Ashwake", "map_export": f"/map-export/{child_id('Ashwake')}"},
        {"name": "Meadowfold", "map_export": f"/map-export/{child_id('Meadowfold')}"},
    ]
    assert web_map.mounts == tuple(sorted(generation.children))
    assert generation.services["intake"].mounts == ()


def test_a_world_added_changes_the_web_maps_documents() -> None:
    first = reconcile(settings(World("Meadowfold", 2456, None)), None)
    second = reconcile(settings(World("Meadowfold", 2456, None), World("Ashwake", 2459, None)), first)
    assert second.number == first.number + 1
    assert second.services["web-map"].mounts != first.services["web-map"].mounts


def test_the_web_map_signs_in_to_the_database_as_its_own_role() -> None:
    generation = reconcile(settings(World("Meadowfold", 2456, None)), None)
    address = {"host": "database", "port": 5432, "name": "vhd"}
    assert generation.services["web-map"].generation["database"] == {**address, "user": "web_map"}
    assert generation.services["intake"].generation["database"] == {**address, "user": "intake"}


def test_intake_takes_telemetry_from_the_worlds_the_generation_runs() -> None:
    first = reconcile(settings(World("Ashwake", 2459, None), World("Meadowfold", 2456, None)), None)
    assert first.services["intake"].generation["worlds"] == {
        child_id("Ashwake"): "Ashwake",
        child_id("Meadowfold"): "Meadowfold",
    }
    second = reconcile(settings(World("Meadowfold", 2456, None)), first)
    assert second.services["intake"].generation["worlds"] == {child_id("Meadowfold"): "Meadowfold"}


def test_each_world_carries_the_backups_and_updates() -> None:
    generation = reconcile(
        settings(World("Meadowfold", 2456, None), backups=Backups("backups", 1800, 48, 0), updates=UPDATES), None
    )
    (child,) = generation.children.values()
    assert child.generation["backups"] == {"every_s": 1800, "keep": 48, "keep_daily": 0}
    assert child.generation["updates"] == {"policy": "when-empty", "every_s": 900}
    assert generation.backups == "backups"


def test_a_backups_or_updates_change_restarts_no_game_and_a_password_does() -> None:
    world = World("Meadowfold", 2456, None)
    (before,) = reconcile(settings(world), None).children.values()
    (live,) = reconcile(
        settings(world, backups=Backups("backups", 7200, 5, 3), updates=Updates("at-start", 3600)), None
    ).children.values()
    (password,) = reconcile(settings(World("Meadowfold", 2456, "hearth-and-horn")), None).children.values()

    assert live != before
    assert not before.restarts(live)
    assert before.restarts(password)


def test_a_new_backups_folder_makes_a_new_generation_with_the_same_documents() -> None:
    first = reconcile(settings(World("Meadowfold", 2456, None)), None)
    moved = reconcile(settings(World("Meadowfold", 2456, None), backups=Backups("/srv/backups", 3600, 24, 14)), first)

    assert moved.number == first.number + 1
    assert moved.backups == "/srv/backups"
    assert dict(moved.children) == dict(first.children)


def test_the_live_settings_and_their_bounds_agree_across_manifest_and_contract() -> None:
    manifest = SCHEMAS.document("schema/manifest.schema.json")["properties"]
    contract = SCHEMAS.document("contracts/game-runtime.generation.schema.json")["properties"]
    live = {
        table
        for table, spec in manifest.items()
        if any(key.get("x-change-cost") == "live" for key in spec.get("properties", {}).values())
    }
    assert live == set(LIVE)
    for table in LIVE:
        bounds = manifest[table]["properties"]["every"]["x-seconds"]
        every = contract[table]["properties"]["every_s"]
        assert (every["minimum"], every["maximum"]) == (bounds["minimum"], bounds["maximum"])
