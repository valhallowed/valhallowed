import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from vhd_control import client
from vhd_control.listeners import MAX_BODY, Senders, control_server, status_server
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


@pytest.fixture
def status_url() -> Iterator[tuple[str, StatusBook]]:
    book = StatusBook()
    server = status_server(book, SCHEMAS, Senders(Resolver()), ("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/status", book
    finally:
        server.shutdown()
        server.server_close()


def post(url: str, body: bytes) -> int:
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")  # noqa: S310 - a local test server
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as error:
        return error.code


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


def test_senders_follow_a_container_docker_recreated() -> None:
    resolver = Resolver()
    senders = Senders(resolver)
    assert senders.allows("Meadowfold", "127.0.0.1")
    resolver.addresses[child_id("Meadowfold")] = {"192.0.2.20"}
    assert senders.allows("Meadowfold", "192.0.2.20")
    assert not senders.allows("Meadowfold", "127.0.0.1")


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
