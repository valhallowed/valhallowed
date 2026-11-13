"""B4: the data layer's schema, roles and grants, which only the database service applies.

The schema moves forward only: each file in migrations/ is applied once, in order, and recorded.
Roles and grants are applied at every start, and on each generation whose passwords changed, with
each role's password from the database's secrets, so a password generated again still signs in. All
of it runs in one transaction, so a failure leaves the database as it was.
"""

import re
from collections.abc import Mapping
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

MIGRATIONS = Path(__file__).parent / "migrations"
_NUMBERED = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

# What each role may do. Intake writes telemetry and reads only what it deduplicates against. The web
# map reads the live players and nothing else. A role is created only when it is handed a password.
GRANTS = {
    "intake": """
        GRANT CONNECT ON DATABASE {database} TO intake;
        GRANT USAGE ON SCHEMA public TO intake;
        GRANT SELECT, INSERT ON world, session TO intake;
        GRANT UPDATE (started_at) ON session TO intake;
        GRANT SELECT, INSERT, UPDATE (name, seen_at) ON player TO intake;
        GRANT INSERT ON presence, position TO intake;
    """,
    "web_map": """
        GRANT CONNECT ON DATABASE {database} TO web_map;
        GRANT USAGE ON SCHEMA public TO web_map;
        GRANT SELECT ON live_player TO web_map;
    """,
}


def migrations(directory: Path = MIGRATIONS) -> list[tuple[int, str]]:
    """Each migration's number and SQL, in order. A file not named NNN_name.sql is a fault."""
    found: list[tuple[int, str]] = []
    for path in sorted(directory.iterdir()):
        match = _NUMBERED.match(path.name)
        if match is None:
            raise ValueError(f"{path.name} is not a migration: name it NNN_name.sql")
        found.append((int(match.group(1)), path.read_text()))
    numbers = [number for number, _ in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f"migrations must be numbered 1 to {len(numbers)}, one each: {numbers}")
    return found


def apply(connection: psycopg.Connection[TupleRow], database: str, passwords: Mapping[str, str]) -> int:
    """Apply what is missing, roles and grants, in one transaction. Returns the schema's version."""
    with connection.transaction():
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration "
            "(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        # Two starts never apply at once: the second waits, then finds nothing missing.
        connection.execute("LOCK TABLE schema_migration IN EXCLUSIVE MODE")
        row = connection.execute("SELECT coalesce(max(version), 0) FROM schema_migration").fetchone()
        applied = int(row[0]) if row is not None else 0
        known = migrations()
        if applied > len(known):
            raise ValueError(f"the database's schema is version {applied}, newer than this release's {len(known)}")
        for number, text in known[applied:]:
            connection.execute(text.encode())
            connection.execute("INSERT INTO schema_migration (version) VALUES (%s)", (number,))
        connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
        for role, password in sorted(passwords.items()):
            exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            # A role's password takes no bound parameter: it is quoted as a literal instead.
            connection.execute(
                sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
            )
            connection.execute(sql.SQL(GRANTS[role]).format(database=sql.Identifier(database)))
        return len(known)
