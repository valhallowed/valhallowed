"""World truth as the plugin reports it, and the runtime's status sent up to the control plane (B1)."""

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.request import Request, urlopen

REPORT_TIMEOUT_S = 5

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorldStatus:
    objects: int
    players: int
    # Which write of the file this is. The plugin replaces the file on every write, so each
    # write has its own inode, and its own mtime even where inodes are reused.
    stamp: tuple[int, int]


class RuntimeStatus(TypedDict):
    """One status report, as contracts/game-runtime.status.schema.json defines it."""

    contract: int
    generation: int
    world: str
    state: str
    reason: NotRequired[str]
    game_build: NotRequired[str]
    objects: NotRequired[int]
    players: NotRequired[int]


Post = Callable[[str, bytes, float], None]


def read_world(path: Path, world: str) -> WorldStatus | None:
    """The plugin's last report on the loaded world, or None if it has written none for this world."""
    try:
        with path.open() as file:
            info = os.fstat(file.fileno())
            data = json.loads(file.read())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("world") != world:
        return None
    objects, players = data.get("objects"), data.get("players")
    if not isinstance(objects, int) or not isinstance(players, int) or objects < 0 or players < 0:
        return None
    return WorldStatus(objects=objects, players=players, stamp=(info.st_ino, info.st_mtime_ns))


def post(url: str, body: bytes, timeout: float) -> None:
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")  # noqa: S310
    with urlopen(request, timeout=timeout):  # noqa: S310 - the generation's own control-plane URL
        pass


class Reporter:
    """Sends status to the control plane. Observations fail soft: a failed report
    is logged once and never stops the world."""

    def __init__(self, url: str | None, post: Post = post) -> None:
        self._url = url
        self._post = post
        self._failing = False

    def send(self, status: RuntimeStatus) -> None:
        _log.info("status %s", json.dumps(status, sort_keys=True))
        if self._url is None:
            return
        try:
            self._post(self._url, json.dumps(status).encode(), REPORT_TIMEOUT_S)
        except OSError as error:
            if not self._failing:
                _log.warning("cannot report status to %s: %s", self._url, error)
            self._failing = True
            return
        if self._failing:
            _log.info("status reports reach %s again", self._url)
        self._failing = False
