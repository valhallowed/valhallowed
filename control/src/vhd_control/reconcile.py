"""Reconcile: the settings, and the generation before, into the generation each child runs (B1).

One number counts generations for the whole install. It moves only when some child's
documents change, and a runtime adopts a new number for unchanged documents without a
restart, so a change to one world never restarts another.

The data layer's internal passwords are generated here, never asked for (Secrets), and each
generation carries on the one before's, so a password changes only when the install's state is
lost, and a role new to the install gets one. The database sets every role's password again
whenever they change.
"""

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from typing import cast

from vhd_control import layout
from vhd_control.manifest import InvalidSettings, Settings, World
from vhd_control.naming import child_id

STATUS_URL = f"http://{layout.CONTROL_PLANE}:{layout.STATUS_PORT}/status"
# Where each world's plugin posts telemetry (B2).
INTAKE_URL = f"http://{layout.INTAKE}:{layout.INTAKE_PORT}/events"
STATUS_INTERVAL_S = 5
# A world reserves its port through port+2 (Game facts).
WORLD_PORTS = 3
# The database roles the services sign in as, each with a generated password.
ROLES = (layout.INTAKE_ROLE, layout.WEB_MAP_ROLE)

Token = Callable[[], str]


def _token() -> str:
    """A new password: 32 characters of URL-safe base64."""
    return secrets.token_urlsafe(24)


def last_port(port: int) -> int:
    """The last of the ports a world on port reserves."""
    return port + WORLD_PORTS - 1


@dataclass(frozen=True)
class Child:
    """What reconcile hands one child, a game runtime: its documents, without the generation
    number they are published under."""

    world: str
    port: int
    generation: Mapping[str, object]
    # Never in a repr, so no log line or test diff can carry a password.
    secrets: Mapping[str, object] = field(repr=False)

    def numbered(self, number: int, disconnect_players: bool = False) -> tuple[dict[str, object], dict[str, object]]:
        """The generation and secrets documents as the runtime reads them. disconnect_players
        carries the server manager's confirmation for this generation alone, so it is no part of
        the child: it never makes documents differ, and never restarts a world by itself."""
        generation = {**self.generation, "generation": number}
        if disconnect_players:
            generation["disconnect_players"] = True
        return generation, {**self.secrets, "generation": number}


@dataclass(frozen=True)
class Service:
    """What reconcile hands one data-layer service: its documents, without the generation number
    they are published under."""

    name: str
    generation: Mapping[str, object]
    # Never in a repr, so no log line or test diff can carry a password.
    secrets: Mapping[str, object] = field(repr=False)
    # The host port vhd apply publishes for the service, which no document carries: a change to it
    # waits for vhd apply (G13). None for a service the host never reaches.
    port: int | None = None

    def numbered(self, number: int) -> tuple[dict[str, object], dict[str, object]]:
        """The generation and secrets documents as the service reads them."""
        return {**self.generation, "generation": number}, {**self.secrets, "generation": number}


@dataclass(frozen=True)
class Generation:
    number: int
    children: Mapping[str, Child]
    services: Mapping[str, Service] = field(default_factory=dict)


def reconcile(settings: Settings, previous: Generation | None, token: Token = _token) -> Generation:
    """The generation for settings: previous itself when no child's documents changed."""
    _check_ports(settings.worlds)
    children: dict[str, Child] = {}
    for world in settings.worlds:
        cid = child_id(world.name)
        if cid in children:
            raise InvalidSettings(
                f"worlds {children[cid].world!r} and {world.name!r} would share the service name {cid}; rename one"
            )
        children[cid] = _child(world)
    services = _services(children, _passwords(previous, token), settings.web_map_port)
    if previous is not None and dict(previous.children) == children and dict(previous.services) == services:
        return previous
    return Generation(number=1 if previous is None else previous.number + 1, children=children, services=services)


def _child(world: World) -> Child:
    secrets: dict[str, object] = {"contract": 1}
    if world.password is not None:
        secrets["password"] = world.password
    return Child(
        world=world.name,
        port=world.port,
        generation={
            "contract": 3,
            "world": {"name": world.name, "port": world.port},
            "plugin": {"status_interval_s": STATUS_INTERVAL_S, "intake_url": INTAKE_URL},
            "status_url": STATUS_URL,
        },
        secrets=secrets,
    )


def _passwords(previous: Generation | None, token: Token) -> dict[str, str]:
    """Each role's password: the one the generation before held, or a new one."""
    database = None if previous is None else previous.services.get(layout.DATABASE)
    held = cast(Mapping[str, str], database.secrets["passwords"]) if database is not None else {}
    return {role: held.get(role) or token() for role in ROLES}


def _services(children: Mapping[str, Child], passwords: Mapping[str, str], web_map_port: int) -> dict[str, Service]:
    """The data layer's documents. Intake takes telemetry from the worlds the generation runs,
    each known by its Compose service name, so a world the manifest dropped is refused from now on."""
    reporting = {"status_url": STATUS_URL, "status_interval_s": STATUS_INTERVAL_S}
    return {
        layout.DATABASE: Service(
            name=layout.DATABASE,
            generation={"contract": 1, **reporting},
            secrets={"contract": 2, "passwords": dict(passwords)},
        ),
        layout.INTAKE: Service(
            name=layout.INTAKE,
            generation={
                "contract": 1,
                **reporting,
                "database": _database(layout.INTAKE_ROLE),
                "worlds": {cid: child.world for cid, child in sorted(children.items())},
            },
            secrets={"contract": 1, "password": passwords[layout.INTAKE_ROLE]},
        ),
        layout.WEB_MAP: Service(
            name=layout.WEB_MAP,
            generation={"contract": 2, **reporting, "database": _database(layout.WEB_MAP_ROLE)},
            secrets={"contract": 2, "password": passwords[layout.WEB_MAP_ROLE]},
            port=web_map_port,
        ),
    }


def _database(role: str) -> dict[str, object]:
    """Where a service signs in to the database, as role: one address for every service."""
    return {"host": layout.DATABASE, "port": layout.DATABASE_PORT, "name": layout.DATABASE_NAME, "user": role}


def _check_ports(worlds: tuple[World, ...]) -> None:
    """What the manifest schema cannot express: no two worlds' port ranges overlap."""
    for lower, upper in pairwise(sorted(worlds, key=lambda w: w.port)):
        last = last_port(lower.port)
        if upper.port <= last:
            raise InvalidSettings(
                f"worlds {lower.name!r} and {upper.name!r} share ports: a world reserves its port through "
                f"port+{WORLD_PORTS - 1}, so {lower.name!r} on {lower.port} reaches {last}"
            )
