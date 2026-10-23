"""The control API's client, for vhd: HTTP over the control plane's Unix socket."""

import http.client
import json
import socket
from pathlib import Path
from typing import Any

TIMEOUT_S = 10


class Unreachable(Exception):
    """The control plane is not running, did not answer, or answered with an error."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("control-plane", timeout=TIMEOUT_S)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT_S)
        sock.connect(str(self._path))
        self.sock = sock


def status(path: Path) -> dict[str, Any]:
    """GET /status (contracts/control.status.schema.json)."""
    connection = _UnixConnection(path)
    try:
        connection.request("GET", "/status")
        response = connection.getresponse()
        body = response.read()
    except OSError as error:
        raise Unreachable(f"the control plane does not answer on {path}: {error}") from error
    finally:
        connection.close()
    if response.status != http.client.OK:
        raise Unreachable(f"the control plane answered {response.status}: {body.decode(errors='replace')}")
    document: dict[str, Any] = json.loads(body)
    return document
