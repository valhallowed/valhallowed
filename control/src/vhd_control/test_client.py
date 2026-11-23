import http.client
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from vhd_control import client
from vhd_control.test_daemon import BACKUP, SCHEMAS

REPLY = {"contract": 1, "id": "a" * 32, "world": "Meadowfold", "backup": BACKUP}


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class Canned:
    """A control plane that answers every POST the same way, and keeps what it was sent."""

    def __init__(self, path: Path, status: int, body: object) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.received: list[object] = []
        canned = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                canned.received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = _Server(str(path), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def socket_path(tmp_path: Path) -> Path:
    return tmp_path / "control.sock"


def test_a_restore_the_control_plane_holds_is_returned_as_it_replied(socket_path: Path) -> None:
    control = Canned(socket_path, http.client.ACCEPTED, REPLY)
    try:
        assert client.restore(socket_path, SCHEMAS, "Meadowfold", BACKUP) == REPLY
    finally:
        control.close()
    assert control.received == [{"contract": 1, "world": "Meadowfold", "backup": BACKUP}]


@pytest.mark.parametrize("status", [400, 404, 409, 503])
def test_a_refusal_reaches_the_caller_with_the_reason(socket_path: Path, status: int) -> None:
    control = Canned(socket_path, status, {"error": "the install runs no world Hollowmere"})
    try:
        with pytest.raises(client.Refused, match="the install runs no world Hollowmere"):
            client.restore(socket_path, SCHEMAS, "Hollowmere", BACKUP)
    finally:
        control.close()


def test_a_refusal_without_a_reason_still_says_its_status(socket_path: Path) -> None:
    control = Canned(socket_path, 503, {})
    try:
        with pytest.raises(client.Refused, match="answered 503"):
            client.restore(socket_path, SCHEMAS, "Meadowfold", BACKUP)
    finally:
        control.close()


@pytest.mark.parametrize("status", [202, 409])
def test_an_answer_that_is_not_json_is_unreachable(socket_path: Path, status: int) -> None:
    control = Canned(socket_path, status, b"<html>bad gateway</html>")
    try:
        with pytest.raises(client.Unreachable, match="not in JSON"):
            client.restore(socket_path, SCHEMAS, "Meadowfold", BACKUP)
    finally:
        control.close()


@pytest.mark.parametrize("reply", [{**REPLY, "id": "not-an-id"}, {**REPLY, "extra": 1}, {"contract": 1}])
def test_a_reply_that_breaks_its_contract_is_unreachable(socket_path: Path, reply: dict[str, object]) -> None:
    control = Canned(socket_path, http.client.ACCEPTED, reply)
    try:
        with pytest.raises(client.Unreachable, match="breaks its contract"):
            client.restore(socket_path, SCHEMAS, "Meadowfold", BACKUP)
    finally:
        control.close()


def test_no_control_plane_on_the_socket_is_unreachable(socket_path: Path) -> None:
    with pytest.raises(client.Unreachable, match="did not answer"):
        client.restore(socket_path, SCHEMAS, "Meadowfold", BACKUP)


def test_a_connection_closed_unanswered_says_the_restore_may_have_been_taken(socket_path: Path) -> None:
    class Hangup(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.recv(4096)

    server = _Server(str(socket_path), Hangup)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(client.Unreachable, match="may have taken the restore"):
            client.restore(socket_path, SCHEMAS, "Meadowfold", BACKUP)
    finally:
        server.shutdown()
        server.server_close()
