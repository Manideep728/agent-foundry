"""The report artifact, acceptance criterion 9.

These drive the real command against a real database, because the failure
these are shaped to catch is a report that renders beautifully from records
nobody ever stored. Everything here uses `--runner local`, so no container is
needed; what is under test is the reading and the rendering, not the
evaluation, which `test_cli_run.py` covers.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.system import harness_version
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.outcomes import FailureCategory
from control_plane.domain.record import RunRecord
from control_plane.domain.states import EvaluationRun, RunState
from control_plane.domain.task import Suite
from control_plane.interface.cli import main
from tests.postgres_support import fresh_schema, requires_postgres

REPO = Path(__file__).resolve().parent.parent
TASKS = REPO / "tasks"
FIXTURES = TASKS / "fixtures"
SOLUTIONS = TASKS / "solutions"


def suite_of(tmp_path: Path, *names: str) -> Path:
    directory = tmp_path / "suite"
    directory.mkdir(exist_ok=True)
    for name in names:
        shutil.copy2(TASKS / f"{name}.toml", directory / f"{name}.toml")
    return directory


def bench(tmp_path: Path, *names: str, solutions: Path | None = None) -> int:
    """Produce some real runs to report on.

    With no names, it runs whatever the caller already put in the directory --
    which is how a test that corrupts a task file keeps the corruption.
    """
    directory = suite_of(tmp_path, *names) if names else tmp_path / "suite"
    return main(
        [
            "bench",
            str(directory),
            "--db",
            str(tmp_path / "af.sqlite3"),
            "--runs",
            str(tmp_path / "runs"),
            "--fixtures",
            str(FIXTURES),
            "--solutions",
            str(SOLUTIONS if solutions is None else solutions),
            "--runner",
            "local",
        ]
    )


def report(tmp_path: Path, *extra: str) -> int:
    return main(["report", "--db", str(tmp_path / "af.sqlite3"), *extra])


def read_report(tmp_path: Path) -> str:
    return (tmp_path / "report.md").read_text(encoding="utf-8")


def a_created_run(run_id: str, task_id: str, agent: str = "fake") -> RunRecord:
    """A run the database has opened and nothing has finished.

    Exactly the row a process that died mid-run leaves behind, without having
    to kill a process to produce one.
    """
    return RunRecord(
        run=EvaluationRun.create(RunId(run_id), TaskId(task_id), 1_700_000_000_000),
        suite=Suite.SYNTHETIC,
        fixture_sha256="a" * 64,
        runner_id="local-subprocess",
        runner_version="stub 0",
        arch="unknown",
        pid1=False,
        agent_id=agent,
        provider_id="fake",
        model_id="scripted",
    )


# --- criterion 9 -------------------------------------------------------------


def test_the_report_names_successes_failures_regressions_cost_and_latency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance criterion 9, read literally.

    Each of the five is a separate assertion, because a report that grew a
    layout change and silently lost one of them is the failure that makes a
    benchmark unciteable, and nothing else in the project would notice.
    """
    assert bench(tmp_path, "pagination-last-page", "interval-merge") == 0
    capsys.readouterr()

    assert report(tmp_path, "--out", str(tmp_path / "report.md")) == 0

    text = read_report(tmp_path)
    assert "| resolved | 2/2 |" in text  # successes
    assert "| tests_still_failing | 0 |" in text  # failures, by name
    assert "| regressed | 0/2 |" in text  # regressions
    assert "| cost, micro-USD | 0 |" in text  # cost
    assert "| agent ms, min/median/max |" in text  # latency
    assert "| validation ms, min/median/max |" in text


def test_every_task_appears_in_the_report_with_its_own_row(tmp_path: Path) -> None:
    # A summary without the runs behind it cannot be checked by the person
    # reading it, and `af show <run-id>` is how they would go and look.
    bench(tmp_path, "pagination-last-page", "interval-merge")

    report(tmp_path, "--out", str(tmp_path / "report.md"))

    text = read_report(tmp_path)
    assert "| pagination-last-page | clean_patch | yes | no |" in text
    assert "| interval-merge | clean_patch | yes | no |" in text


def test_the_report_names_the_environment_that_validated_the_runs(
    tmp_path: Path,
) -> None:
    # A benchmark figure nobody can attribute to a machine is not
    # reproducible, which is why these columns exist at all.
    bench(tmp_path, "pagination-last-page")

    report(tmp_path, "--out", str(tmp_path / "report.md"))

    text = read_report(tmp_path)
    assert "- Architecture:" in text
    assert "- Supervisor was PID 1:" in text
    assert "- Image: none recorded" in text  # --runner local has no image
    assert f"- Harness: `{harness_version()}`" in text


# --- the rules the report must not break -------------------------------------


def test_two_configurations_are_reported_in_separate_sections(
    tmp_path: Path,
) -> None:
    """D6, at the surface furthest from the rule that states it.

    Before the grouping was fixed, a database holding two configurations
    summarised as one block: two fake runs and one model run reported
    `resolved 2/3`, a figure true of neither.

    The second configuration is written straight into the database rather than
    produced by a second agent, because what is under test is the report --
    running a real model here would make this test need a model server.
    """
    bench(tmp_path, "pagination-last-page")
    with SqliteRunRepository(tmp_path / "af.sqlite3") as repository:
        opened = a_created_run("run-other", "interval-merge", agent="tools")
        repository.create(opened)
        repository.finish(
            replace(
                opened,
                run=opened.run.fail(
                    FailureCategory.INFRASTRUCTURE_FAILED,
                    1_700_000_001_000,
                    "docker died",
                ),
            ),
            expected=RunState.CREATED,
        )

    report(tmp_path, "--out", str(tmp_path / "report.md"))

    text = read_report(tmp_path)
    assert "## synthetic -- fake/fake/scripted" in text
    assert "## synthetic -- tools/fake/scripted" in text
    # Each section counts only its own runs. Blended, this was 1/2.
    assert "| resolved | 1/1 |" in text
    assert "### Produced no measurement (1)" in text


def test_a_run_still_in_flight_is_named_and_left_out_of_every_figure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The crash `summarise` would otherwise be handed.

    A run in a non-terminal state is neither measured, nor unmeasured, nor
    aborted, and `SuiteSummary` refuses a set of runs its counts do not
    account for. Reporting on stored runs is the first caller that can see
    one, so the report is where it has to be handled -- and dropping it in
    silence would shrink the denominator every time a process died.
    """
    bench(tmp_path, "pagination-last-page")
    with SqliteRunRepository(tmp_path / "af.sqlite3") as repository:
        repository.create(a_created_run("run-stuck", "interval-merge"))
    capsys.readouterr()

    assert report(tmp_path, "--out", str(tmp_path / "report.md")) == 0

    text = read_report(tmp_path)
    assert "`run-stuck` (created)" in text
    assert "1 run(s) recorded, 1 of them measured" in text
    assert "| resolved | 1/1 |" in text
    # The operator is told on stderr; the report is where the ids are, so
    # a database with sixty stuck runs does not print sixty stderr lines.
    assert "1 run(s) had not finished" in capsys.readouterr().err


def test_an_unmeasured_run_is_listed_with_its_reason(tmp_path: Path) -> None:
    # A harness-failure count nobody can trace to a task and a reason cannot
    # be acted on.
    suite_of(tmp_path, "pagination-last-page", "interval-merge")
    broken = tmp_path / "suite" / "interval-merge.toml"
    text = broken.read_text()
    start = text.index('sha256 = "') + len('sha256 = "')
    end = text.index('"', start)
    # Repin the fixture to a digest it cannot have, so the harness refuses the
    # run rather than producing a result nobody could reproduce.
    broken.write_text(text[:start] + "f" * 64 + text[end:])
    assert bench(tmp_path) == 1

    report(tmp_path, "--out", str(tmp_path / "report.md"))

    rendered = read_report(tmp_path)
    assert "### Produced no measurement (1)" in rendered
    assert "| interval-merge | fixture_hash_mismatch | preparing |" in rendered


def test_the_same_database_renders_the_same_bytes_apart_from_the_timestamp(
    tmp_path: Path,
) -> None:
    # A report that reordered itself between two runs could not be diffed,
    # which is the whole reason for choosing Markdown over anything else.
    bench(tmp_path, "pagination-last-page", "interval-merge", "cache-eviction")

    report(tmp_path, "--out", str(tmp_path / "report.md"))
    first = read_report(tmp_path)
    report(tmp_path, "--out", str(tmp_path / "report.md"))
    second = read_report(tmp_path)

    def without_timestamp(text: str) -> list[str]:
        return [line for line in text.splitlines() if not line.startswith("Generated")]

    assert without_timestamp(first) == without_timestamp(second)


# --- the command itself ------------------------------------------------------


def test_report_writes_to_stdout_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bench(tmp_path, "pagination-last-page")
    capsys.readouterr()

    assert report(tmp_path, "--out", "-") == 0

    out = capsys.readouterr().out
    assert out.startswith("# AgentFoundry benchmark report")
    assert not (tmp_path / "report.md").exists()


def test_report_over_an_empty_database_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A report of nothing that exits 0 is a green CI job asserting that zero
    # tasks passed -- the same trap `bench` refuses on an empty directory.
    with SqliteRunRepository(tmp_path / "af.sqlite3"):
        pass

    assert report(tmp_path, "--out", "-") == 2
    assert "no runs" in capsys.readouterr().err


def test_report_without_a_database_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert report(tmp_path, "--out", "-") == 2
    assert "no database" in capsys.readouterr().err


def test_report_reads_every_run_not_the_listing_default(tmp_path: Path) -> None:
    """`list_runs` defaults to 50, and a report must not inherit that.

    A benchmark whose denominator was silently capped would look complete and
    be wrong, and would go on looking complete as it got further from the
    truth.
    """
    bench(tmp_path, "pagination-last-page")
    with SqliteRunRepository(tmp_path / "af.sqlite3") as repository:
        for index in range(60):
            repository.create(a_created_run(f"run-{index:04d}", "interval-merge"))
        assert len(repository.list_runs()) == 50
        assert len(repository.list_runs(limit=None)) == 61

    report(tmp_path, "--out", str(tmp_path / "report.md"))

    assert "60 run(s) had not finished" in read_report(tmp_path)


def test_repeated_tasks_get_a_row_saying_whether_their_runs_agreed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Benched twice into one database: two runs per task, compared per task."""
    bench(tmp_path, "pagination-last-page", "interval-merge")
    bench(tmp_path)
    capsys.readouterr()

    report(tmp_path, "--out", str(tmp_path / "report.md"))

    text = read_report(tmp_path)
    assert "2 of 2 task(s) agree across their repeats" in text
    assert "| interval-merge | 2 | 2/2 | clean_patch | 1 | 1 |" in text


def test_a_single_pass_gets_no_agreement_table(tmp_path: Path) -> None:
    bench(tmp_path, "pagination-last-page", "interval-merge")

    report(tmp_path, "--out", str(tmp_path / "report.md"))

    assert "agree across their repeats" not in read_report(tmp_path)


@pytest.mark.postgres
@requires_postgres
def test_a_suite_benched_into_postgres_reports_like_one_in_sqlite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The commands take a URL where they took a path, and nothing else moves."""
    directory = suite_of(tmp_path, "pagination-last-page", "interval-merge")
    with fresh_schema() as url:
        code = main(
            [
                "bench",
                str(directory),
                "--db",
                url,
                "--runs",
                str(tmp_path / "runs"),
                "--fixtures",
                str(FIXTURES),
                "--solutions",
                str(SOLUTIONS),
                "--runner",
                "local",
            ]
        )
        capsys.readouterr()
        assert code == 0

        assert main(["report", "--db", url, "--out", "-"]) == 0

    text = capsys.readouterr().out
    assert "| resolved | 2/2 |" in text
    assert "| interval-merge | clean_patch | yes | no |" in text
