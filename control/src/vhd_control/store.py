"""The generations volume: what each child was handed, and the install's state beside it.

    <root>/state.json                    the newest generation, what each child holds, what vhd apply applied
    <root>/<child id>/secrets.json       what that child reads (B1), secrets first
    <root>/<child id>/generation.json
    <root>/.lock                         held by whichever writes: the control plane or vhd apply

A child mounts only its own directory, so the root, state.json included, is the control
plane's and vhd's alone.
"""

import fcntl
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from vhd_control.reconcile import Child, Generation

STATE_FILE = "state.json"
GENERATION_FILE = "generation.json"
SECRETS_FILE = "secrets.json"


@dataclass(frozen=True)
class Handed:
    """The documents a child holds, and the generation they were published under."""

    number: int
    child: Child


@dataclass
class State:
    generation: Generation | None = None
    handed: dict[str, Handed] = field(default_factory=dict)
    # The port each world's container was created with, by child id. None until vhd apply has run.
    applied: dict[str, int] | None = None


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the volume's lock, so the control plane and vhd apply never write at once."""
        with (self.root / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def load(self) -> State:
        path = self.root / STATE_FILE
        if not path.exists():
            return State()
        document = json.loads(path.read_text())
        generation = document.get("generation")
        return State(
            generation=None if generation is None else _generation(generation),
            handed={cid: Handed(h["number"], _child(h["child"])) for cid, h in document["handed"].items()},
            applied=document.get("applied"),
        )

    def save(self, state: State) -> None:
        document = {
            "generation": None if state.generation is None else _generation_document(state.generation),
            "handed": {cid: {"number": h.number, "child": _child_document(h.child)} for cid, h in state.handed.items()},
            "applied": state.applied,
        }
        # It holds every world's secrets, so only its owner reads it.
        _write(self.root / STATE_FILE, document, 0o600)

    def hand(self, state: State, child_id: str, child: Child, number: int) -> None:
        """Publish a child's documents under number: secrets first, the generation second (B1)."""
        directory = self.root / child_id
        directory.mkdir(exist_ok=True)
        directory.chmod(0o755)
        generation, secrets = child.numbered(number)
        _write(directory / SECRETS_FILE, secrets, 0o644)
        _write(directory / GENERATION_FILE, generation, 0o644)
        state.handed[child_id] = Handed(number, child)


def _write(path: Path, document: object, mode: int) -> None:
    """Write whole, then move into place, so a reader never sees half a file."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(document, sort_keys=True))
    temporary.chmod(mode)
    os.replace(temporary, path)


def _child_document(child: Child) -> dict[str, object]:
    return {"world": child.world, "port": child.port, "generation": child.generation, "secrets": child.secrets}


def _child(document: Mapping[str, Any]) -> Child:
    return Child(
        world=cast(str, document["world"]),
        port=cast(int, document["port"]),
        generation=cast(dict[str, object], document["generation"]),
        secrets=cast(dict[str, object], document["secrets"]),
    )


def _generation_document(generation: Generation) -> dict[str, object]:
    return {"number": generation.number, "children": {c: _child_document(d) for c, d in generation.children.items()}}


def _generation(document: Mapping[str, Any]) -> Generation:
    return Generation(
        number=cast(int, document["number"]),
        children={c: _child(d) for c, d in cast(dict[str, Any], document["children"]).items()},
    )
