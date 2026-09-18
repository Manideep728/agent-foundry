"""Running a whole suite through one command.

All of these use `--runner local`, so the suite behaviour is tested without
Docker. What is being tested is the scheduling -- one lock, one recovery, one
service, a loop -- rather than the evaluation, which `test_cli_run.py` covers.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from control_plane.adapters.run_lock import exclusive_run_lock
from control_plane.interface.cli import build_parser, main

REPO = Path(__file__).resolve().parent.parent
TASKS = REPO / "tasks"
FIXTURES = TASKS / "fixtures"
SOLUTIONS = TASKS / "solutions"


def suite_of(tmp_path: Path, *names: str) -> Path:
    """A task directory holding copies of the named task files."""
    directory = tmp_path / "suite"
    directory.mkdir(exist_ok=True)
    for name in names:
        shutil.copy2(TASKS / f"{name}.toml", directory / f"{name}.toml")
    return directory


def bench_args(tmp_path: Path, directory: Path) -> list[str]:
    return [
        "bench",
        str(directory),
        "--db",
        str(tmp_path / "af.sqlite3"),
        "--runs",
        str(tmp_path / "runs"),
        "--fixtures",
        str(FIXTURES),
        "--solutions",
        str(SOLUTIONS),
        "--runner",
        "local",
    ]


def test_bench_evaluates_every_task_in_the_directory_in_one_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Acceptance criterion 8. Catches a loop that stops at the first task.
    directory = suite_of(tmp_path, "pagination-last-page", "interval-merge")

    assert main(bench_args(tmp_path, directory)) == 0

    out = capsys.readouterr().out
    assert "pagination-last-page" in out
    assert "interval-merge" in out
    assert "2 measured of 2 attempted" in out


def test_the_second_task_is_not_refused_by_the_first_tasks_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The flock trap, which is the whole reason bench cannot reuse run_task.

    A lock belongs to the open file description, so a second acquisition in
    the same process contends exactly as another process would. A bench built
    as "call run_task once per task inside an outer lock" fails on its second
    task with DatabaseBusy, and this is the test that would say so.
    """
    directory = suite_of(tmp_path, "pagination-last-page", "interval-merge")

    assert main(bench_args(tmp_path, directory)) == 0

    captured = capsys.readouterr()
    assert "another af run" not in captured.err
    assert "2 measured of 2 attempted" in captured.out


def test_bench_exits_one_when_a_task_produces_no_measurement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A suite where the harness broke on one task must not report success,
    # or a CI job stays green while seven of eight tasks are unmeasurable.
    directory = suite_of(tmp_path, "pagination-last-page", "interval-merge")
    broken = directory / "interval-merge.toml"
    # Repin the fixture to a digest it cannot have, so the harness refuses the
    # run rather than producing a result nobody could reproduce.
    text = broken.read_text()
    start = text.index('sha256 = "') + len('sha256 = "')
    end = text.index('"', start)
    broken.write_text(text[:start] + "f" * 64 + text[end:])

    assert main(bench_args(tmp_path, directory)) == 1

    out = capsys.readouterr().out
    # The other task still ran, and the failure is named rather than implied.
    assert "1 measured of 2 attempted" in out
    assert "fixture_hash_mismatch" in out


def test_bench_exits_zero_when_every_task_is_measured_however_badly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The exit code grades the harness, never the agent. An agent that changes
    # nothing produced a measurement, and the suite exits 0.
    directory = suite_of(tmp_path, "pagination-last-page")
    args = bench_args(tmp_path, directory)
    args[args.index("--solutions") + 1] = str(tmp_path / "no-solutions")

    assert main(args) == 0

    out = capsys.readouterr().out
    # The indented line from the summary block, not the per-task line above it
    # -- that one ends with the outcome name and would match too.
    counted = next(
        line
        for line in out.splitlines()
        if line.startswith("  ") and "no_patch_produced" in line
    )
    assert counted.split()[-1] == "1"
    assert "1 measured of 1 attempted" in out


def test_bench_warns_once_about_the_local_runner_not_once_per_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Eight copies of a warning is how a warning becomes wallpaper.
    directory = suite_of(tmp_path, "pagination-last-page", "interval-merge")

    main(bench_args(tmp_path, directory))

    assert capsys.readouterr().err.count("no isolation") == 1


def test_bench_on_an_empty_directory_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A benchmark of nothing that exits 0 is a green CI job asserting that
    # zero tasks passed.
    empty = tmp_path / "suite"
    empty.mkdir()

    assert main(bench_args(tmp_path, empty)) == 2
    assert "no task files" in capsys.readouterr().err


def test_one_unreadable_task_file_stops_the_whole_suite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail fast, because the alternative changes the denominator silently.

    Skipping the broken file would report "1 task" for a directory holding
    two, and two runs of the same suite would give different denominators and
    both look healthy.
    """
    directory = suite_of(tmp_path, "pagination-last-page")
    (directory / "broken.toml").write_text("this is not valid toml = = =\n")

    assert main(bench_args(tmp_path, directory)) == 2
    assert "broken.toml" in capsys.readouterr().err


def test_bench_beside_a_live_run_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # D17. Recovery assumes every unfinished run is abandoned, which is only
    # true while no other evaluation holds this database.
    directory = suite_of(tmp_path, "pagination-last-page")
    database = tmp_path / "af.sqlite3"

    with exclusive_run_lock(database):
        assert main(bench_args(tmp_path, directory)) == 1

    assert "another af run" in capsys.readouterr().err


def test_the_summary_names_the_model_that_produced_the_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # D6: results from different providers are never combined, which starts
    # with saying which one produced these.
    directory = suite_of(tmp_path, "pagination-last-page")

    main(bench_args(tmp_path, directory))

    assert "model         fake/scripted" in capsys.readouterr().out


def test_bench_defaults_to_the_tasks_directory() -> None:
    args = build_parser().parse_args(["bench"])

    assert args.directory == Path("tasks")


def test_bench_and_run_accept_the_same_evaluation_flags() -> None:
    """A flag added to one subcommand must be added to the other.

    They share `_add_run_flags` precisely so they cannot drift; this is what
    says so if someone adds a flag to `run` directly.
    """
    run = build_parser().parse_args(["run", "x.toml"])
    bench = build_parser().parse_args(["bench"])

    shared = set(vars(run)) - {"task_file", "command"}
    assert shared == set(vars(bench)) - {"directory", "command"}
