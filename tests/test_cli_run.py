"""The run, show and list commands.

Most tests pass `--runner local`, so they need no Docker. The tests that use the
default Docker runner either expect it to fail cleanly or skip without the image.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.interface.cli import build_parser, main
from tests.docker_support import IMAGE, requires_image

REPO = Path(__file__).resolve().parent.parent
TASK_FILE = REPO / "tasks" / "csv-quoted-fields.toml"
FIXTURES = REPO / "tasks" / "fixtures"
SOLUTIONS = REPO / "tasks" / "solutions"


def run_args(
    tmp_path: Path, solutions: Path | None = None, runner: str = "local"
) -> list[str]:
    return [
        "run",
        str(TASK_FILE),
        "--db",
        str(tmp_path / "af.sqlite3"),
        "--runs",
        str(tmp_path / "runs"),
        "--fixtures",
        str(FIXTURES),
        "--solutions",
        str(solutions or SOLUTIONS),
        "--runner",
        runner,
    ]


def test_run_defaults_to_the_docker_runner_and_the_local_image() -> None:
    args = build_parser().parse_args(["run", str(TASK_FILE)])

    assert args.runner == "docker"
    assert args.image == "afrunner:local"


def test_an_unknown_runner_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["run", str(TASK_FILE), "--runner", "ssh"])

    assert exit_info.value.code == 2


def test_a_missing_image_fails_the_run_and_says_what_to_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Runs with or without Docker installed: either `docker` is absent or the
    # image is, and both are an infrastructure failure rather than a score.
    args = [*run_args(tmp_path, runner="docker"), "--image", "afrunner:absent"]

    assert main(args) == 1

    captured = capsys.readouterr()
    assert "infrastructure_failed" in captured.out
    assert "make runner-image" in captured.err
    assert "no isolation" not in captured.err


@pytest.mark.docker
@requires_image
def test_the_docker_runner_measures_the_task_without_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [*run_args(tmp_path, runner="docker"), "--image", IMAGE]

    assert main(args) == 0

    captured = capsys.readouterr()
    assert "clean_patch" in captured.out
    assert "pid1=True" in captured.out
    assert f"afrunner in docker image {IMAGE}" in captured.out
    assert "no isolation" not in captured.err


def test_a_solved_task_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(run_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "clean_patch" in out
    assert "fail->pass  1/1" in out


def test_the_lack_of_isolation_is_warned_about_every_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(run_args(tmp_path))
    assert "no isolation" in capsys.readouterr().err


def test_an_agent_that_changes_nothing_still_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The exit-code split. A measurement exists, so the harness succeeded --
    # whatever it says about the agent. A CI script must not have to parse
    # text to tell those apart.
    assert main(run_args(tmp_path, solutions=tmp_path / "none")) == 0
    assert "no_patch_produced" in capsys.readouterr().out


def test_a_missing_task_file_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", str(tmp_path / "absent.toml")]) == 2
    assert "no such task file" in capsys.readouterr().err


def test_list_shows_the_stored_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(run_args(tmp_path))
    capsys.readouterr()

    assert main(["list", "--db", str(tmp_path / "af.sqlite3")]) == 0
    out = capsys.readouterr().out
    assert "csv-quoted-fields" in out
    assert "clean_patch" in out


def test_show_reprints_a_stored_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(run_args(tmp_path))
    first = capsys.readouterr().out
    run_id = first.split()[1]

    assert main(["show", run_id, "--db", str(tmp_path / "af.sqlite3")]) == 0
    assert "clean_patch" in capsys.readouterr().out


def test_show_on_an_unknown_run_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(run_args(tmp_path))
    capsys.readouterr()

    assert (
        main(["show", "run-does-not-exist", "--db", str(tmp_path / "af.sqlite3")]) == 2
    )
    assert "no run" in capsys.readouterr().err


def test_show_without_a_database_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show", "run-0001", "--db", str(tmp_path / "absent.sqlite3")]) == 2
    assert "no database" in capsys.readouterr().err
