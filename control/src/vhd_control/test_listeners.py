import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from vhd_control import client, listeners
from vhd_control.listeners import MAX_BODY, control_server, status_server
from vhd_control.naming import child_id
from vhd_control.status import StatusBook
from vhd_control.test_daemon import SCHEMAS, Install

REPORT = {"contract": 1, "generation": 1, "world": "Meadowfold", "state": "running", "objects": 10, "players": 2}


class Resolver:
    """Docker's DNS, as the control plane sees it: Meadowfold's container is this test."""

    def __init__(self) -> None:
        self.addresses = {child_id("Meadowfold"): {"127.0.0.1"}, child_id("Ashwake"): {"192.0.2.10"}}

    def __call__(self, name: str) -> set[str]:
        if name not in self.addresses:
            raise OSError(f"no such service: {name}")
        return self.addresses[name]


WORLDS = frozenset({"Meadowfold", "Ashwake"})


class Listener:
    def __init__(self, resolver: Resolver) -> None:
        self.book = StatusBook()
        self.resolver = resolver
        self.server = status_server(self.book, SCHEMAS, lambda: WORLDS, ("127.0.0.1", 0), resolve=resolver)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/status"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def status_url() -> Iterator[tuple[str, StatusBook]]:
    listener = Listener(Resolver())
    try:
        yield listener.url, listener.book
    finally:
        listener.close()


def post(url: str, body: bytes) -> int:
    """The status code, or 0 when the server closed the connection unanswered."""
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")  # noqa: S310 - a local test server
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, ConnectionError):
        return 0


def test_a_valid_report_is_recorded(status_url: tuple[str, StatusBook]) -> None:
    url, book = status_url
    assert post(url, json.dumps(REPORT).encode()) == 204
    view = book.view("Meadowfold")
    assert view.report == REPORT


def test_a_report_that_breaks_the_contract_is_dropped(status_url: tuple[str, StatusBook]) -> None:
    url, book = status_url
    assert post(url, json.dumps({**REPORT, "state": "healthy"}).encode()) == 400
    assert post(url, b"{") == 400
    assert book.view("Meadowfold").report is None


def test_a_runtime_cannot_report_for_another_world(status_url: tuple[str, StatusBook]) -> None:
    url, book = status_url
    spoof = {**REPORT, "world": "Ashwake", "players": 0}
    assert post(url, json.dumps(spoof).encode()) == 403
    assert post(url, json.dumps({**REPORT, "world": "Elsewhere"}).encode()) == 403
    assert book.view("Ashwake").report is None


def test_a_report_follows_a_container_docker_recreated() -> None:
    resolver = Resolver()
    listener = Listener(resolver)
    try:
        assert post(listener.url, json.dumps(REPORT).encode()) == 204
        resolver.addresses[child_id("Meadowfold")] = {"192.0.2.20"}
        assert post(listener.url, json.dumps(REPORT).encode()) == 403
    finally:
        listener.close()


def test_a_world_the_install_does_not_run_never_reaches_dns() -> None:
    resolver = Resolver()
    asked: list[str] = []
    listener = Listener(lambda name: asked.append(name) or resolver(name))  # type: ignore[arg-type,func-returns-value]
    try:
        assert post(listener.url, json.dumps({**REPORT, "world": "Elsewhere"}).encode()) == 403
    finally:
        listener.close()
    assert asked == []


def test_reports_beyond_the_handler_limit_are_turned_away(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(listeners, "MAX_HANDLERS", 1)
    listener = Listener(Resolver())
    port = listener.server.server_address[1]
    try:
        with socket.create_connection(("127.0.0.1", port)) as idle:
            idle.sendall(b"POST /status HTTP/1.1\r\n")
            assert post(listener.url, json.dumps(REPORT).encode()) == 0
        # The idle connection's thread frees its slot once it sees the close.
        codes = []
        for _ in range(50):
            codes.append(post(listener.url, json.dumps(REPORT).encode()))
            if codes[-1] == 204:
                break
            time.sleep(0.05)
        assert codes[-1] == 204
    finally:
        listener.close()


def test_an_oversized_report_is_refused_unread(status_url: tuple[str, StatusBook]) -> None:
    url, _ = status_url
    assert post(url, b" " * (MAX_BODY + 1)) == 413


def test_the_status_listener_takes_status_and_nothing_else(status_url: tuple[str, StatusBook]) -> None:
    url, _ = status_url
    assert post(url.replace("/status", "/generation"), json.dumps(REPORT).encode()) == 404


def test_the_control_api_serves_status_over_its_socket(tmp_path: Path) -> None:
    install = Install(tmp_path)
    install.players("Meadowfold", 2)
    manifest = (install.folder / "manifest.toml").read_text()
    install.write(manifest, 'schema = 1\n\n[world.Meadowfold]\npassword = "hearth-and-horn"\n')
    install.daemon.step()
    socket_path = tmp_path / "control.sock"
    server = control_server(install.daemon, socket_path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        document = client.status(socket_path)
    finally:
        server.shutdown()
        server.server_close()

    assert SCHEMAS.problem("contracts/control.status.schema.json", document) is None
    meadowfold = document["worlds"]["Meadowfold"]
    assert meadowfold["pending"] == {"generation": 2, "waits_for": "an empty world"}
    assert meadowfold["report"]["players"] == 2
    assert document["worlds"]["Ashwake"]["state"] == "unknown"
    assert socket_path.stat().st_mode & 0o777 == 0o600


def test_the_client_says_when_the_control_plane_is_not_running(tmp_path: Path) -> None:
    with pytest.raises(client.Unreachable, match="does not answer"):
        client.status(tmp_path / "control.sock")
