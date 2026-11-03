import json
from collections.abc import Mapping
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest

from vhd_data.documents import Documents, Reader, validator
from vhd_data.intake import ForeignSession, Intake, Telemetry, Unavailable
from vhd_data.status import ServiceStatus
from vhd_data.test_documents import CONTRACTS, intake_documents

MEADOWFOLD = "world-meadowfold-1a2b3c"
SESSION = "3f2a9c1e-7b4d-4e8a-9c2f-1a6b5d0e8f47"
AT = "2026-11-03T18:04:58Z"
POSITION = {"kind": "player.position", "at": AT, "player": {"id": "Steam_1"}, "x": 1, "y": 2, "z": 3}


def batch(*events: Mapping[str, object]) -> dict[str, object]:
    return {"contract": 1, "session": SESSION, "sent_at": "2026-11-03T18:05:00Z", "events": list(events)}


class FakeTelemetry(Telemetry):
    def __init__(self) -> None:
        super().__init__(lambda: (_ for _ in ()).throw(AssertionError("no database here")))
        self.taken: list[tuple[str, str, list[Mapping[str, Any]]]] = []
        self.fail: Exception | None = None

    def check(self) -> None:
        if self.fail is not None:
            raise Unavailable("down")

    def take(self, world: str, session: str, events: list[Mapping[str, Any]]) -> None:
        if self.fail is not None:
            raise self.fail
        self.taken.append((world, session, events))


class Sent:
    def __init__(self) -> None:
        self.reports: list[ServiceStatus] = []

    def __call__(self, url: str, body: bytes, timeout: float) -> None:
        self.reports.append(json.loads(body))


@pytest.fixture
def intake(tmp_path: Path) -> tuple[Intake, FakeTelemetry]:
    intake_documents(tmp_path, {MEADOWFOLD: "Meadowfold"})
    telemetry = FakeTelemetry()

    def made(documents: Documents) -> Telemetry:
        return telemetry

    taker = Intake(
        reader=Reader("intake", tmp_path, CONTRACTS),
        contracts=CONTRACTS,
        telemetry=made,
        resolve=lambda name: {"192.0.2.5"} if name == MEADOWFOLD else set(),
    )
    taker._adopt()
    return taker, telemetry


def test_a_batch_from_a_worlds_container_is_taken_as_that_world(intake: tuple[Intake, FakeTelemetry]) -> None:
    taker, telemetry = intake
    assert taker.take("192.0.2.5", batch(POSITION)) == (HTTPStatus.ACCEPTED, {"accepted": 1, "dropped": 0})
    assert telemetry.taken == [("Meadowfold", SESSION, [POSITION])]
    assert taker.counts == (1, 0)


def test_a_batch_from_anywhere_else_is_refused(intake: tuple[Intake, FakeTelemetry]) -> None:
    taker, telemetry = intake
    status, _ = taker.take("192.0.2.9", batch(POSITION))
    assert status == HTTPStatus.FORBIDDEN
    assert telemetry.taken == []


def test_a_bad_event_is_dropped_and_counted_while_the_rest_are_taken(intake: tuple[Intake, FakeTelemetry]) -> None:
    taker, telemetry = intake
    bad = {**POSITION, "x": "far"}
    assert taker.take("192.0.2.5", batch(POSITION, bad)) == (HTTPStatus.ACCEPTED, {"accepted": 1, "dropped": 1})
    assert telemetry.taken[0][2] == [POSITION]
    assert taker.counts == (1, 1)


def test_a_malformed_envelope_is_refused(intake: tuple[Intake, FakeTelemetry]) -> None:
    taker, _ = intake
    assert taker.take("192.0.2.5", {**batch(POSITION), "world": "Meadowfold"})[0] == HTTPStatus.BAD_REQUEST
    assert taker.take("192.0.2.5", [POSITION])[0] == HTTPStatus.BAD_REQUEST


def test_a_database_that_cannot_take_it_drops_the_batch(intake: tuple[Intake, FakeTelemetry]) -> None:
    taker, telemetry = intake
    telemetry.fail = Unavailable("connection refused")
    assert taker.take("192.0.2.5", batch(POSITION, POSITION))[0] == HTTPStatus.SERVICE_UNAVAILABLE
    assert taker.counts == (0, 2)
    assert taker.state.get()[0] == "failing"


def test_a_session_of_another_world_is_refused(intake: tuple[Intake, FakeTelemetry]) -> None:
    taker, telemetry = intake
    telemetry.fail = ForeignSession(SESSION)
    assert taker.take("192.0.2.5", batch(POSITION))[0] == HTTPStatus.CONFLICT


def test_status_meets_its_contract_and_carries_the_counts(intake: tuple[Intake, FakeTelemetry]) -> None:
    taker, _ = intake
    sent = Sent()
    taker.reporter._post = sent
    taker._reach()
    taker.take("192.0.2.5", batch(POSITION))
    assert taker._documents is not None
    taker._report(taker._documents)
    report = sent.reports[-1]
    assert report == {
        "contract": 1, "generation": 1, "service": "intake", "state": "running", "accepted": 1, "dropped": 0,
    }  # fmt: skip
    status = validator(CONTRACTS / "service.status.schema.json")
    assert not list(status.iter_errors(report))


def test_intake_waits_for_a_database_that_is_not_up_yet(intake: tuple[Intake, FakeTelemetry]) -> None:
    taker, telemetry = intake
    telemetry.fail = Unavailable("no route")
    taker._reach()
    state, reason = taker.state.get()
    assert state == "starting"
    assert reason is not None and "cannot reach the database" in reason
