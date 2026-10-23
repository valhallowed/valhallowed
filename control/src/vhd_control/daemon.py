"""The running control plane: reload the settings, reconcile, and hand each runtime what it may have.

What a runtime may have depends on what the change costs it (Change cost):
- documents equal but for their number: always, since the runtime adopts them in place;
- anything else restarts its game, so it waits while players may be online (an empty world);
- a new world or a new port changes its container, so it waits for vhd apply.
vhd apply hands every runtime its documents itself, once the server manager has confirmed
any restart, so it is also how a waiting change is applied now.
"""

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from vhd_control import manifest
from vhd_control.manifest import InvalidSettings, Settings
from vhd_control.reconcile import Child, reconcile
from vhd_control.schemas import Schemas
from vhd_control.status import StatusBook
from vhd_control.store import State, Store

POLL_S = 2.0
WAITS_FOR_APPLY = "vhd apply"
WAITS_FOR_EMPTY = "an empty world"

_log = logging.getLogger(__name__)


@dataclass
class Daemon:
    install: Path
    store: Store
    schemas: Schemas
    book: StatusBook
    _settings: Settings | None = field(default=None, init=False)
    _stamp: tuple[int | None, ...] | None = field(default=None, init=False)
    # Why the newest files were refused, and why the settings they hold were.
    _unreadable: str | None = field(default=None, init=False)
    _refused: str | None = field(default=None, init=False)
    _pending: dict[str, tuple[int, str]] = field(default_factory=dict, init=False)
    _worlds: frozenset[str] = field(default=frozenset(), init=False)
    _failing: str | None = field(default=None, init=False)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.step()
            # A failed pass, such as a full disk, must not end the control plane: its listeners
            # keep answering, and the next pass tries again.
            except Exception as error:
                if str(error) != self._failing:
                    _log.exception("a reconcile pass failed; retrying every %s s", POLL_S)
                self._failing = str(error)
            else:
                self._failing = None
            stop.wait(POLL_S)

    def worlds(self) -> frozenset[str]:
        """The worlds that may report status: the newest generation's, and any the manifest
        dropped whose containers vhd apply has not removed yet."""
        return self._worlds

    def step(self) -> None:
        """One pass: reload changed settings, reconcile, and hand on what may go now."""
        self._reload()
        if self._settings is None:
            return
        with self.store.locked():
            state = self.store.load()
            try:
                generation = reconcile(self._settings, state.generation)
            except InvalidSettings as error:
                self._refused = self._log_refusal(str(error), self._refused)
                return
            self._refused = None
            changed = generation is not state.generation
            pending: dict[str, tuple[int, str]] = {}
            for cid, child in generation.children.items():
                waits = self._waits_for(state, cid, child)
                if waits is not None:
                    pending[cid] = (generation.number, waits)
                elif state.handed[cid].number != generation.number:
                    self.store.hand(state, cid, child, generation.number)
                    changed = True
            state.generation = generation
            if changed:
                self.store.save(state)
            self._worlds = frozenset(
                [child.world for child in generation.children.values()]
                + [handed.child.world for handed in state.handed.values()]
            )
        for cid, (number, waits) in pending.items():
            if self._pending.get(cid) != (number, waits):
                _log.info("generation %d for %s waits for %s", number, generation.children[cid].world, waits)
        self._pending = pending

    def status(self) -> dict[str, object] | None:
        """What GET /status returns (contracts/control.status.schema.json), or None before the
        first generation."""
        state = self.store.load()
        if state.generation is None:
            return None
        worlds: dict[str, object] = {}
        for cid, child in state.generation.children.items():
            view = self.book.view(child.world)
            entry: dict[str, object] = {"state": view.state}
            handed = state.handed.get(cid)
            if handed is not None:
                entry["published"] = handed.number
            pending = self._pending.get(cid)
            if pending is not None:
                entry["pending"] = {"generation": pending[0], "waits_for": pending[1]}
            if view.report is not None and view.age_s is not None:
                entry["report"] = view.report
                entry["report_age_s"] = round(view.age_s, 1)
            worlds[child.world] = entry
        for cid, handed in state.handed.items():
            if cid in state.generation.children:
                continue
            view = self.book.view(handed.child.world)
            worlds[handed.child.world] = {
                "state": view.state,
                "published": handed.number,
                "removed": True,
                "pending": {"generation": state.generation.number, "waits_for": WAITS_FOR_APPLY},
            }
        document: dict[str, object] = {"contract": 1, "generation": state.generation.number, "worlds": worlds}
        if self.refused is not None:
            document["refused"] = self.refused
        return document

    def pending(self, child_id: str) -> tuple[int, str] | None:
        return self._pending.get(child_id)

    @property
    def refused(self) -> str | None:
        """Why the newest manifest or secrets were refused, or None while they run."""
        return self._unreadable or self._refused

    def _waits_for(self, state: State, child_id: str, child: Child) -> str | None:
        handed = state.handed.get(child_id)
        if state.applied is None or state.applied.get(child_id) != child.port or handed is None:
            return WAITS_FOR_APPLY
        if handed.child == child or self.book.view(child.world).empty:
            return None
        return WAITS_FOR_EMPTY

    def _reload(self) -> None:
        stamp = tuple(_mtime(self.install / name) for name in (manifest.MANIFEST_FILE, manifest.SECRETS_FILE))
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            self._settings = manifest.load(self.install, self.schemas)
        except InvalidSettings as error:
            self._unreadable = self._log_refusal(str(error), self._unreadable)
            return
        self._unreadable = None
        _log.info("settings read from %s", self.install)

    @staticmethod
    def _log_refusal(reason: str, before: str | None) -> str:
        if reason != before:
            _log.error("refused: %s; the last good generation keeps running", reason)
        return reason


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
