"""The recorded sessions (sessions/*.json): synthesized telemetry, acceptance material for what
the data layer takes in (B2) and shows as live players. Each file holds the batches of one
session, how old each batch's receipt is when it is replayed, and the live players it should
leave. test_database.py replays them into PostgreSQL; these tests need none."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from vhd_data.documents import validator
from vhd_data.test_documents import CONTRACTS

SESSIONS = Path(__file__).resolve().parent / "sessions"
PLAYER_ID = r"^Steam_[0-9]{17}$"


@dataclass(frozen=True)
class Recorded:
    """One recorded session: its batches with the age in seconds of each one's receipt (oldest
    first), and the live players it leaves by name (None for a nameless player) with their x,
    as the live_player view shows them and as the web map's read of it does."""

    name: str
    batches: list[tuple[float, dict[str, Any]]]
    view: list[tuple[str | None, float]]
    web_map: list[tuple[str | None, float]]


def _players(rows: list[dict[str, Any]]) -> list[tuple[str | None, float]]:
    return [(row["name"], row["x"]) for row in rows]


def recorded_sessions() -> list[Recorded]:
    sessions = []
    for path in sorted(SESSIONS.glob("*.json")):
        material = json.loads(path.read_text())
        sessions.append(
            Recorded(
                name=path.stem,
                batches=[(entry["age_s"], entry["batch"]) for entry in material["batches"]],
                view=_players(material["live"]["view"]),
                web_map=_players(material["live"]["web_map"]),
            )
        )
    return sessions


RECORDED = recorded_sessions()
EACH = pytest.mark.parametrize("recorded", RECORDED, ids=lambda recorded: recorded.name)


def test_there_are_five_recorded_sessions() -> None:
    assert [recorded.name for recorded in RECORDED] == [
        "disconnect-no-leave",
        "join-move-leave",
        "same-name",
        "stands-still",
        "world-stops",
    ]


@EACH
def test_every_batch_meets_the_events_contract(recorded: Recorded) -> None:
    events = validator(CONTRACTS / "intake.events.schema.json")
    for _, batch in recorded.batches:
        events.validate(batch)


@EACH
def test_a_session_is_one_session_with_its_batches_oldest_first(recorded: Recorded) -> None:
    ages = [age for age, _ in recorded.batches]
    assert ages == sorted(ages, reverse=True)
    assert len({batch["session"] for _, batch in recorded.batches}) == 1
    sent = [batch["sent_at"] for _, batch in recorded.batches]
    assert sent == sorted(sent)
    for _, batch in recorded.batches:
        assert [event["at"] for event in batch["events"]] == sorted(event["at"] for event in batch["events"])
        assert all(event["at"] <= batch["sent_at"] for event in batch["events"])


def test_the_sessions_are_distinct_and_their_players_synthetic() -> None:
    sessions = [recorded.batches[0][1]["session"] for recorded in RECORDED]
    assert len(set(sessions)) == len(sessions)
    ids = [
        event["player"]["id"]
        for recorded in RECORDED
        for _, batch in recorded.batches
        for event in batch["events"]
        if "player" in event
    ]
    assert ids
    assert all(re.fullmatch(PLAYER_ID, player) for player in ids)
    # A player id belongs to one session, so the sessions cannot touch each other's players.
    owners: dict[str, str] = {}
    for recorded in RECORDED:
        for _, batch in recorded.batches:
            for event in batch["events"]:
                if "player" in event:
                    assert owners.setdefault(event["player"]["id"], recorded.name) == recorded.name


@EACH
def test_the_web_map_shows_only_players_the_view_shows(recorded: Recorded) -> None:
    assert recorded.view
    remaining = list(recorded.view)
    for player in recorded.web_map:
        remaining.remove(player)
