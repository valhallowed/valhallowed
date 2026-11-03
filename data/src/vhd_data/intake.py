"""The intake service: telemetry batches from the worlds (B2) into the database (B4).

A batch carries no world: intake knows the sender by its source address, the one Docker's DNS
gives a world's Compose service, as the control plane's status listener does, and asks on every
batch, since a recreated container's old address can pass to another. That holds while no
child can forge an address: every child runs without capabilities, NET_RAW included.

Telemetry is observation, so it fails soft: a batch intake cannot take is answered, counted
and dropped, and the plugin never waits for a retry.

| Batch | Answer |
|---|---|
| valid | 202 {accepted, dropped}; an event that breaks its kind is dropped and counted |
| malformed envelope | 400 |
| not from a world's container | 403 |
| its session already belongs to another world | 409 |
| the database unavailable | 503 |
"""

import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import psycopg

from vhd_data.documents import Documents, IncompleteDocuments, InvalidDocuments, Reader, problem, validator
from vhd_data.status import Reporter, ServiceStatus, State

SERVICE = "intake"
PORT = 9458
EVENTS_SCHEMA = "intake.events.schema.json"
# 500 events, the contract's most, at a few hundred bytes each.
MAX_BODY = 512 * 1024
TIMEOUT_S = 5
# Batches handled at once. A world sends one every few seconds; more than this at once is
# turned away rather than given a thread each.
MAX_HANDLERS = 16
POLL_S = 1.0
# Waits between attempts to reach the database, doubling to the most.
RETRY_FIRST_S = 1.0
RETRY_MAX_S = 30.0

Resolve = Callable[[str], set[str]]

_log = logging.getLogger(__name__)


def _resolve(name: str) -> set[str]:
    return {str(info[4][0]) for info in socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)}


class Unavailable(Exception):
    """The database cannot take the batch now."""


class ForeignSession(Exception):
    """The batch's session already belongs to another world."""


class Telemetry:
    """Intake's connection to the database, as its own role. One connection, one batch at a time:
    a few worlds each send a batch every few seconds."""

    def __init__(self, connect: Callable[[], psycopg.Connection[Any]]) -> None:
        self._connect = connect
        self._connection: psycopg.Connection[Any] | None = None
        self._lock = threading.Lock()

    def check(self) -> None:
        """Reach the database, or raise Unavailable."""
        with self._lock:
            self._connected()

    def reset(self) -> None:
        """Drop the connection, so the next batch signs in with the newest documents."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
            self._connection = None

    def take(self, world: str, session: str, events: list[Mapping[str, Any]]) -> None:
        """Write one batch's events, all or none."""
        with self._lock:
            connection = self._connected()
            try:
                with connection.transaction():
                    self._write(connection, world, session, events)
            except psycopg.Error as error:
                if connection.closed or isinstance(error, psycopg.OperationalError):
                    self._connection = None
                raise Unavailable(str(error)) from error

    def _connected(self) -> psycopg.Connection[Any]:
        if self._connection is None or self._connection.closed:
            try:
                self._connection = self._connect()
            except psycopg.Error as error:
                raise Unavailable(str(error)) from error
        return self._connection

    @staticmethod
    def _write(connection: psycopg.Connection[Any], world: str, session: str, events: list[Mapping[str, Any]]) -> None:
        connection.execute("INSERT INTO world (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (world,))
        row = connection.execute("SELECT id FROM world WHERE name = %s", (world,)).fetchone()
        if row is None:
            raise psycopg.DataError(f"world {world!r} was not stored")
        world_id = row[0]
        connection.execute(
            "INSERT INTO session (id, world_id) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (session, world_id)
        )
        owner = connection.execute("SELECT world_id FROM session WHERE id = %s", (session,)).fetchone()
        if owner is None or owner[0] != world_id:
            raise ForeignSession(session)
        for event in events:
            kind, at = event["kind"], _time(event["at"])
            if kind == "session.start":
                connection.execute(
                    "UPDATE session SET started_at = %s WHERE id = %s AND started_at IS NULL", (at, session)
                )
                continue
            player = event["player"]
            connection.execute(
                "INSERT INTO player (id, name, seen_at) VALUES (%(id)s, %(name)s, %(at)s) "
                "ON CONFLICT (id) DO UPDATE SET name = coalesce(excluded.name, player.name), "
                "seen_at = greatest(player.seen_at, excluded.seen_at)",
                {"id": player["id"], "name": player.get("name"), "at": at},
            )
            if kind == "player.position":
                connection.execute(
                    "INSERT INTO position (session_id, player_id, at, x, y, z) VALUES (%s, %s, %s, %s, %s, %s)",
                    (session, player["id"], at, event["x"], event["y"], event["z"]),
                )
            else:
                connection.execute(
                    "INSERT INTO presence (session_id, player_id, joined, at) VALUES (%s, %s, %s, %s)",
                    (session, player["id"], kind == "player.join", at),
                )


def _time(text: str) -> datetime:
    """A contract time, which always ends in Z, as an aware datetime."""
    return datetime.fromisoformat(text).astimezone(UTC)


def _telemetry(documents: Documents) -> Telemetry:
    return Telemetry(lambda: _connect(documents))


@dataclass
class Intake:
    reader: Reader
    contracts: Path
    telemetry: Callable[[Documents], Telemetry] = _telemetry
    resolve: Resolve = _resolve
    reporter: Reporter = field(default_factory=Reporter)
    now: Callable[[], float] = time.monotonic
    state: State = field(default_factory=State, init=False)
    _documents: Documents | None = field(default=None, init=False)
    _telemetry: Telemetry | None = field(default=None, init=False)
    _counts: dict[str, int] = field(default_factory=lambda: {"accepted": 0, "dropped": 0}, init=False)
    _counts_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    # When intake next tries to reach the database while it cannot, and how long it waits then.
    _retry_at: float = field(default=0.0, init=False)
    _wait: float = field(default=RETRY_FIRST_S, init=False)

    def __post_init__(self) -> None:
        # The envelope whole, then each event on its own, so one bad event drops only itself.
        self._envelope = validator(self.contracts / EVENTS_SCHEMA, "envelope")
        self._event = validator(self.contracts / EVENTS_SCHEMA, "event")

    def request_stop(self, signum: int = 0, frame: object = None) -> None:
        self._stop.set()

    @property
    def counts(self) -> tuple[int, int]:
        with self._counts_lock:
            return self._counts["accepted"], self._counts["dropped"]

    def run(self, server: ThreadingHTTPServer) -> int:
        """Serve batches until asked to stop, adopting each new generation in place."""
        threading.Thread(target=server.serve_forever, daemon=True).start()
        reported = float("-inf")
        try:
            while not self._stop.is_set():
                self._adopt()
                documents = self._documents
                if documents is not None:
                    self._reach()
                    if self.now() - reported >= documents.generation["status_interval_s"]:
                        self._report(documents)
                        reported = self.now()
                self._stop.wait(POLL_S)
        finally:
            server.shutdown()
            server.server_close()
        return 0

    def _reach(self) -> None:
        """Until the database takes telemetry, try it with a capped backoff: it may start after
        intake, or restart while intake runs."""
        state, _ = self.state.get()
        if state == "running" or self._telemetry is None or self.now() < self._retry_at:
            return
        try:
            self._telemetry.check()
        except Unavailable as error:
            self.state.set("failing" if state == "failing" else "starting", f"cannot reach the database: {error}")
            self._retry_at, self._wait = self.now() + self._wait, min(self._wait * 2, RETRY_MAX_S)
            return
        _log.info("the database takes telemetry")
        self.state.set("running")
        self._wait = RETRY_FIRST_S

    def take(self, sender: str, document: object) -> tuple[HTTPStatus, dict[str, object]]:
        """One batch from sender: the answer, and what it says."""
        documents, telemetry = self._documents, self._telemetry
        if documents is None or telemetry is None:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "intake has no generation yet"}
        found = problem(self._envelope, document)
        if found is not None:
            return HTTPStatus.BAD_REQUEST, {"error": found}
        batch = cast(dict[str, Any], document)
        world = self._world_of(sender, documents.generation["worlds"])
        if world is None:
            return HTTPStatus.FORBIDDEN, {"error": "intake takes telemetry only from a world's own container"}
        events = [e for e in batch["events"] if problem(self._event, e) is None]
        dropped = len(batch["events"]) - len(events)
        try:
            telemetry.take(world, batch["session"], events)
        except ForeignSession:
            self._count(0, len(batch["events"]))
            return HTTPStatus.CONFLICT, {"error": "the session belongs to another world"}
        except Unavailable as error:
            self._count(0, len(batch["events"]))
            if self.state.get()[0] == "running":
                _log.warning("the database is unavailable: %s", error)
            self.state.set("failing", f"cannot reach the database: {error}")
            return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "the database is unavailable"}
        self._count(len(events), dropped)
        return HTTPStatus.ACCEPTED, {"accepted": len(events), "dropped": dropped}

    def _world_of(self, sender: str, worlds: Mapping[str, str]) -> str | None:
        """The world whose container has the sender's address. A world the generation no longer
        runs, or a name Docker's DNS does not know, matches nothing."""
        for service, world in sorted(worlds.items()):
            try:
                if sender in self.resolve(service):
                    return world
            except OSError:
                continue
        return None

    def _count(self, accepted: int, dropped: int) -> None:
        with self._counts_lock:
            self._counts["accepted"] += accepted
            self._counts["dropped"] += dropped

    def _adopt(self) -> None:
        """Take a new generation in place. New database settings or a new password sign in again."""
        try:
            documents = self.reader.read()
        except IncompleteDocuments:
            return
        except InvalidDocuments as error:
            _log.error("refused this generation: %s", error)
            return
        if documents is None or documents == self._documents:
            return
        before = self._documents
        self._documents = documents
        if before is None or _sign_in(before) != _sign_in(documents):
            if self._telemetry is not None:
                self._telemetry.reset()
            self._telemetry = self.telemetry(documents)
            self.state.set("starting", "waiting for the database")

    def _report(self, documents: Documents) -> None:
        state, reason = self.state.get()
        accepted, dropped = self.counts
        status: ServiceStatus = {
            "contract": 1,
            "generation": documents.number,
            "service": SERVICE,
            "state": state,
            "accepted": accepted,
            "dropped": dropped,
        }
        if reason is not None and state != "running":
            status["reason"] = reason
        self.reporter.send(documents.generation["status_url"], status)


def _sign_in(documents: Documents) -> tuple[object, object]:
    """What intake signs in to the database with."""
    return documents.generation["database"], documents.secrets["password"]


def _connect(documents: Documents) -> psycopg.Connection[Any]:
    database = documents.generation["database"]
    return psycopg.connect(
        host=database["host"],
        port=database["port"],
        dbname=database["name"],
        user=database["user"],
        password=documents.secrets["password"],
        connect_timeout=5,
        autocommit=False,
    )


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(MAX_HANDLERS)

    # Any: typeshed's request type is private to socketserver.
    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            # Turned away: the plugin drops the batch and counts it.
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A sender that hangs up or runs out of time is routine.
        _log.debug("a batch from %s ended early", client_address)


def server(intake: Intake, address: tuple[str, int]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        timeout = TIMEOUT_S

        def setup(self) -> None:
            super().setup()
            # The socket's timeout bounds each read; this bounds the whole request.
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

        def log_message(self, format: str, *args: object) -> None:
            _log.debug(format, *args)

        def reply(self, code: HTTPStatus, document: dict[str, object]) -> None:
            body = json.dumps(document).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/events":
                self.reply(HTTPStatus.NOT_FOUND, {"error": "intake takes POST /events"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.reply(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length is required"})
                return
            if not 0 < length <= MAX_BODY:
                self.reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": f"a batch is at most {MAX_BODY} bytes"})
                return
            body = self.rfile.read(length)
            if len(body) < length:
                return
            try:
                document: object = json.loads(body)
            except ValueError:
                self.reply(HTTPStatus.BAD_REQUEST, {"error": "not JSON"})
                return
            self.reply(*intake.take(self.client_address[0], document))

    return _Server(address, Handler)
