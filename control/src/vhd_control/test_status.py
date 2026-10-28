from vhd_control.reconcile import STATUS_INTERVAL_S
from vhd_control.status import EMPTY_WITHIN, STALE_AFTER, StatusBook


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def report(state: str, players: int | None = None) -> dict[str, object]:
    document: dict[str, object] = {"contract": 1, "generation": 1, "world": "Meadowfold", "state": state}
    if players is not None:
        document["players"] = players
    return document


def test_a_world_never_reported_is_unknown_and_never_empty() -> None:
    view = StatusBook().view("Meadowfold")
    assert view.state == "unknown"
    assert not view.empty


def test_a_recent_report_is_the_state() -> None:
    clock = Clock()
    book = StatusBook(clock)
    book.record(report("running", players=0))
    clock.now = 4.0
    view = book.view("Meadowfold")
    assert (view.state, view.age_s, view.empty) == ("running", 4.0, True)


def test_an_empty_report_older_than_an_interval_or_so_no_longer_vouches() -> None:
    clock = Clock()
    book = StatusBook(clock)
    book.record(report("running", players=0))
    clock.now = EMPTY_WITHIN * STATUS_INTERVAL_S + 0.1
    view = book.view("Meadowfold")
    assert view.state == "running"
    assert not view.empty


def test_a_silent_runtime_becomes_unknown_and_counts_as_occupied() -> None:
    clock = Clock()
    book = StatusBook(clock)
    book.record(report("running", players=0))
    clock.now = STALE_AFTER * STATUS_INTERVAL_S + 1
    view = book.view("Meadowfold")
    assert view.state == "unknown"
    assert not view.empty


def test_players_online_are_not_empty() -> None:
    book = StatusBook()
    book.record(report("running", players=2))
    assert not book.view("Meadowfold").empty


def test_no_player_count_is_empty_only_where_no_game_runs() -> None:
    book = StatusBook()
    book.record(report("installing"))
    assert book.view("Meadowfold").empty
    book.record(report("unverified"))
    assert not book.view("Meadowfold").empty
