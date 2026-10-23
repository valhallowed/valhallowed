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
import threading
import time
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
TIMEOUT_S = 5
# Reports handled at once. A runtime sends one every few seconds; more than this at once is
# turned away rather than given a thread each.
MAX_HANDLERS = 32

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
        body = self.read_body(length)
        if body is None:
            self.reply(HTTPStatus.REQUEST_TIMEOUT)
            return None
        try:
            document: object = json.loads(body)
        except ValueError:
            self.reply(HTTPStatus.BAD_REQUEST, {"error": "not JSON"})
            return None
        return document

    def read_body(self, length: int) -> bytes | None:
        """The body, or None if it does not arrive whole within TIMEOUT_S: the socket's timeout
        bounds each read, and a sender trickling bytes must not hold a thread for longer."""
        deadline = time.monotonic() + TIMEOUT_S
        body = b""
        while len(body) < length:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.connection.settimeout(left)
            try:
                chunk = self.rfile.read1(length - len(body))
            except TimeoutError:
                return None
            if not chunk:
                return None
            body += chunk
        return body


class _StatusServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], handler: type[_Handler]) -> None:
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(MAX_HANDLERS)

    # Any: typeshed's request type is private to socketserver.
    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A sender that hangs up mid-request is routine; socketserver would print a traceback.
        _log.debug("status request from %s ended in error", client_address, exc_info=True)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def status_server(
    book: StatusBook,
    schemas: Schemas,
    worlds: Callable[[], Collection[str]],
    address: tuple[str, int],
    *,
    resolve: Resolve = _resolve,
) -> ThreadingHTTPServer:
    class Handler(_Handler):
        def do_POST(self) -> None:
            if self.path != "/status":
                self.reply(HTTPStatus.NOT_FOUND)
                return
            document = self.read_json()
            if document is None:
                return
            problem = schemas.problem(STATUS_SCHEMA, document)
            if problem is not None:
                # Observations fail soft: the report is dropped, and the runtime runs on.
                _log.warning("dropped a status report %s", problem)
                self.reply(HTTPStatus.BAD_REQUEST, {"error": problem})
                return
            report = cast(dict[str, Any], document)
            # The world first: a name the install does not run never reaches Docker's DNS.
            world = report["world"]
            if world not in worlds() or not _sent_from(world, self.client_address[0], resolve):
                _log.warning("dropped a status report for %s from %s, not its container", world, self.client_address[0])
                self.reply(HTTPStatus.FORBIDDEN, {"error": "a world reports only from its own container"})
                return
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
