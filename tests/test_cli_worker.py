"""`af worker` wiring.

Only the paths that reach no broker are covered here. The loop itself is
covered against the fake bus in `tests/test_service_worker.py`, and the whole
path against a real broker by the fan-out check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.interface.cli import _poll_interval_ms, build_parser, main


@pytest.mark.parametrize("flag", ["--max-messages", "--idle-exit-s"])
def test_a_limit_below_one_is_a_usage_error(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Zero would be a worker that starts, holds the lock, and does nothing."""
    assert main(["worker", flag, "0"]) == 2
    assert f"{flag} must be at least 1" in capsys.readouterr().err


def test_an_empty_directory_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A worker with no tasks would refuse every message it was sent."""
    assert main(["worker", str(tmp_path)]) == 2
    assert "no task files" in capsys.readouterr().err


def test_an_unknown_prompt_is_refused_before_the_worker_joins_the_group(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["worker", "--agent", "tools", "--prompt", "no-such-prompt"]) == 2
    assert "no-such-prompt" in capsys.readouterr().err


def worker_settings() -> set[str]:
    return set(vars(build_parser().parse_args(["worker"])))


def test_a_worker_takes_the_configuration_it_compares_messages_against() -> None:
    assert {"agent", "model", "prompt", "image"} <= worker_settings()


def test_a_worker_takes_the_execution_flags_a_run_needs() -> None:
    """Unlike the dispatcher, a worker measures, so it has its own database."""
    assert {"db", "runs", "runner", "fixtures", "bootstrap"} <= worker_settings()


def poll_interval_ms(*flags: str) -> int:
    return _poll_interval_ms(build_parser().parse_args(["worker", *flags]))


def test_the_default_model_timeout_keeps_the_measured_poll_interval() -> None:
    assert poll_interval_ms() == 900_000


def test_a_longer_model_timeout_lengthens_the_poll_interval_with_it() -> None:
    """D32: a run that waits 900 s on one model call must not be evicted, and
    measured a second time, for holding its message past fifteen minutes."""
    assert poll_interval_ms("--ollama-timeout", "900") == 2_700_000


def test_a_shorter_model_timeout_never_shortens_the_poll_interval() -> None:
    """Fifteen minutes was measured against real runs, not derived; a short
    model timeout says nothing about how long validation takes."""
    assert poll_interval_ms("--ollama-timeout", "60") == 900_000
