"""The control plane's two listeners.

- Status (B1): runtimes POST their reports on the install network. It accepts status and
  nothing else, so a runtime, which faces players, can reach no operator action, and it takes
  a world's report only from that world's own container, so no runtime can speak for another.
  The sender check trusts source addresses, which holds while no child can forge one: every
  child runs without capabilities (render.LOCKED_DOWN), NET_RAW included.
- The control API (I0): GET /status on a Unix socket in a volume only the control plane
  and vhd mount, so no child and no network reaches it.
"""

import json
import logging
import socket
import socketserver
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Collection
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from vhd_control.daemon import Daemon
from vhd_control.naming import child_id
from vhd_control.schemas import Schemas
from vhd_control.status import StatusBook

STATUS_SCHEMA = "contracts/game-runtime.status.schema.json"
MAX_BODY = 16 * 1024
# A whole request, from connect to reply, gets this long.
TIMEOUT_S = 5
# Requests handled at once, and from one sender. A runtime sends one report every few seconds;
# more than this at once is turned away rather than given a thread each.
MAX_HANDLERS = 32
PER_SENDER = 4
# How often turned-away connections are logged, with their count since.
DROPS_LOGGED_EVERY_S = 60

Resolve = Callable[[str], set[str]]

_log = logging.getLogger(__name__)


def _resolve(name: str) -> set[str]:
    return {str(info[4][0]) for info in socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)}


def _sent_from(world: str, address: str, resolve: Resolve) -> bool:
    """Whether address is the one Docker's DNS gives the world's service on the install
    network, asked on every report. A container Compose recreates gets a new address, and its
    old one can pass to another world's container, so none is remembered."""
    try:
        return address in resolve(child_id(world))
    except OSError:
        return False


class _Handler(BaseHTTPRequestHandler):
    timeout = TIMEOUT_S

    def setup(self) -> None:
        super().setup()
        # The socket's timeout bounds each read; this bounds the whole request, so a sender
        # trickling its request line, headers or body holds a thread no longer.
        self._deadline = threading.Timer(TIMEOUT_S, self._expire)
        self._deadline.start()

    def finish(self) -> None:
        self._deadline.cancel()
        super().finish()

    def _expire(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def reply(self, code: HTTPStatus, document: object | None = None) -> None:
        body = b"" if document is None else json.dumps(document).encode()
        self.send_response(code)
        if document is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        _log.debug(format, *args)

    def read_json(self) -> object | None:
        """The request's JSON body, or None once it has replied why there is none."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.reply(HTTPStatus.LENGTH_REQUIRED)
            return None
        if not 0 < length <= MAX_BODY:
            self.reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        body = self.rfile.read(length)
        if len(body) < length:
            # The sender hung up, or the request ran out of time.
            return None
        try:
            document: object = json.loads(body)
        except ValueError:
            self.reply(HTTPStatus.BAD_REQUEST, {"error": "not JSON"})
            return None
        return document


class _StatusServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], handler: type[_Handler]) -> None:
        super().__init__(address, handler)
        self._lock = threading.Lock()
        self._slots = MAX_HANDLERS
        self._by_sender: Counter[str] = Counter()
        self._turned_away = 0
        self._logged_at = 0.0

    # Any: typeshed's request type is private to socketserver.
    def process_request(self, request: Any, client_address: Any) -> None:
        sender = str(client_address[0])
        with self._lock:
            admitted = self._slots > 0 and self._by_sender[sender] < PER_SENDER
            if admitted:
                self._slots -= 1
                self._by_sender[sender] += 1
            else:
                self._turn_away()
        if not admitted:
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(sender)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(str(client_address[0]))

    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        # A sender that hangs up or runs out of time is routine; anything else is a fault here.
        if isinstance(error, OSError):
            _log.debug("status request from %s ended: %s", client_address, error)
        else:
            _log.warning("status request from %s failed", client_address, exc_info=True)

    def _release(self, sender: str) -> None:
        with self._lock:
            self._slots += 1
            self._by_sender[sender] -= 1
            if self._by_sender[sender] <= 0:
                del self._by_sender[sender]

    def _turn_away(self) -> None:
        """Count a connection turned away, logging the count at most once a minute."""
        self._turned_away += 1
        now = time.monotonic()
        if now - self._logged_at >= DROPS_LOGGED_EVERY_S:
            _log.warning("turned away %d status connection(s): too many at once", self._turned_away)
            self._turned_away, self._logged_at = 0, now


def status_server(
    book: StatusBook,
    schemas: Schemas,
    worlds: Callable[[], Collection[str]],
    address: tuple[str, int],
    *,
    resolve: Resolve = _resolve,
) -> ThreadingHTTPServer:
    # Why each sender's reports were last dropped, so a runtime sending the same bad report every
    # few seconds is logged once, and again only when the reason changes or after a good report.
    dropping: dict[str, str] = {}
    lock = threading.Lock()

    def drop(sender: str, why: str) -> None:
        with lock:
            if dropping.get(sender) == why:
                return
            dropping[sender] = why
        _log.warning("dropped status reports from %s: %s", sender, why)

    class Handler(_Handler):
        def do_POST(self) -> None:
            if self.path != "/status":
                self.reply(HTTPStatus.NOT_FOUND)
                return
            document = self.read_json()
            if document is None:
                return
            sender = self.client_address[0]
            problem = schemas.problem(STATUS_SCHEMA, document)
            if problem is not None:
                # Observations fail soft: the report is dropped, and the runtime runs on.
                drop(sender, f"a report {problem}")
                self.reply(HTTPStatus.BAD_REQUEST, {"error": problem})
                return
            report = cast(dict[str, Any], document)
            # The world first: a name the install does not run never reaches Docker's DNS.
            world = report["world"]
            if world not in worlds():
                drop(sender, f"reports for {world}, which the install does not run")
                self.reply(HTTPStatus.FORBIDDEN, {"error": "the install runs no such world"})
                return
            if not _sent_from(world, sender, resolve):
                drop(sender, f"reports for {world}, from outside its container")
                self.reply(HTTPStatus.FORBIDDEN, {"error": "a world reports only from its own container"})
                return
            with lock:
                dropping.pop(sender, None)
            book.record(report)
            self.reply(HTTPStatus.NO_CONTENT)

    return _StatusServer(address, Handler)


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def control_server(daemon: Daemon, path: Path) -> _UnixServer:
    class Handler(_Handler):
        def do_GET(self) -> None:
            if self.path != "/status":
                self.reply(HTTPStatus.NOT_FOUND)
                return
            status = daemon.status()
            if status is None:
                self.reply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no generation yet; run vhd apply"})
                return
            self.reply(HTTPStatus.OK, status)

    path.unlink(missing_ok=True)
    server = _UnixServer(str(path), Handler)
    path.chmod(0o600)
    return server
