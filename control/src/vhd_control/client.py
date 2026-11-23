"""The control API's client, for vhd: HTTP over the control plane's Unix socket."""

import http.client
import json
import socket
from pathlib import Path
from typing import Any

from vhd_control.schemas import Schemas

TIMEOUT_S = 10
STATUS_CONTRACT = "contracts/control.status.schema.json"
RESTORE_REPLY_CONTRACT = "contracts/control.restore-reply.schema.json"


class Unreachable(Exception):
    """The control plane is not running, did not answer, answered with an error, or answered with
    a document that breaks its contract. Either way it vouches for no world."""


class Refused(Exception):
    """The control plane refused an operator action, and changed nothing. The message says why."""


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


def restore(path: Path, schemas: Schemas, world: str, backup: str) -> dict[str, Any]:
    """POST /restore: the restore the control plane holds for the world, its reply held to its
    contract. Raises Refused when the control plane refused it, and Unreachable when the answer
    never came, when the control plane may have taken it all the same."""
    body = json.dumps({"contract": 1, "world": world, "backup": backup})
    connection = _UnixConnection(path)
    try:
        connection.request("POST", "/restore", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        answer = response.read()
    except OSError as error:
        raise Unreachable(
            f"the control plane did not answer on {path} ({error}); it may have taken the restore all the same: "
            "vhd status shows any it holds"
        ) from error
    finally:
        connection.close()
    try:
        document = json.loads(answer)
    except ValueError as error:
        raise Unreachable(f"the control plane answered {response.status}, not in JSON: {error}") from error
    if response.status != http.client.ACCEPTED:
        why = document.get("error") if isinstance(document, dict) else None
        raise Refused(str(why) if why else f"the control plane answered {response.status}")
    problem = schemas.problem(RESTORE_REPLY_CONTRACT, document)
    if problem is not None:
        raise Unreachable(f"the control plane's answer to the restore breaks its contract, {problem}")
    reply: dict[str, Any] = document
    return reply
