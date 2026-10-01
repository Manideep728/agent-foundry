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
from control_plane.domain.model import MicroUsd, ModelInvocation, ModelUsage
from control_plane.interface import cli
from control_plane.interface.cli import build_parser, main
from control_plane.services.ports import AgentResult

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


def test_the_summary_names_the_configuration_that_produced_the_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D6: results from different providers are never combined.

    All three parts, because the agent and the model vary independently: the
    single-shot agent and the tool loop drive the same model, and naming only
    the model would print two rows a reader cannot tell apart.
    """
    directory = suite_of(tmp_path, "pagination-last-page")

    main(bench_args(tmp_path, directory))

    assert "config        fake/fake/scripted" in capsys.readouterr().out


def test_bench_defaults_to_the_tasks_directory() -> None:
    args = build_parser().parse_args(["bench"])

    assert args.directory == Path("tasks")


BENCH_ONLY = {"max_bench_micros", "repeats"}
"""Flags that describe a suite and would do nothing on a single run.

Listed, and named, so that adding to it is a decision someone takes on purpose
rather than a drift the guard below stopped catching. A suite ceiling on
`af run` would be a flag that silently does nothing: the operator believes a
limit is in force, and none is.
"""


def test_bench_and_run_accept_the_same_evaluation_flags() -> None:
    """A flag added to one subcommand must be added to the other.

    They share `_add_run_flags` precisely so they cannot drift; this is what
    says so if someone adds a flag to `run` directly. The one deliberate
    exception is named above rather than allowed in general.
    """
    run = build_parser().parse_args(["run", "x.toml"])
    bench = build_parser().parse_args(["bench"])

    shared = set(vars(run)) - {"task_file", "command"}
    assert shared == set(vars(bench)) - {"directory", "command"} - BENCH_ONLY


# --- the suite ceiling -------------------------------------------------------


class SpendingAgent:
    """Changes nothing and charges 100 micro-USD a run.

    Every model this project can run locally is free, so a ceiling could not be
    reached by any real agent available here. The stub is what makes the rule
    testable before a paid provider exists.
    """

    agent_id = "stub"
    provider_id = "stub"
    model_id = "stub-1"
    prompt_id = None

    def edit(self, workspace: Path, task: object) -> AgentResult:
        return AgentResult(
            (
                ModelInvocation(
                    provider_id="stub",
                    model_id="stub-1",
                    usage=ModelUsage(input_tokens=1_000, output_tokens=0),
                    cost=MicroUsd(100),
                    latency_ms=1,
                ),
            )
        )


def test_a_benchmark_stops_before_the_task_that_would_break_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three tasks, 250 allowed, 100 a run: the third never starts.

    Not "stop once over budget". Starting the third task means either
    abandoning it half-way or spending 300 of an allowed 250, and refusing to
    begin is the only choice that keeps both numbers true.
    """
    monkeypatch.setattr(cli, "_build_agent", lambda args, clock: SpendingAgent())
    directory = suite_of(
        tmp_path, "csv-quoted-fields", "interval-merge", "path-normalise"
    )

    code = main(
        [
            *bench_args(tmp_path, directory),
            "--max-bench-micros",
            "250",
            "--max-run-micros",
            "100",
        ]
    )

    assert code == 1, "a suite that measured fewer tasks than it planned is not green"
    assert "stopping before" in capsys.readouterr().err


def test_a_suite_within_its_ceiling_runs_every_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The complement: the ceiling must not end a suite it can afford.
    monkeypatch.setattr(cli, "_build_agent", lambda args, clock: SpendingAgent())
    directory = suite_of(tmp_path, "csv-quoted-fields", "interval-merge")

    code = main(
        [
            *bench_args(tmp_path, directory),
            "--max-bench-micros",
            "10000",
            "--max-run-micros",
            "100",
        ]
    )

    assert code == 0


def test_a_ceiling_of_zero_is_a_usage_error(tmp_path: Path) -> None:
    # Zero reads as both "spend nothing" and "no limit", so it is refused
    # rather than guessed at -- before any run is created.
    directory = suite_of(tmp_path, "csv-quoted-fields")

    code = main([*bench_args(tmp_path, directory), "--max-bench-micros", "0"])

    assert code == 2


# --- repeats -----------------------------------------------------------------


def test_repeats_run_the_whole_suite_again_and_compare_each_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fake agent applies the same solution every time, so every task must
    agree. A loop that ran each task once would show no agreement line at all."""
    directory = suite_of(tmp_path, "pagination-last-page", "interval-merge")

    code = main([*bench_args(tmp_path, directory), "--repeats", "2"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err.count("pagination-last-page") == 2
    assert "[4/4]" in captured.err
    assert "tasks agree   2/2" in captured.out


def test_a_single_pass_prints_no_agreement_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With one run per task, "2/2 agree" would be true and meaningless."""
    directory = suite_of(tmp_path, "pagination-last-page", "interval-merge")

    assert main(bench_args(tmp_path, directory)) == 0
    assert "tasks agree" not in capsys.readouterr().out


def test_repeats_below_one_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = suite_of(tmp_path, "interval-merge")

    assert main([*bench_args(tmp_path, directory), "--repeats", "0"]) == 2
    assert "--repeats must be at least 1" in capsys.readouterr().err
