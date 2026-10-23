"""The generations volume: what each child was handed, and the install's state beside it.

    <root>/state.json                           the newest generation, what each child holds, what vhd apply applied
    <root>/<child id>/<port>/secrets.json       what that child reads (B1), secrets first
    <root>/<child id>/<port>/generation.json
    <root>/.lock                                held by whichever writes: the control plane or vhd apply

A child's container mounts only the directory for its own port, so the root, state.json
included, is the control plane's and vhd's alone, and a world whose port changes reads its new
documents only in the container vhd apply creates for that port.
"""

import fcntl
import json
import os
import shutil
from collections.abc import Collection, Iterator, Mapping
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
    # The last time vhd apply restarted worlds that may have had players online, as the server
    # manager confirmed: the generation, and each such world's player count, or None if unknown.
    override: dict[str, object] | None = None


class Store:
    """owner: whom every file and directory is given to, when the writer is someone else. vhd
    apply runs as root; the control plane, which runs as that owner, then writes them too."""

    def __init__(self, root: Path, owner: tuple[int, int] | None = None) -> None:
        self.root = root
        self.owner = owner

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the volume's lock, so the control plane and vhd apply never write at once."""
        with (self.root / ".lock").open("a") as lock:
            if self.owner is not None:
                os.fchown(lock.fileno(), *self.owner)
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
            override=document.get("override"),
        )

    def save(self, state: State) -> None:
        document = {
            "generation": None if state.generation is None else _generation_document(state.generation),
            "handed": {cid: {"number": h.number, "child": _child_document(h.child)} for cid, h in state.handed.items()},
            "applied": state.applied,
            "override": state.override,
        }
        # It holds every world's secrets, so only its owner reads it.
        self._write(self.root / STATE_FILE, document, 0o600)

    def hand(self, state: State, child_id: str, child: Child, number: int, record: bool = True) -> None:
        """Publish a child's documents under number, into the directory for its port: secrets
        first, the generation second (B1). Unless record is false, state notes that the child
        holds them; vhd apply records a world moving to a new port only once Compose has moved it."""
        directory = self.directory(child_id, child.port)
        for level in (directory.parent, directory):
            level.mkdir(exist_ok=True)
            level.chmod(0o755)
            if self.owner is not None:
                os.chown(level, *self.owner)
        generation, secrets = child.numbered(number)
        self._write(directory / SECRETS_FILE, secrets, 0o644)
        self._write(directory / GENERATION_FILE, generation, 0o644)
        if record:
            state.handed[child_id] = Handed(number, child)

    def holds(self, child_id: str, port: int) -> bool:
        """Whether the directory for port has both of the child's files."""
        directory = self.directory(child_id, port)
        return (directory / SECRETS_FILE).exists() and (directory / GENERATION_FILE).exists()

    def directory(self, child_id: str, port: int) -> Path:
        """Where a child's documents live while its container runs on port: its subpath."""
        return self.root / child_id / str(port)

    def prune(self, ports: Mapping[str, int], removed: Collection[str]) -> None:
        """Remove what no container mounts any more: a removed child's directory, and a world's
        old ports once Compose has moved it. A directory the store never handed is left alone."""
        for child_id in removed:
            shutil.rmtree(self.root / child_id, ignore_errors=True)
        for child_id, port in ports.items():
            directory = self.root / child_id
            if not directory.is_dir():
                continue
            for stale in directory.iterdir():
                if stale.name == str(port):
                    continue
                if stale.is_dir():
                    shutil.rmtree(stale)
                else:
                    stale.unlink()

    def _write(self, path: Path, document: object, mode: int) -> None:
        """Write whole and flushed to disk, then move into place, so a reader never sees half a
        file and a crash never leaves an empty one. The file has its mode and owner from the
        start: state.json holds every secret, so no moment may find it readable to others."""
        temporary = path.with_name(f".{path.name}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(descriptor, "w") as file:
            os.fchmod(file.fileno(), mode)
            if self.owner is not None:
                os.fchown(file.fileno(), *self.owner)
            file.write(json.dumps(document, sort_keys=True))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


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
