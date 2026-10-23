"""vhd apply: bring the install's containers in line with its manifest (G13).

The one place that holds Docker's socket, for its run alone. Two rules shape it: restarting a
world that may have players online takes the server manager's confirmation, and
.vhd/compose.json names what Compose last ran successfully, so a failed or interrupted run
leaves both the install and the next run's players check as they were.
"""

import fcntl
import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, TextIO

from vhd_control import client, layout, manifest
from vhd_control.docker import Docker, DockerError
from vhd_control.manifest import Settings
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
APPLY_LOCK = ".apply.lock"
IGNORE_ORPHANS = "COMPOSE_IGNORE_ORPHANS"


class Refused(Exception):
    """vhd apply will not go ahead. The message says why, and what to do instead."""


@dataclass(frozen=True)
class _Restart:
    world: str
    # Compose recreates or removes its container.
    container: bool
    # Its runtime gets documents it cannot adopt without restarting the game.
    documents: bool


@dataclass(frozen=True)
class _Before:
    """What the install ran before this apply, to bring back if it fails."""

    generation: Generation | None
    handed: dict[str, Handed]
    applied: dict[str, int]


@dataclass
class Apply:
    install: Path
    generations: Path
    control_dir: Path
    schemas: Schemas
    docker: Docker
    status: Callable[[], dict[str, Any]]
    hostname: str
    out: TextIO
    store: Store = field(init=False)

    def run(self, disconnect_players: bool) -> None:
        settings = manifest.load(self.install, self.schemas)
        self._check_engine()
        host = self._host()
        # vhd runs as root; everything it writes belongs to whom the control plane runs as.
        self.store = Store(self.generations, host.owner)
        with self._only_run():
            self._apply(settings, host, disconnect_players)

    @contextmanager
    def _only_run(self) -> Iterator[None]:
        """Hold vhd apply's own lock for the whole run, so two never interleave. Unlike the volume's
        lock it is not waited for: a second vhd apply is refused at once."""
        with (self.generations / APPLY_LOCK).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Refused("another vhd apply is running on this install; wait for it to finish") from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _apply(self, settings: Settings, host: Host, disconnect_players: bool) -> None:
        self._quiet_orphans(host.owner)
        # Before the players check, not after it: a pull can take minutes.
        self._pull(host)
        running = self._running(host.project)
        previous = json.loads(self._applied.read_text()) if self._applied.exists() else None
        worlds = self._worlds()
        next_file = self.install / layout.NEXT
        failure: Exception | None = None
        with self.store.locked():
            state = self.store.load()
            before = _Before(state.generation, dict(state.handed), dict(state.applied or {}))
            generation = reconcile(settings, state.generation)
            rendered = render(generation, host, settings.web_map_port)
            recreated = _recreated(rendered, previous)
            restarts = _restarts(generation, state, recreated, running, worlds)
            override = self._confirm(restarts, disconnect_players, worlds) if restarts else None
            confirmed = {cid for cid, restart in restarts.items() if override and restart.world in override}
            self._write(next_file, rendered, host.owner)
            try:
                self._stop_recreated(host, state, generation, restarts)
                moving = self._stage(state, generation, confirmed)
                self._own(host.owner)
            # Whatever failed, worlds may already be stopped or hold new documents.
            except Exception as error:
                failure = error
        if failure is None:
            # Flushed, so it comes before Compose's own output.
            print(
                f"vhd apply: generation {generation.number} for {len(generation.children)} world(s)",
                file=self.out,
                flush=True,
            )
            try:
                self.docker.stream(self._up(host, next_file))
            except Exception as error:
                failure = error
        if failure is not None:
            next_file.unlink(missing_ok=True)
            self._roll_back(host, previous is not None, before, generation, failure)
        next_file.replace(self._applied)
        with self.store.locked():
            self._commit(self.store.load(), generation, moving, override)

    def _quiet_orphans(self, owner: tuple[int, int]) -> None:
        """The host's Compose sees the containers vhd apply renders as orphans of compose.yaml,
        and every docker compose run warns, suggesting --remove-orphans, which would delete them.
        The install's .env tells it not to, unless the server manager already set the key."""
        env = self.install / ".env"
        lines = env.read_text().splitlines() if env.exists() else []
        if any(line.split("=", 1)[0].strip() == IGNORE_ORPHANS for line in lines):
            return
        lines.append(f"{IGNORE_ORPHANS}=true")
        temporary = env.with_name(".env.tmp")
        temporary.write_text("\n".join(lines) + "\n")
        os.chown(temporary, *owner)
        temporary.replace(env)

    @property
    def _applied(self) -> Path:
        """The compose file Compose last ran successfully."""
        return self.install / layout.RENDERED

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

    def _roll_back(
        self, host: Host, ran_before: bool, before: "_Before", generation: Generation, error: Exception
    ) -> NoReturn:
        """Something failed once worlds may have been stopped or handed documents: bring the install
        back as it ran. Each world gets back the documents it held, since a runtime from the
        previous release may refuse the new ones, and Compose runs the compose file it last ran
        successfully, which vhd apply has not touched."""
        if not ran_before:
            raise error
        print("vhd apply: failed; bringing back the install as it ran before", file=self.out, flush=True)
        with self.store.locked():
            state = self.store.load()
            for cid, handed in before.handed.items():
                if before.applied.get(cid) == handed.child.port:
                    self.store.publish(cid, handed.child, handed.number)
            state.generation, state.handed = before.generation, dict(before.handed)
            self.store.save(state)
            self.store.prune(before.applied, set(generation.children) - set(before.handed))
        try:
            self.docker.stream(self._up(host, self._applied))
        except DockerError as again:
            raise DockerError(f"{error}; bringing back the previous install failed too: {again}") from again
        raise Refused(
            f"vhd apply could not apply generation {generation.number} ({error}). The install is back as "
            "it ran before; fix the cause, then run vhd apply again."
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

    def _worlds(self) -> dict[str, Any]:
        """What status says of each world, or nothing when the control plane does not answer:
        then no world can be vouched for."""
        try:
            worlds: dict[str, Any] = self.status()["worlds"]
        except client.Unreachable:
            return {}
        return worlds

    def _confirm(
        self, restarts: Mapping[str, _Restart], disconnect_players: bool, worlds: Mapping[str, Any]
    ) -> dict[str, object] | None:
        """Refuse to restart a world that may have players online, unless told to. Told to, it
        returns the override to record with the change: each such world's player count, or None
        where status could not say."""
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
            self.docker.stream(["compose", "-p", host.project, "-f", str(self._applied), "stop", *stopping])

    def _stage(self, state: State, generation: Generation, confirmed: set[str]) -> set[str]:
        """Hand each runtime what it may read now, and return the worlds moving to a new port.
        Those get their documents in the directory for the new port, which only the container
        Compose creates for it mounts, so the running one never reads a port it does not publish.
        A world whose restart the server manager confirmed gets its documents again, saying so,
        which also frees a runtime waiting for an empty world; any other runtime still waits."""
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
                cid in confirmed
                or handed is None
                or handed.number != generation.number
                or handed.child != child
                # Rewritten when missing too: Compose cannot mount a subpath that is not there.
                or not self.store.holds(cid, child.port)
            ):
                self.store.hand(state, cid, child, generation.number, cid in confirmed)
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


def _recreated(rendered: dict[str, Any], previous: dict[str, Any] | None) -> set[str]:
    """The services whose definition changes, which Compose recreates."""
    before = (previous or {}).get("services", {})
    return {name for name, service in rendered["services"].items() if before.get(name) != service}


def _restarts(
    generation: Generation, state: State, recreated: set[str], running: set[str], worlds: Mapping[str, Any]
) -> dict[str, _Restart]:
    """The running worlds this apply restarts, by child id: those whose container changes, whose
    documents change in a way the runtime cannot adopt, whose runtime still waits on documents it
    was handed, or which the manifest no longer has."""
    restarts: dict[str, _Restart] = {}
    for cid, child in generation.children.items():
        if cid not in running:
            continue
        handed = state.handed.get(cid)
        container = cid in recreated
        runs = worlds.get(child.world, {}).get("report", {}).get("generation")
        waiting = handed is not None and runs is not None and runs < handed.number and handed.child == child
        documents = handed is None or handed.child != child or waiting
        if container or documents:
            restarts[cid] = _Restart(child.world, container, documents)
    for cid, handed in state.handed.items():
        if cid in running and cid not in generation.children:
            restarts[cid] = _Restart(handed.child.world, container=True, documents=False)
    return restarts
