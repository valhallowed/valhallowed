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
from psycopg import sql

from vhd_data import schema
from vhd_data.database import Database, Paths
from vhd_data.documents import Reader
from vhd_data.intake import ForeignSession, Telemetry, Unavailable
from vhd_data.test_documents import CONTRACTS, PASSWORD, STATUS_URL, write

BIN = Paths().bin
pytestmark = pytest.mark.skipif(not (BIN / "initdb").exists(), reason="no PostgreSQL here")

SESSION = "3f2a9c1e-7b4d-4e8a-9c2f-1a6b5d0e8f47"
OTHER = "0b7e4a2c-5d1f-4c3e-8a9b-2f6d1e0c7a35"
EMBER_OLD = "5c1d8e2a-4b7f-4a93-8d6e-0f3a2b9c7e14"
EMBER = "9e4b7a1c-2d5f-4f08-b3a6-6c8d1e0f2a59"
HOLLOW = "d2a6f0b8-3c9e-4e17-a5b4-7f1c8d3e6a20"
AT = "2026-11-03T18:04:58Z"
WEB_MAP_PASSWORD = "Zk3Qp8Vr1Lx6Tn2Wc9Hb4Yd7Fj0GmSaX"
NEW_INTAKE_PASSWORD = "N3wPassw0rdN3wPassw0rdN3wPassw0r"
NEW_WEB_MAP_PASSWORD = "Mw5Ds2Kc8Rn1Bx7Tq4Lv9Hj3Fy6GpZaE"


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

    def web_map(self, password: str = WEB_MAP_PASSWORD) -> psycopg.Connection[Any]:
        return psycopg.connect(
            host="127.0.0.1",
            port=self.port,
            dbname="vhd",
            user="web_map",
            password=password,
            connect_timeout=5,
            autocommit=True,
        )

    def owner(self) -> psycopg.Connection[Any]:
        return self.database.connect()

    def wait_for(self, state: str) -> None:
        for _ in range(100):
            if self.database.state.get()[0] == state:
                return
            threading.Event().wait(0.1)
        raise AssertionError(f"the database is {self.database.state.get()}, not {state}")


def database_documents(directory: Path, number: int, intake: str = PASSWORD, web_map: str = WEB_MAP_PASSWORD) -> None:
    generation = {"status_url": STATUS_URL, "status_interval_s": 1}
    passwords = {"intake": intake, "web_map": web_map}
    write(directory, generation, {"passwords": passwords}, number, secrets_contract=2)


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


def position(player: str = "Steam_1", x: float = 1.0, at: str = AT) -> dict[str, Any]:
    return {"kind": "player.position", "at": at, "player": {"id": player}, "x": x, "y": 2.0, "z": 3.0}


def moment(second: int) -> str:
    return f"2026-11-03T18:05:{second:02d}Z"


def join(player: str, name: str, second: int) -> dict[str, Any]:
    return {"kind": "player.join", "at": moment(second), "player": {"id": player, "name": name}}


def leave(player: str, second: int) -> dict[str, Any]:
    return {"kind": "player.leave", "at": moment(second), "player": {"id": player}}


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


@pytest.fixture(scope="module")
def played(running: Running) -> None:
    """Emberfall's older session holds Eir and a stale spot of Alva's; its newer one holds Alva, Bryn,
    Cade, Dag and Gro. Hollowmere holds Fenn. A world's newest session is the one intake took last."""
    telemetry = running.intake()
    telemetry.take(
        "Emberfall",
        EMBER_OLD,
        [join("Steam_25", "Eir", 1), position("Steam_25", 5.0, moment(2)), position("Steam_21", 4.0, moment(3))],
    )
    telemetry.take("Hollowmere", HOLLOW, [join("Steam_26", "Fenn", 1), position("Steam_26", 60.0, moment(2))])
    telemetry.take(
        "Emberfall",
        EMBER,
        [
            # Alva joined and moved.
            join("Steam_21", "Alva", 10),
            position("Steam_21", 11.0, moment(11)),
            # Bryn left after moving.
            join("Steam_22", "Bryn", 10),
            position("Steam_22", 12.0, moment(11)),
            leave("Steam_22", 12),
            # Cade left, rejoined and moved.
            join("Steam_23", "Cade", 10),
            position("Steam_23", 13.0, moment(11)),
            leave("Steam_23", 12),
            join("Steam_23", "Cade", 13),
            position("Steam_23", 14.0, moment(14)),
            # Dag sent positions and never a join.
            position("Steam_24", 15.0, moment(11)),
            # Gro's leave is older than the position that came after it.
            join("Steam_27", "Gro", 10),
            leave("Steam_27", 11),
            position("Steam_27", 16.0, moment(12)),
        ],
    )
    telemetry.reset()


def live(running: Running, world: str) -> dict[str | None, float]:
    """The world's live players by name, each with the x of where they were last seen."""
    with running.owner() as connection:
        rows = connection.execute("SELECT name, x FROM live_player WHERE world = %s", (world,)).fetchall()
    return {row[0]: row[1] for row in rows}


def test_a_player_who_joined_and_moved_is_live_where_the_newest_session_saw_them(
    running: Running, played: None
) -> None:
    assert live(running, "Emberfall")["Alva"] == 11.0


def test_a_player_who_left_after_their_last_position_is_not_live(running: Running, played: None) -> None:
    assert "Bryn" not in live(running, "Emberfall")


def test_a_player_who_left_and_rejoined_and_moved_is_live_where_they_moved(running: Running, played: None) -> None:
    assert live(running, "Emberfall")["Cade"] == 14.0


def test_a_leave_older_than_the_latest_position_leaves_a_player_live(running: Running, played: None) -> None:
    assert live(running, "Emberfall")["Gro"] == 16.0


def test_a_player_with_positions_and_no_join_is_live_with_no_name(running: Running, played: None) -> None:
    assert live(running, "Emberfall")[None] == 15.0


def test_a_player_in_an_older_session_is_not_live(running: Running, played: None) -> None:
    assert "Eir" not in live(running, "Emberfall")


def test_each_world_shows_its_own_players(running: Running, played: None) -> None:
    assert live(running, "Emberfall") == {"Alva": 11.0, "Cade": 14.0, "Gro": 16.0, None: 15.0}
    assert live(running, "Hollowmere") == {"Fenn": 60.0}


def test_a_live_player_carries_the_times_of_their_latest_position(running: Running, played: None) -> None:
    with running.owner() as connection:
        seen = connection.execute("SELECT at, received_at FROM live_player WHERE name = 'Cade'").fetchone()
        taken = connection.execute("SELECT at, received_at FROM position WHERE player_id = 'Steam_23' AND x = 14.0")
        assert seen is not None and seen == taken.fetchone()


def test_the_live_players_have_no_id_column(running: Running) -> None:
    with running.owner() as connection:
        columns = connection.execute("SELECT * FROM live_player LIMIT 0").description
    assert columns is not None
    assert [column.name for column in columns] == ["world", "name", "x", "y", "z", "at", "received_at"]


def test_the_live_players_are_a_security_barrier(running: Running) -> None:
    with running.owner() as connection:
        row = connection.execute("SELECT reloptions FROM pg_class WHERE relname = 'live_player'").fetchone()
    assert row is not None
    assert row[0] == ["security_barrier=true"]


@pytest.fixture(scope="module")
def web_map(running: Running) -> Iterator[psycopg.Connection[Any]]:
    """A connection signed in as the web map's role, with the password the first generation handed the
    database. The running database has applied it before it reports running."""
    with running.web_map() as connection:
        yield connection


def test_the_web_map_reads_the_live_players(web_map: psycopg.Connection[Any], played: None) -> None:
    rows = web_map.execute("SELECT name, x FROM live_player WHERE world = 'Hollowmere'").fetchall()
    assert rows == [("Fenn", 60.0)]


def test_the_web_map_is_refused_every_table(running: Running, web_map: psycopg.Connection[Any]) -> None:
    with running.owner() as owner:
        tables = [row[0] for row in owner.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
    assert {"world", "session", "player", "presence", "position", "schema_migration"} <= set(tables)
    for table in tables:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            web_map.execute(sql.SQL("SELECT 1 FROM {}").format(sql.Identifier(table)))


def test_the_web_map_may_write_nothing(web_map: psycopg.Connection[Any]) -> None:
    for statement in (
        "INSERT INTO world (name) VALUES ('Elsewhere')",
        "UPDATE player SET name = 'Elsewhere'",
        "DELETE FROM position",
        "CREATE TABLE intruder (id int)",
        "DROP VIEW live_player",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            web_map.execute(statement)


def test_a_database_that_ran_only_the_first_migration_takes_the_view_at_its_next_start(running: Running) -> None:
    with running.database.connect("postgres", autocommit=True) as connection:
        connection.execute("CREATE DATABASE upgrade")
    try:
        with running.database.connect("upgrade") as connection:
            schema.apply(connection, "upgrade", {})
            connection.execute("DROP VIEW live_player")
            connection.execute("DELETE FROM schema_migration WHERE version > 1")
            connection.commit()
            assert schema.apply(connection, "upgrade", {}) == len(schema.migrations())
            assert connection.execute("SELECT count(*) FROM live_player").fetchone() == (0,)
    finally:
        with running.database.connect("postgres", autocommit=True) as connection:
            connection.execute("DROP DATABASE upgrade")


def test_a_wrong_password_cannot_sign_in(running: Running) -> None:
    with pytest.raises(Unavailable):
        running.intake("x" * 32).check()


def test_a_new_password_is_applied_in_place(running: Running) -> None:
    database_documents(running.directory, 2, NEW_INTAKE_PASSWORD)
    for _ in range(100):
        try:
            running.intake(NEW_INTAKE_PASSWORD).check()
            break
        except Unavailable:
            threading.Event().wait(0.1)
    else:
        raise AssertionError("intake cannot sign in with its new password")
    with pytest.raises(Unavailable):
        running.intake(PASSWORD).check()


def test_a_new_web_map_password_is_applied_in_place(running: Running) -> None:
    database_documents(running.directory, 3, NEW_INTAKE_PASSWORD, NEW_WEB_MAP_PASSWORD)
    try:
        for _ in range(100):
            try:
                with running.web_map(NEW_WEB_MAP_PASSWORD) as connection:
                    connection.execute("SELECT 1 FROM live_player LIMIT 1")
                break
            except psycopg.OperationalError:
                threading.Event().wait(0.1)
        else:
            raise AssertionError("the web map cannot sign in with its new password")
        with pytest.raises(psycopg.OperationalError):
            running.web_map(WEB_MAP_PASSWORD)
    finally:
        # The module's other tests sign in with the first generation's passwords.
        database_documents(running.directory, 4)
        for _ in range(100):
            try:
                with running.web_map():
                    running.intake().check()
                break
            except (psycopg.OperationalError, Unavailable):
                threading.Event().wait(0.1)
        else:
            raise AssertionError("the first generation's passwords were not applied again")


def test_applying_again_changes_nothing(running: Running) -> None:
    with running.owner() as connection:
        before = connection.execute("SELECT count(*) FROM schema_migration").fetchone()
        schema.apply(connection, "vhd", {"intake": NEW_INTAKE_PASSWORD})
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
