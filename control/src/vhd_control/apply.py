"""vhd apply: bring the install's containers in line with its manifest (G13).

The one place that holds Docker's socket, for its run alone. In order, it:
1. reads and validates the manifest and secrets;
2. refuses a Docker Engine older than 26, which lacks the volume subpaths runtimes mount;
3. learns the install's host path, Docker's socket and the Compose project from its own container;
4. takes the release's images from the install's compose.yaml (x-vhd);
5. refuses to restart a world that may have players online, unless told to disconnect them;
6. hands every runtime its documents, owned by the install folder's owner, a world moving to a
   new port in the directory only its new container mounts;
7. renders .vhd/compose.json, and has Compose recreate only the containers whose definition changed;
8. once Compose succeeds, records the ports the containers now publish.
"""

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from vhd_control import client, layout, manifest
from vhd_control.docker import Docker
from vhd_control.reconcile import Generation, reconcile
from vhd_control.render import Host, render
from vhd_control.schemas import Schemas
from vhd_control.status import WorldView
from vhd_control.store import Handed, State, Store

# Docker Engine 26, the first with volume subpaths.
MIN_ENGINE_API = (1, 45)
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
IMAGES = ("control-plane", "game-runtime")

_log = logging.getLogger(__name__)


class Refused(Exception):
    """vhd apply will not go ahead. The message says why, and what to do instead."""


@dataclass
class Apply:
    install: Path
    store: Store
    control_dir: Path
    schemas: Schemas
    docker: Docker
    status: Callable[[], dict[str, Any]]
    hostname: str
    out: TextIO

    def run(self, disconnect_players: bool) -> None:
        settings = manifest.load(self.install, self.schemas)
        self._check_engine()
        host = self._host()
        # vhd runs as root; everything it writes belongs to whom the control plane runs as.
        self.store = Store(self.store.root, host.owner)
        running = self._running(host.project)
        rendered_path = self.install / layout.RENDERED
        previous = json.loads(rendered_path.read_text()) if rendered_path.exists() else None
        with self.store.locked():
            state = self.store.load()
            generation = reconcile(settings, state.generation)
            rendered = render(generation, host)
            restarts = _restarts(generation, state, rendered, previous, running)
            if restarts:
                self._confirm(restarts, disconnect_players)
            moving = self._stage(state, generation)
            self._own(host.owner)
        self._write(rendered_path, rendered, host.owner)
        # Flushed, so it comes before Compose's own output.
        print(
            f"vhd apply: generation {generation.number} for {len(generation.children)} world(s)",
            file=self.out,
            flush=True,
        )
        # If Compose fails, state keeps the containers' old ports, and status shows the change
        # still waiting for vhd apply.
        self.docker.stream(
            ["compose", "-p", host.project, "-f", str(rendered_path), "up", "--detach", "--wait", "--remove-orphans"]
        )
        with self.store.locked():
            self._commit(self.store.load(), generation, moving)

    def _check_engine(self) -> None:
        version = self.docker.output(["version", "--format", "{{.Server.APIVersion}}"]).strip()
        try:
            api = tuple(int(part) for part in version.split("."))
        except ValueError as error:
            raise Refused(f"Docker reported API version {version!r}, which vhd cannot read") from error
        if api < MIN_ENGINE_API:
            raise Refused(f"Docker Engine API {version} is older than VHD needs: install Docker Engine 26 or newer")

    def _host(self) -> Host:
        container = json.loads(self.docker.output(["inspect", self.hostname]))[0]
        mounts = {m["Destination"]: m["Source"] for m in container["Mounts"]}
        project = container["Config"]["Labels"].get(PROJECT_LABEL)
        if str(layout.INSTALL) not in mounts or layout.DOCKER_SOCKET not in mounts or project is None:
            raise Refused("run vhd from the install folder, as: docker compose run --rm vhd apply")
        config = json.loads(
            self.docker.output(["compose", "-f", str(self.install / "compose.yaml"), "config", "--format", "json"])
        )
        images = dict(config.get("x-vhd", {}).get("images", {}))
        missing = [name for name in IMAGES if name not in images]
        if missing:
            raise Refused(f"the install's compose.yaml lists no {missing[0]} image; download it again from the release")
        owner = self.install.stat()
        return Host(
            project=project,
            install=mounts[str(layout.INSTALL)],
            docker_socket=mounts[layout.DOCKER_SOCKET],
            owner=(owner.st_uid, owner.st_gid),
            images=images,
        )

    def _running(self, project: str) -> set[str]:
        """The services of this install with a running container."""
        listed = self.docker.output(
            ["ps", "--filter", f"label={PROJECT_LABEL}={project}", "--format", f'{{{{.Label "{SERVICE_LABEL}"}}}}']
        )
        return {line for line in listed.splitlines() if line}

    def _confirm(self, restarts: dict[str, str], disconnect_players: bool) -> None:
        """Refuse to restart a world that may have players online, unless told to."""
        try:
            worlds = self.status()["worlds"]
        except client.Unreachable:
            worlds = {}
        occupied = sorted(
            name
            for name in restarts.values()
            if not WorldView(
                worlds.get(name, {}).get("state", "unknown"),
                worlds.get(name, {}).get("report"),
                worlds.get(name, {}).get("report_age_s"),
            ).empty
        )
        if not occupied:
            return
        if not disconnect_players:
            raise Refused(
                f"this apply restarts {', '.join(occupied)}, which may have players online. Wait until "
                "status shows no players, or run: docker compose run --rm vhd apply --disconnect-players"
            )
        _log.warning("restarting %s with players possibly online, as the server manager asked", ", ".join(occupied))
        print(
            f"vhd apply: restarting {', '.join(occupied)} with players possibly online, as asked",
            file=self.out,
            flush=True,
        )

    def _stage(self, state: State, generation: Generation) -> set[str]:
        """Hand each runtime what it may read now, and return the worlds moving to a new port.
        Those get their documents in the directory for the new port, which only the container
        Compose creates for it mounts, so the running one never reads a port it does not publish."""
        for directory in (self.store.root, self.control_dir):
            directory.chmod(0o755)
        applied = state.applied or {}
        moving: set[str] = set()
        for cid, child in generation.children.items():
            handed = state.handed.get(cid)
            if cid in applied and applied[cid] != child.port:
                self.store.hand(state, cid, child, generation.number, record=False)
                moving.add(cid)
            elif handed is None or handed.number != generation.number or handed.child != child:
                self.store.hand(state, cid, child, generation.number)
        state.generation = generation
        self.store.save(state)
        return moving

    def _commit(self, state: State, generation: Generation, moving: set[str]) -> None:
        """Once Compose has the containers in line, record what each world now runs."""
        for cid in moving:
            state.handed[cid] = Handed(generation.number, generation.children[cid])
        for cid in set(state.handed) - set(generation.children):
            del state.handed[cid]
        state.applied = {cid: child.port for cid, child in generation.children.items()}
        self.store.save(state)
        self.store.prune(state.applied)

    def _own(self, owner: tuple[int, int]) -> None:
        """Give the volumes' roots to the install folder's owner, whom the control plane runs as.
        The store gives it every file and directory it writes."""
        for directory in (self.store.root, self.control_dir):
            os.chown(directory, *owner)

    def _write(self, path: Path, document: dict[str, object], owner: tuple[int, int]) -> None:
        path.parent.mkdir(exist_ok=True)
        os.chown(path.parent, *owner)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        os.chown(temporary, *owner)
        temporary.replace(path)


def _restarts(
    generation: Generation,
    state: State,
    rendered: dict[str, Any],
    previous: dict[str, Any] | None,
    running: set[str],
) -> dict[str, str]:
    """The running worlds this apply restarts, by child id: those whose container changes, whose
    documents change in a way the runtime cannot adopt, or which the manifest no longer has."""
    before = (previous or {}).get("services", {})
    restarts: dict[str, str] = {}
    for cid, child in generation.children.items():
        if cid not in running:
            continue
        handed = state.handed.get(cid)
        container = before.get(cid) != rendered["services"][cid]
        documents = handed is None or handed.child != child
        if container or documents:
            restarts[cid] = child.world
    for cid, handed in state.handed.items():
        if cid in running and cid not in generation.children:
            restarts[cid] = handed.child.world
    return restarts
