"""A service's status, sent up to the control plane's status listener (B1)."""

import http.client
import json
import logging
import threading
from collections.abc import Callable
from typing import NotRequired, TypedDict
from urllib.request import Request, urlopen

REPORT_TIMEOUT_S = 5
# The longest reason the contract takes.
MAX_REASON = 500

_log = logging.getLogger(__name__)


class ServiceStatus(TypedDict):
    """One status report, as contracts/service.status.schema.json defines it."""

    contract: int
    generation: int
    service: str
    state: str
    reason: NotRequired[str]
    accepted: NotRequired[int]
    dropped: NotRequired[int]


Post = Callable[[str, bytes, float], None]


def post(url: str, body: bytes, timeout: float) -> None:
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")  # noqa: S310
    with urlopen(request, timeout=timeout):  # noqa: S310 - the generation's own control-plane URL
        pass


def reason(text: str) -> str:
    """text as the contract takes a reason: one line of printable characters, cut to fit."""
    line = "".join(c if c.isprintable() else " " for c in text).strip()
    return (line or "no reason given")[:MAX_REASON]


class Reporter:
    """Sends status to the control plane. Observations fail soft: a failed report is logged once
    and never stops the service."""

    def __init__(self, post: Post = post) -> None:
        self._post = post
        self._failing = False
        self._logged: ServiceStatus | None = None

    def send(self, url: str, status: ServiceStatus) -> None:
        # Logged when it changes: a service sends the same report every few seconds for weeks.
        if status != self._logged:
            _log.info("status %s", json.dumps(status, sort_keys=True))
            self._logged = status
        try:
            self._post(url, json.dumps(status).encode(), REPORT_TIMEOUT_S)
        except (OSError, http.client.HTTPException) as error:
            if not self._failing:
                _log.warning("cannot report status to %s: %s", url, error)
            self._failing = True
            return
        if self._failing:
            _log.info("status reports reach %s again", url)
        self._failing = False


class State:
    """What a service says of itself, set by its threads and read by its reporter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "starting"
        self._reason: str | None = None

    def set(self, state: str, why: str | None = None) -> None:
        with self._lock:
            self._state, self._reason = state, None if why is None else reason(why)

    def get(self) -> tuple[str, str | None]:
        with self._lock:
            return self._state, self._reason
