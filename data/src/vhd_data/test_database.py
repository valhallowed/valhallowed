"""The database service and intake against a real PostgreSQL. They run where its binaries are,
the data-layer image's test stage (scripts/check-data-layer.sh), and are skipped elsewhere."""

import json
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest

from vhd_data import schema
from vhd_data.database import Database, Paths
from vhd_data.documents import Reader
from vhd_data.intake import ForeignSession, Telemetry, Unavailable
from vhd_data.test_documents import CONTRACTS, PASSWORD, STATUS_URL, write

BIN = Paths().bin
pytestmark = pytest.mark.skipif(not (BIN / "initdb").exists(), reason="no PostgreSQL here")

SESSION = "3f2a9c1e-7b4d-4e8a-9c2f-1a6b5d0e8f47"
OTHER = "0b7e4a2c-5d1f-4c3e-8a9b-2f6d1e0c7a35"
AT = "2026-11-03T18:04:58Z"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class Running:
    database: Database
    directory: Path
    port: int
    reports: list[dict[str, Any]]

    def intake(self, password: str = PASSWORD) -> Telemetry:
        return Telemetry(
            lambda: psycopg.connect(
                host="127.0.0.1", port=self.port, dbname="vhd", user="intake", password=password, connect_timeout=5
            )
        )

    def owner(self) -> psycopg.Connection[Any]:
        return self.database.connect()

    def wait_for(self, state: str) -> None:
        for _ in range(100):
            if self.database.state.get()[0] == state:
                return
            threading.Event().wait(0.1)
        raise AssertionError(f"the database is {self.database.state.get()}, not {state}")


def database_documents(directory: Path, number: int, password: str = PASSWORD) -> None:
    generation = {"status_url": STATUS_URL, "status_interval_s": 1}
    write(directory, generation, {"passwords": {"intake": password}}, number)


@pytest.fixture(scope="module")
def running(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Running]:
    root = tmp_path_factory.mktemp("database")
    directory = root / "generation"
    database_documents(directory, 1)
    paths = Paths(generation=directory, cluster=root / "data" / "cluster", socket=root / "socket", contracts=CONTRACTS)
    (root / "data").mkdir()
    reports: list[dict[str, Any]] = []
    port = _free_port()
    service = Database(paths=paths, reader=Reader("database", directory, CONTRACTS), listen="127.0.0.1", port=port)
    service.reporter._post = lambda url, body, timeout: reports.append(json.loads(body))
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    up = Running(service, directory, port, reports)
    up.wait_for("running")
    yield up
    service.request_stop()
    thread.join(60)
    assert not thread.is_alive()


def position(player: str = "Steam_1", x: float = 1.0) -> dict[str, Any]:
    return {"kind": "player.position", "at": AT, "player": {"id": player}, "x": x, "y": 2.0, "z": 3.0}


def test_the_database_reports_running_once_its_schema_is_applied(running: Running) -> None:
    assert running.reports
    assert running.reports[-1] == {"contract": 1, "generation": 1, "service": "database", "state": "running"}
    with running.owner() as connection:
        row = connection.execute("SELECT max(version) FROM schema_migration").fetchone()
    assert row is not None and row[0] == len(schema.migrations())


def test_intake_takes_a_batch_into_the_database(running: Running) -> None:
    telemetry = running.intake()
    telemetry.take(
        "Meadowfold",
        SESSION,
        [
            {"kind": "session.start", "at": AT},
            {"kind": "player.join", "at": AT, "player": {"id": "Steam_1", "name": "Sigrún"}},
            position(),
            {"kind": "player.leave", "at": AT, "player": {"id": "Steam_1"}},
        ],
    )
    with running.owner() as connection:
        session = connection.execute(
            "SELECT w.name, s.started_at IS NOT NULL FROM session s JOIN world w ON w.id = s.world_id WHERE s.id = %s",
            (SESSION,),
        ).fetchone()
        player = connection.execute("SELECT name FROM player WHERE id = 'Steam_1'").fetchone()
        joined = connection.execute("SELECT joined FROM presence WHERE session_id = %s ORDER BY joined", (SESSION,))
        positions = connection.execute("SELECT x, y, z FROM position WHERE session_id = %s", (SESSION,)).fetchall()
        assert session == ("Meadowfold", True)
        assert player == ("Sigrún",)
        assert [row[0] for row in joined] == [False, True]
        assert positions == [(1.0, 2.0, 3.0)]


def test_a_session_stays_with_the_world_that_first_sent_it(running: Running) -> None:
    telemetry = running.intake()
    telemetry.take("Meadowfold", OTHER, [position()])
    with pytest.raises(ForeignSession):
        telemetry.take("Ashwake", OTHER, [position(x=9.0)])
    with running.owner() as connection:
        rows = connection.execute("SELECT x FROM position WHERE session_id = %s", (OTHER,)).fetchall()
    assert rows == [(1.0,)]


def test_intake_may_write_telemetry_and_nothing_more(running: Running) -> None:
    with psycopg.connect(
        host="127.0.0.1", port=running.port, dbname="vhd", user="intake", password=PASSWORD, connect_timeout=5
    ) as connection:
        for statement in (
            "DELETE FROM position",
            "UPDATE world SET name = 'Elsewhere'",
            "CREATE TABLE intruder (id int)",
            "DROP TABLE presence",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                connection.execute(statement)


def test_a_wrong_password_cannot_sign_in(running: Running) -> None:
    with pytest.raises(Unavailable):
        running.intake("x" * 32).check()


def test_a_new_password_is_applied_in_place(running: Running) -> None:
    new = "N3wPassw0rdN3wPassw0rdN3wPassw0r"
    database_documents(running.directory, 2, new)
    for _ in range(100):
        try:
            running.intake(new).check()
            break
        except Unavailable:
            threading.Event().wait(0.1)
    else:
        raise AssertionError("intake cannot sign in with its new password")
    with pytest.raises(Unavailable):
        running.intake(PASSWORD).check()


def test_applying_again_changes_nothing(running: Running) -> None:
    with running.owner() as connection:
        before = connection.execute("SELECT count(*) FROM schema_migration").fetchone()
        schema.apply(connection, "vhd", {"intake": "N3wPassw0rdN3wPassw0rdN3wPassw0r"})
        after = connection.execute("SELECT count(*) FROM schema_migration").fetchone()
    assert before == after


def test_a_database_newer_than_the_release_is_refused(running: Running) -> None:
    with running.owner() as connection:
        connection.execute("INSERT INTO schema_migration (version) VALUES (99)")
        connection.commit()
        try:
            with pytest.raises(ValueError, match="newer than this release"):
                schema.apply(connection, "vhd", {"intake": PASSWORD})
        finally:
            connection.rollback()
            connection.execute("DELETE FROM schema_migration WHERE version = 99")
            connection.commit()
