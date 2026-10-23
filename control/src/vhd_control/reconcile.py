"""Reconcile: the settings, and the generation before, into the generation each child runs (B1).

One number counts generations for the whole install. It moves only when some child's
documents change, and a runtime adopts a new number for unchanged documents without a
restart, so a change to one world never restarts another.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise

from vhd_control import layout
from vhd_control.manifest import InvalidSettings, Settings, World
from vhd_control.naming import child_id

STATUS_URL = f"http://{layout.CONTROL_PLANE}:{layout.STATUS_PORT}/status"
STATUS_INTERVAL_S = 5
# A world's server uses its port through port+2 (Game facts).
WORLD_PORTS = 3


@dataclass(frozen=True)
class Child:
    """One game runtime's documents, without the generation number they are published under."""

    world: str
    port: int
    generation: Mapping[str, object]
    secrets: Mapping[str, object]

    def numbered(self, number: int, disconnect_players: bool = False) -> tuple[dict[str, object], dict[str, object]]:
        """The generation and secrets documents as the runtime reads them. disconnect_players
        carries the server manager's confirmation for this generation alone, so it is no part of
        the child: it never makes documents differ, and never restarts a world by itself."""
        generation = {**self.generation, "generation": number}
        if disconnect_players:
            generation["disconnect_players"] = True
        return generation, {**self.secrets, "generation": number}


@dataclass(frozen=True)
class Generation:
    number: int
    children: Mapping[str, Child]


def reconcile(settings: Settings, previous: Generation | None) -> Generation:
    """The generation for settings: previous itself when no child's documents changed."""
    _check_ports(settings.worlds)
    children = {child_id(world.name): _child(world) for world in settings.worlds}
    if previous is not None and dict(previous.children) == children:
        return previous
    return Generation(number=1 if previous is None else previous.number + 1, children=children)


def _child(world: World) -> Child:
    secrets: dict[str, object] = {"contract": 1}
    if world.password is not None:
        secrets["password"] = world.password
    return Child(
        world=world.name,
        port=world.port,
        generation={
            "contract": 2,
            "world": {"name": world.name, "port": world.port},
            "plugin": {"status_interval_s": STATUS_INTERVAL_S},
            "status_url": STATUS_URL,
        },
        secrets=secrets,
    )


def _check_ports(worlds: tuple[World, ...]) -> None:
    """What the manifest schema cannot express: no two worlds' port ranges overlap."""
    for lower, upper in pairwise(sorted(worlds, key=lambda w: w.port)):
        last = lower.port + WORLD_PORTS - 1
        if upper.port <= last:
            raise InvalidSettings(
                f"worlds {lower.name!r} and {upper.name!r} share ports: a world uses its port through "
                f"port+{WORLD_PORTS - 1}, so {lower.name!r} on {lower.port} reaches {last}"
            )
