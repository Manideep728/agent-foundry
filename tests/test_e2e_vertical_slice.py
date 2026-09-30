"""One task, end to end, against a fixture that genuinely starts broken.

Everything real except the agent: the real filesystem, the real local runner
executing real `unittest` processes, real SQLite in a temporary directory, and
the real system clock, because the run's durations are real durations.

This catches what no unit test can. The failure modes it exists for are wiring
failures: the workspace not being the directory the commands actually run in,
command index 1 in the report not corresponding to the `pass_to_pass` line in
the TOML, a patch captured after test artefacts appeared, or a SQLite write
that silently lost the score.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from control_plane.adapters.fake_agent import FakeAgent
from control_plane.adapters.fixtures import digest_tree
from control_plane.adapters.local_runner import LocalRunner
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.system import SystemClock, harness_version
from control_plane.adapters.task_toml import fixture_root_for, load_task
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.outcomes import AgentOutcome
from control_plane.domain.states import RunState
from control_plane.domain.task import CommandRole
from control_plane.domain.validation import CommandOutcome, ExitReason
from control_plane.services.evaluate import EvaluationService
from tests.fakes import FakeIdGenerator

REPO = Path(__file__).resolve().parent.parent
TASK_FILE = REPO / "tasks" / "csv-quoted-fields.toml"
FIXTURES = REPO / "tasks" / "fixtures"
SOLUTIONS = REPO / "tasks" / "solutions"


def test_one_task_runs_end_to_end(tmp_path: Path) -> None:
    task = load_task(TASK_FILE)
    fixture_root = fixture_root_for(task, FIXTURES)

    # --- 1. the task pins the fixture that is actually on disk --------------
    pinned_digest, _ = digest_tree(fixture_root)
    assert task.fixture_sha256 == pinned_digest

    # --- 2. the fixture really is broken before the patch -------------------
    # Asserted, not assumed. Without this the whole test could pass against a
    # fixture that was never broken, and every agent would resolve it for free.
    f2p = task.commands_with_role(CommandRole.FAIL_TO_PASS)[0]
    p2p = task.commands_with_role(CommandRole.PASS_TO_PASS)[0]

    baseline_f2p = subprocess.run(
        [sys.executable, "-m", "unittest", f2p.command.split()[-1]],
        cwd=fixture_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    baseline_p2p = subprocess.run(
        [sys.executable, "-m", "unittest", p2p.command.split()[-1]],
        cwd=fixture_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert baseline_f2p.returncode != 0, "the fail-to-pass tests already pass"
    assert baseline_p2p.returncode == 0, "the regression guard is already failing"

    # --- 3. run the pipeline ------------------------------------------------
    clock = SystemClock()
    database = tmp_path / "af.sqlite3"
    with SqliteRunRepository(database) as repository:
        service = EvaluationService(
            repository=repository,
            runner=LocalRunner(clock),
            agent=FakeAgent(SOLUTIONS, clock),
            workspaces=WorkspaceFactory(tmp_path / "runs"),
            clock=clock,
            ids=FakeIdGenerator(),
            fixtures_root=FIXTURES,
            harness_version=harness_version(),
        )
        record = service.execute(task)

    # --- 4. the verdict -----------------------------------------------------
    assert record.run.state is RunState.SCORED
    assert record.outcome is AgentOutcome.CLEAN_PATCH
    assert record.score is not None
    assert record.score.resolved
    assert not record.score.regressed
    assert record.score.fail_to_pass_passed == record.score.fail_to_pass_total == 1
    assert record.score.pass_to_pass_passed == record.score.pass_to_pass_total == 1
    assert record.score.timed_out_commands == 0
    assert not record.task_defect

    # --- 5. the evidence ----------------------------------------------------
    assert record.report is not None
    assert record.report.exit_reason is ExitReason.COMPLETED
    assert not record.report.aborted
    assert len(record.report.commands) == 2
    for result in record.report.commands:
        assert result.outcome is CommandOutcome.EXITED
        assert result.exit_code == 0
        assert result.succeeded
    # Index order is the role mapping. If these drifted, results would be
    # scored against the wrong role and the verdict could silently invert.
    assert record.report.commands[0].command == f2p.command
    assert record.report.commands[1].command == p2p.command

    assert record.patch_is_empty is False
    assert record.patch_bytes > 0
    assert record.workspace_path is not None
    workspace_root = Path(record.workspace_path)
    patch_text = (workspace_root / "patch.diff").read_text()
    assert "csvkit/fields.py" in patch_text
    assert "in_quotes" in patch_text

    # The runner is honest about not being the real one.
    assert record.pid1 is False
    assert "no isolation" in record.runner_version

    # --- 6. the fixture was never touched -----------------------------------
    assert digest_tree(fixture_root)[0] == pinned_digest

    # --- 7. it survives a round trip through storage -------------------------
    with SqliteRunRepository(database) as repository:
        stored = repository.get(record.run.run_id)

    assert stored is not None
    assert stored.run.state is RunState.SCORED
    assert stored.outcome is AgentOutcome.CLEAN_PATCH
    assert stored.score == record.score
    assert stored.report is not None
    assert len(stored.report.commands) == 2
    assert stored.patch_sha256 == record.patch_sha256
    # The tests run from a checkout, so the build must be able to name itself.
    assert stored.harness_version is not None
    assert stored.harness_version == harness_version()

    # --- 8. the working copy is gone, the evidence is not --------------------
    assert not (workspace_root / "repo").exists()
    assert (workspace_root / "manifest.txt").is_file()
    assert (workspace_root / "out" / "logs" / "cmd-000.stdout.log").is_file()


def test_the_same_task_run_twice_produces_two_comparable_records(
    tmp_path: Path,
) -> None:
    # The claim in the README: the same task run twice yields two comparable
    # records. Comparable means same verdict and same fixture, not identical
    # timings.
    task = load_task(TASK_FILE)
    clock = SystemClock()
    database = tmp_path / "af.sqlite3"

    with SqliteRunRepository(database) as repository:
        service = EvaluationService(
            repository=repository,
            runner=LocalRunner(clock),
            agent=FakeAgent(SOLUTIONS, clock),
            workspaces=WorkspaceFactory(tmp_path / "runs"),
            clock=clock,
            ids=FakeIdGenerator(),
            fixtures_root=FIXTURES,
            harness_version=None,
        )
        first = service.execute(task)
        second = service.execute(task)

        assert first.run.run_id != second.run.run_id
        assert first.outcome is second.outcome
        assert first.score == second.score
        assert first.fixture_sha256 == second.fixture_sha256
        assert first.patch_sha256 == second.patch_sha256
        assert len(repository.list_runs()) == 2
