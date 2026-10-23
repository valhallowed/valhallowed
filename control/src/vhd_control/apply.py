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
8. once Compose succeeds, records the ports the containers now publish. If Compose fails, it
   brings the install back on the compose file it ran before.
"""

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TextIO

from vhd_control import client, layout, manifest
from vhd_control.docker import Docker, DockerError
from vhd_control.reconcile import Generation, reconcile
from vhd_control.render import IMAGES, Host, render
from vhd_control.schemas import Schemas
from vhd_control.status import WorldView
from vhd_control.store import Handed, State, Store

# Docker Engine 26, the first with volume subpaths.
MIN_ENGINE_API = (1, 45)
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
# How long Compose waits for the containers it starts to run.
WAIT_TIMEOUT_S = 300

_log = logging.getLogger(__name__)


class Refused(Exception):
    """vhd apply will not go ahead. The message says why, and what to do instead."""


@dataclass(frozen=True)
class _Restart:
    world: str
    # Compose recreates or removes its container.
    container: bool
    # Its runtime gets documents it cannot adopt without restarting the game.
    documents: bool


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
        # Before the players check, not after it: a pull can take minutes.
        self._pull(host)
        running = self._running(host.project)
        applied_path = self.install / layout.RENDERED
        previous = json.loads(applied_path.read_text()) if applied_path.exists() else None
        with self.store.locked():
            state = self.store.load()
            generation = reconcile(settings, state.generation)
            rendered = render(generation, host, settings.web_map_port)
            restarts = _restarts(generation, state, rendered, previous, running)
            override = self._confirm(restarts, disconnect_players) if restarts else None
            self._stop_recreated(host, state, generation, restarts)
            confirmed = {cid for cid, restart in restarts.items() if override and restart.world in override}
            moving = self._stage(state, generation, confirmed)
            self._own(host.owner)
        candidate = self.install / layout.CANDIDATE
        self._write(candidate, rendered, host.owner)
        # Flushed, so it comes before Compose's own output.
        print(
            f"vhd apply: generation {generation.number} for {len(generation.children)} world(s)",
            file=self.out,
            flush=True,
        )
        try:
            self.docker.stream(self._up(host, candidate))
        except DockerError as error:
            candidate.unlink()
            self._roll_back(host, previous is not None, error, generation.number)
        candidate.replace(applied_path)
        with self.store.locked():
            self._commit(self.store.load(), generation, moving, override)

    def _pull(self, host: Host) -> None:
        """Every image the install runs, pulled when the host lacks it."""
        for image in sorted(set(host.images.values())):
            try:
                self.docker.output(["image", "inspect", "--format", "{{.Id}}", image])
            except DockerError:
                self.docker.stream(["pull", image])

    def _up(self, host: Host, compose_file: Path) -> list[str]:
        return [
            "compose", "-p", host.project, "-f", str(compose_file),
            "up", "--detach", "--wait", "--wait-timeout", str(WAIT_TIMEOUT_S), "--remove-orphans",
        ]  # fmt: skip

    def _roll_back(self, host: Host, ran_before: bool, error: DockerError, number: int) -> NoReturn:
        """Compose failed partway: a container it recreated may be gone. Bring the install back on
        the compose file it ran before, which vhd apply has not touched, and whose directories
        state still records."""
        if not ran_before:
            raise error
        print(
            "vhd apply: Compose failed; bringing back the containers the install ran before", file=self.out, flush=True
        )
        try:
            self.docker.stream(self._up(host, self.install / layout.RENDERED))
        except DockerError as again:
            raise DockerError(f"{error}; bringing back the previous containers failed too: {again}") from again
        raise Refused(
            f"Compose could not apply generation {number} ({error}). The install is back on the "
            "containers it ran before; fix the cause, then run vhd apply again."
        ) from error

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
            owner=(owner.st_uid, owner.st_gid),
            images=images,
        )

    def _running(self, project: str) -> set[str]:
        """The services of this install with a running container."""
        listed = self.docker.output(
            ["ps", "--filter", f"label={PROJECT_LABEL}={project}", "--format", f'{{{{.Label "{SERVICE_LABEL}"}}}}']
        )
        return {line for line in listed.splitlines() if line}

    def _confirm(self, restarts: Mapping[str, _Restart], disconnect_players: bool) -> dict[str, object] | None:
        """Refuse to restart a world that may have players online, unless told to. Told to, it
        returns the override to record with the change: each such world's player count, or None
        where status could not say."""
        try:
            worlds = self.status()["worlds"]
        except client.Unreachable:
            worlds = {}
        occupied: dict[str, int | None] = {}
        for restart in restarts.values():
            entry = worlds.get(restart.world, {})
            view = WorldView(entry.get("state", "unknown"), entry.get("report"), entry.get("report_age_s"))
            if not view.empty:
                players = (view.report or {}).get("players") if view.state != "unknown" else None
                occupied[restart.world] = players
        if not occupied:
            return None
        named = ", ".join(
            f"{world} ({'players unknown' if players is None else f'{players} player(s)'})"
            for world, players in sorted(occupied.items())
        )
        if not disconnect_players:
            raise Refused(
                f"this apply restarts {named}, where players may be online. Wait until status shows "
                "no players, or run: docker compose run --rm vhd apply --disconnect-players"
            )
        _log.warning("restarting %s, as the server manager asked", named)
        print(f"vhd apply: restarting {named}, as asked", file=self.out, flush=True)
        return dict(occupied)

    def _stop_recreated(
        self, host: Host, state: State, generation: Generation, restarts: Mapping[str, _Restart]
    ) -> None:
        """Stop each world whose container and documents both change in place. Handed the
        documents while it ran, its runtime would restart the game on them, and Compose would
        then restart it again. A world moving to a new port reads them only in its new container."""
        applied = state.applied or {}
        stopping = sorted(
            cid
            for cid, restart in restarts.items()
            if restart.container and restart.documents and applied.get(cid) == generation.children[cid].port
        )
        if stopping:
            self.docker.stream(
                ["compose", "-p", host.project, "-f", str(self.install / layout.RENDERED), "stop", *stopping]
            )

    def _stage(self, state: State, generation: Generation, confirmed: set[str]) -> set[str]:
        """Hand each runtime what it may read now, and return the worlds moving to a new port.
        Those get their documents in the directory for the new port, which only the container
        Compose creates for it mounts, so the running one never reads a port it does not publish.
        A world whose restart the server manager confirmed with players online is told so; any
        other runtime still waits for an empty world before restarting the game."""
        for directory in (self.store.root, self.control_dir):
            directory.chmod(0o755)
        applied = state.applied or {}
        moving: set[str] = set()
        for cid, child in generation.children.items():
            handed = state.handed.get(cid)
            if cid in applied and applied[cid] != child.port:
                self.store.publish(cid, child, generation.number, cid in confirmed)
                moving.add(cid)
            elif (
                handed is None
                or handed.number != generation.number
                or handed.child != child
                or not self.store.holds(cid, child.port)
            ):
                # Rewritten when missing too: Compose cannot mount a subpath that is not there.
                self.store.publish(cid, child, generation.number, cid in confirmed)
                state.handed[cid] = Handed(generation.number, child)
        state.generation = generation
        self.store.save(state)
        return moving

    def _commit(
        self, state: State, generation: Generation, moving: set[str], override: dict[str, object] | None
    ) -> None:
        """Once Compose has the containers in line, record what each world now runs, and any
        override of the players check with the generation it applied."""
        if override is not None:
            state.override = {"generation": generation.number, "worlds": override}
        for cid in moving:
            state.handed[cid] = Handed(generation.number, generation.children[cid])
        removed = set(state.handed) - set(generation.children)
        for cid in removed:
            del state.handed[cid]
        state.applied = {cid: child.port for cid, child in generation.children.items()}
        self.store.save(state)
        self.store.prune(state.applied, removed)

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
) -> dict[str, _Restart]:
    """The running worlds this apply restarts, by child id: those whose container changes, whose
    documents change in a way the runtime cannot adopt, or which the manifest no longer has."""
    before = (previous or {}).get("services", {})
    restarts: dict[str, _Restart] = {}
    for cid, child in generation.children.items():
        if cid not in running:
            continue
        handed = state.handed.get(cid)
        container = before.get(cid) != rendered["services"][cid]
        documents = handed is None or handed.child != child
        if container or documents:
            restarts[cid] = _Restart(child.world, container, documents)
    for cid, handed in state.handed.items():
        if cid in running and cid not in generation.children:
            restarts[cid] = _Restart(handed.child.world, container=True, documents=False)
    return restarts
