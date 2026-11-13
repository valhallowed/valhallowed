import logging
from pathlib import Path

import pytest

from vhd_data.database import Database, Paths
from vhd_data.documents import Documents, IncompleteDocuments, InvalidDocuments, Reader


class Scripted(Reader):
    """A reader that answers each read with the next outcome in its script."""

    def __init__(self, *script: Documents | Exception | None) -> None:
        self._script = list(script)

    def read(self) -> Documents | None:
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_a_refusal_is_logged_once_until_it_changes_or_a_generation_is_read(caplog: pytest.LogCaptureFixture) -> None:
    documents = Documents(2, {}, {})
    reader = Scripted(
        InvalidDocuments("a"),
        InvalidDocuments("a"),
        IncompleteDocuments("later"),
        InvalidDocuments("a"),
        InvalidDocuments("b"),
        documents,
        InvalidDocuments("b"),
    )
    database = Database(paths=Paths(generation=Path("/none")), reader=reader)
    with caplog.at_level(logging.ERROR):
        results = [database._read() for _ in range(7)]
    assert results == [None, None, None, None, None, documents, None]
    refusals = [record.getMessage() for record in caplog.records if "refused this generation" in record.getMessage()]
    assert refusals == [
        "refused this generation: a",
        "refused this generation: b",
        "refused this generation: b",
    ]
