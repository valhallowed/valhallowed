"""The control plane's two listeners.

- Status (B1): runtimes POST their reports on the install network. It accepts status and
  nothing else, so a runtime, which faces players, can reach no operator action, and it takes
  a world's report only from that world's own container, so no runtime can speak for another.
- The control API (I0): GET /status on a Unix socket in a volume only the control plane
  and vhd mount, so no child and no network reaches it.
"""

import json
import logging
import socket
import socketserver
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from vhd_control.daemon import Daemon
from vhd_control.naming import child_id
from vhd_control.schemas import Schemas
from vhd_control.status import StatusBook

STATUS_SCHEMA = "contracts/game-runtime.status.schema.json"
MAX_BODY = 16 * 1024
TIMEOUT_S = 5

_log = logging.getLogger(__name__)


def _resolve(name: str) -> set[str]:
    return {str(info[4][0]) for info in socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)}


class Senders:
    """Which address may report for a world: the one Docker's DNS gives the world's service on
    the install network, asked on every report. A container Compose recreates gets a new
    address, and its old one can pass to another world's container, so none is remembered."""

    def __init__(self, resolve: Callable[[str], set[str]] = _resolve) -> None:
        self._resolve = resolve

    def allows(self, world: str, address: str) -> bool:
        try:
            return address in self._resolve(child_id(world))
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


def status_server(
    book: StatusBook, schemas: Schemas, senders: Senders, address: tuple[str, int]
) -> ThreadingHTTPServer:
    class Handler(_Handler):
        def do_POST(self) -> None:
            if self.path != "/status":
                self.reply(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.reply(HTTPStatus.LENGTH_REQUIRED)
                return
            if not 0 < length <= MAX_BODY:
                self.reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            try:
                report = json.loads(self.rfile.read(length))
            except ValueError:
                self.reply(HTTPStatus.BAD_REQUEST, {"error": "not JSON"})
                return
            problem = schemas.problem(STATUS_SCHEMA, report)
            if problem is not None:
                # Observations fail soft: the report is dropped, and the runtime runs on.
                _log.warning("dropped a status report %s", problem)
                self.reply(HTTPStatus.BAD_REQUEST, {"error": problem})
                return
            if not senders.allows(report["world"], self.client_address[0]):
                _log.warning(
                    "dropped a status report for %s from %s, not its container", report["world"], self.client_address[0]
                )
                self.reply(HTTPStatus.FORBIDDEN, {"error": "a world reports only from its own container"})
                return
            book.record(report)
            self.reply(HTTPStatus.NO_CONTENT)

    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    return server


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
