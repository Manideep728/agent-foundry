"""`af dispatch` wiring.

Only the paths that reach no broker are covered here. What happens once a
message is on a topic belongs to `tests/test_adapter_kafka_bus.py`, which
needs one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.interface.cli import build_parser, main


def test_no_repeats_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Refused before the broker is contacted, so a typo costs nothing."""
    assert main(["dispatch", "--repeats", "0"]) == 2
    assert "--repeats must be at least 1" in capsys.readouterr().err


def test_an_empty_directory_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dispatching nothing and exiting 0 would be a green command that sent no work."""
    assert main(["dispatch", str(tmp_path)]) == 2
    assert "no task files" in capsys.readouterr().err


def test_an_unknown_prompt_is_refused_before_anything_is_published(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The dispatcher validates the configuration it is about to send."""
    assert main(["dispatch", "--agent", "tools", "--prompt", "no-such-prompt"]) == 2
    assert "no-such-prompt" in capsys.readouterr().err


def dispatch_settings() -> set[str]:
    """What `af dispatch` parses, read from the namespace it produces."""
    return set(vars(build_parser().parse_args(["dispatch"])))


def test_dispatch_takes_the_configuration_flags() -> None:
    """These travel in the message, so a worker is told what to measure."""
    assert {"agent", "model", "prompt", "image", "repeats"} <= dispatch_settings()


def test_dispatch_takes_no_execution_flags() -> None:
    """It writes no database and makes no workspace.

    A `--db` here would be a flag the command accepted and ignored, which is
    the failure the task loader's unknown-key rule prevents one level up.
    """
    assert {"db", "runs", "runner"}.isdisjoint(dispatch_settings())
