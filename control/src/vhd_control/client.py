"""The control API's client, for vhd: HTTP over the control plane's Unix socket."""

import http.client
import json
import socket
from pathlib import Path
from typing import Any

from vhd_control.schemas import Schemas

TIMEOUT_S = 10
STATUS_CONTRACT = "contracts/control.status.schema.json"


class Unreachable(Exception):
    """The control plane is not running, did not answer, answered with an error, or answered with
    a document that breaks its contract. Either way it vouches for no world."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("control-plane", timeout=TIMEOUT_S)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT_S)
        sock.connect(str(self._path))
        self.sock = sock


def status(path: Path, schemas: Schemas) -> dict[str, Any]:
    """GET /status, held to its contract."""
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
    try:
        document: dict[str, Any] = json.loads(body)
    except ValueError as error:
        raise Unreachable(f"the control plane's status is not JSON: {error}") from error
    problem = schemas.problem(STATUS_CONTRACT, document)
    if problem is not None:
        raise Unreachable(f"the control plane's status breaks its contract, {problem}")
    return document
