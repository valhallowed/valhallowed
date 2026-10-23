import json
import logging
from pathlib import Path

import pytest

from vhd_runtime import status

REPORT: status.RuntimeStatus = {"contract": 1, "generation": 1, "world": "Meadowfold", "state": "running"}


def test_reads_the_plugins_report_for_this_world(tmp_path: Path) -> None:
    path = tmp_path / "world.json"
    path.write_text(json.dumps({"world": "Meadowfold", "objects": 48113, "players": 2}))
    info = path.stat()
    assert status.read_world(path, "Meadowfold") == status.WorldStatus(
        objects=48113, players=2, stamp=(info.st_ino, info.st_mtime_ns)
    )
    assert status.read_world(path, "Ashwake") is None


@pytest.mark.parametrize("content", ["", "{", '{"world": "Meadowfold", "objects": -1, "players": 0}', "[]"])
def test_ignores_a_report_it_cannot_trust(tmp_path: Path, content: str) -> None:
    path = tmp_path / "world.json"
    path.write_text(content)
    assert status.read_world(path, "Meadowfold") is None


def test_ignores_a_missing_report(tmp_path: Path) -> None:
    assert status.read_world(tmp_path / "world.json", "Meadowfold") is None


def test_reporter_without_url_only_logs() -> None:
    posts: list[str] = []
    status.Reporter(None, lambda url, body, timeout: posts.append(url)).send(REPORT)
    assert posts == []


def test_reporter_logs_a_failing_url_once(caplog: pytest.LogCaptureFixture) -> None:
    def refuse(url: str, body: bytes, timeout: float) -> None:
        raise ConnectionRefusedError("refused")

    reporter = status.Reporter("http://control-plane:9457/status", refuse)
    with caplog.at_level(logging.WARNING):
        reporter.send(REPORT)
        reporter.send(REPORT)
    assert [r.message for r in caplog.records].count(
        "cannot report status to http://control-plane:9457/status: refused"
    ) == 1


def test_reporter_posts_the_report() -> None:
    bodies: list[bytes] = []
    status.Reporter("http://control-plane:9457/status", lambda url, body, timeout: bodies.append(body)).send(REPORT)
    assert json.loads(bodies[0]) == REPORT
