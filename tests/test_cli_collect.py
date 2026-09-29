"""`af collect` wiring. The step itself is in `tests/test_service_collect.py`."""

from __future__ import annotations

import pytest

from control_plane.interface.cli import build_parser, main


@pytest.mark.parametrize("flag", ["--max-messages", "--idle-exit-s"])
def test_a_limit_below_one_is_a_usage_error(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["collect", flag, "0"]) == 2
    assert f"{flag} must be at least 1" in capsys.readouterr().err


def collect_settings() -> set[str]:
    return set(vars(build_parser().parse_args(["collect"])))


def test_a_collector_takes_the_database_it_owns() -> None:
    assert {"db", "bootstrap"} <= collect_settings()


def test_a_collector_takes_no_measurement_flags() -> None:
    """It measures nothing, so a --model here would be accepted and ignored."""
    assert {"agent", "model", "runner", "runs"}.isdisjoint(collect_settings())
