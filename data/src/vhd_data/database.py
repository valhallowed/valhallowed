"""The database service: PostgreSQL from the image's Debian, run and reported on by this process.

At first start it creates the cluster, owned by the bootstrap role the service runs as, who
signs in over the cluster's socket alone. At every start it applies the schema, roles and
grants (schema.py) before it reports running, and on each new generation whose passwords
changed it applies them again. A service signs in over the install network as its own role,
with its password; nothing else does.
"""

import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from vhd_data import schema
from vhd_data.documents import Documents, IncompleteDocuments, InvalidDocuments, Reader
from vhd_data.status import Reporter, ServiceStatus, State, reason

SERVICE = "database"
NAME = "vhd"
PORT = 5432
POLL_S = 1.0
# How long PostgreSQL gets to come up, and to stop after a fast shutdown, before it is killed.
READY_TIMEOUT_S = 60
STOP_TIMEOUT_S = 50

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Paths:
    generation: Path = Path("/generation")
    cluster: Path = Path("/data/cluster")
    socket: Path = Path("/run/postgresql")
    contracts: Path = Path("/opt/vhd/contracts")
    bin: Path = Path("/usr/lib/postgresql/17/bin")


@dataclass
class Database:
    paths: Paths
    reader: Reader
    reporter: Reporter = field(default_factory=Reporter)
    # Where PostgreSQL listens for the services. Tests narrow it to this host.
    listen: str = "*"
    port: int = PORT
    now: Callable[[], float] = time.monotonic
    state: State = field(default_factory=State, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _server: subprocess.Popen[bytes] | None = field(default=None, init=False)

    def request_stop(self, signum: int = 0, frame: object = None) -> None:
        self._stop.set()

    def run(self) -> int:
        documents = self._documents()
        if documents is None:
            return 0
        try:
            self._init()
            self._start()
        except (OSError, subprocess.CalledProcessError, TimeoutError) as error:
            _log.error("the database cannot start: %s", error)
            self.state.set("failing", f"cannot start PostgreSQL: {error}")
            self._report(documents)
            return 1
        applied: Mapping[str, str] | None = None
        attempted = reported = float("-inf")
        while not self._stop.is_set():
            current = documents = self._read() or documents
            interval = current.generation["status_interval_s"]
            passwords: Mapping[str, str] = current.secrets["passwords"]
            # Again for new passwords, and a failed attempt once each reporting interval.
            if passwords != applied and (applied is not None or self.now() - attempted >= interval):
                attempted = self.now()
                applied = passwords if self._apply(passwords) else None
            if self._server is not None and self._server.poll() is not None:
                _log.error("PostgreSQL exited with %s", self._server.returncode)
                self.state.set("failing", f"PostgreSQL exited with {self._server.returncode}")
                self._report(current)
                return 1
            if self.now() - reported >= interval:
                self._report(current)
                reported = self.now()
            self._stop.wait(POLL_S)
        self._shut_down()
        return 0

    def connect(self, database: str = NAME, autocommit: bool = False) -> psycopg.Connection[Any]:
        """The bootstrap role's connection, over the cluster's socket."""
        return psycopg.connect(
            host=str(self.paths.socket), port=self.port, dbname=database, connect_timeout=5, autocommit=autocommit
        )

    def _documents(self) -> Documents | None:
        """Wait for the first complete documents, or None once asked to stop."""
        while not self._stop.is_set():
            documents = self._read()
            if documents is not None:
                return documents
            self._stop.wait(POLL_S)
        return None

    def _read(self) -> Documents | None:
        try:
            return self.reader.read()
        except IncompleteDocuments:
            return None
        except InvalidDocuments as error:
            _log.error("refused this generation: %s", error)
            return None

    def _init(self) -> None:
        """Create the cluster at first start. A cluster from an interrupted initdb is removed first,
        since PostgreSQL refuses to start on it."""
        if (self.paths.cluster / "PG_VERSION").exists() and (self.paths.cluster / "global" / "pg_control").exists():
            return
        if self.paths.cluster.exists():
            for leftover in sorted(self.paths.cluster.rglob("*"), reverse=True):
                leftover.rmdir() if leftover.is_dir() and not leftover.is_symlink() else leftover.unlink()
        _log.info("creating the database cluster in %s", self.paths.cluster)
        subprocess.run(  # noqa: S603 - fixed arguments
            [
                str(self.paths.bin / "initdb"),
                "--pgdata", str(self.paths.cluster),
                "--auth-local=peer", "--auth-host=scram-sha-256",
                "--encoding=UTF8", "--locale=C.UTF-8", "--no-instructions",
            ],
            check=True, capture_output=True,
        )  # fmt: skip

    def _start(self) -> None:
        # Rewritten at every start: the bootstrap role over the socket, each service over the network.
        (self.paths.cluster / "pg_hba.conf").write_text(
            "local all all peer\n"
            f"host {NAME} all all scram-sha-256\n"
        )  # fmt: skip
        self.paths.socket.mkdir(parents=True, exist_ok=True)
        self._server = subprocess.Popen(  # noqa: S603 - fixed arguments
            [
                str(self.paths.bin / "postgres"),
                "-D", str(self.paths.cluster),
                "-c", f"listen_addresses={self.listen}",
                "-c", f"port={self.port}",
                "-c", f"unix_socket_directories={self.paths.socket}",
            ],
            # Its own process group, so a terminal's interrupt reaches only this process.
            start_new_session=True,
        )  # fmt: skip
        deadline = self.now() + READY_TIMEOUT_S
        while True:
            if self._server.poll() is not None:
                raise OSError(f"PostgreSQL exited with {self._server.returncode} while starting")
            try:
                # CREATE DATABASE runs outside a transaction.
                with self.connect("postgres", autocommit=True) as connection:
                    if connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (NAME,)).fetchone() is None:
                        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(NAME)))
                return
            except psycopg.OperationalError:
                if self.now() > deadline:
                    raise TimeoutError(f"PostgreSQL did not accept connections within {READY_TIMEOUT_S} s") from None
                time.sleep(0.2)

    def _apply(self, passwords: Mapping[str, str]) -> bool:
        try:
            with self.connect() as connection:
                version = schema.apply(connection, NAME, passwords)
        except (psycopg.Error, ValueError) as error:
            why = f"cannot apply the schema: {error}"
            if self.state.get() != ("failing", reason(why)):
                _log.error("%s", why)
            self.state.set("failing", why)
            return False
        _log.info("schema version %d, roles and grants applied", version)
        self.state.set("running")
        return True

    def _report(self, documents: Documents) -> None:
        state, reason = self.state.get()
        status: ServiceStatus = {"contract": 1, "generation": documents.number, "service": SERVICE, "state": state}
        if reason is not None:
            status["reason"] = reason
        self.reporter.send(documents.generation["status_url"], status)

    def _shut_down(self) -> None:
        """PostgreSQL's fast shutdown: open transactions roll back, and the cluster stops clean."""
        if self._server is None or self._server.poll() is not None:
            return
        os.kill(self._server.pid, signal.SIGINT)
        try:
            self._server.wait(STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _log.error("PostgreSQL did not stop within %s s; killing it", STOP_TIMEOUT_S)
            self._server.kill()
            self._server.wait()
