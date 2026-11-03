"""What each runtime and each data-layer service last reported (B1), and how old that report is."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vhd_control.reconcile import STATUS_INTERVAL_S

# A child silent for this many of its reporting intervals is unknown, never its last state.
STALE_AFTER = 3
# Only a report no older than this many intervals vouches for an empty world: one arrives every
# interval, give or take its delivery, and an older one may predate a player who joined since.
EMPTY_WITHIN = 1.5
# States in which no game process runs, so no player can be connected.
NO_GAME = frozenset({"installing", "held"})


@dataclass(frozen=True)
class WorldView:
    state: str
    report: dict[str, Any] | None
    age_s: float | None

    @property
    def empty(self) -> bool:
        """Whether status vouches that no player is connected now. Unknown is never empty, and
        neither is a world whose newest report is older than EMPTY_WITHIN intervals."""
        if self.state == "unknown" or self.report is None:
            return False
        if self.age_s is None or self.age_s > EMPTY_WITHIN * STATUS_INTERVAL_S:
            return False
        players = self.report.get("players")
        return players == 0 if players is not None else self.state in NO_GAME


@dataclass(frozen=True)
class ServiceView:
    state: str
    report: dict[str, Any] | None
    age_s: float | None


class StatusBook:
    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        # By ("world", save name) or ("service", service name).
        self._reports: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}

    def record(self, report: dict[str, Any]) -> None:
        """Keep a validated report as its world's or service's latest."""
        key = ("world", report["world"]) if "world" in report else ("service", report["service"])
        with self._lock:
            self._reports[key] = (report, self._now())

    def view(self, world: str) -> WorldView:
        return WorldView(*self._latest(("world", world)))

    def service(self, name: str) -> ServiceView:
        return ServiceView(*self._latest(("service", name)))

    def _latest(self, key: tuple[str, str]) -> tuple[str, dict[str, Any] | None, float | None]:
        with self._lock:
            entry = self._reports.get(key)
        if entry is None:
            return "unknown", None, None
        report, received = entry
        age = self._now() - received
        state = "unknown" if age > STALE_AFTER * STATUS_INTERVAL_S else str(report["state"])
        return state, report, age
