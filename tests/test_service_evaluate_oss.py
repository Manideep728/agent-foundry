"""The orchestrator's part in a real-repository task (D39).

A recording runner stands in for Docker: it captures the request and looks at
the workspace at the moment validation would start, then stops the run. That
moment is the one these rules are about.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from control_plane.adapters.fake_agent import FakeAgent
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.task_toml import load_task
from control_plane.adapters.test_patch import GitTestPatcher
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.outcomes import FailureCategory
from control_plane.domain.task import Environment, Suite, TaskDefinition
from control_plane.services.errors import RunnerFailed
from control_plane.services.evaluate import EvaluationService
from control_plane.services.ports import GradingPatcher, ValidationRequest
from tests.fakes import FakeClock, FakeIdGenerator

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tasks" / "fixtures"
SOLUTIONS = REPO / "tasks" / "solutions"
TASK_FILE = REPO / "tasks" / "csv-quoted-fields.toml"

GRADING_PATCH = """\
diff --git a/tests/test_graded.py b/tests/test_graded.py
new file mode 100644
--- /dev/null
+++ b/tests/test_graded.py
@@ -0,0 +1 @@
+GRADED = True
"""


class RecordingRunner:
    runner_id = "recording"
    runner_version = "recording 0"
    isolated = True
    image_digest = None

    def __init__(self) -> None:
        self.request: ValidationRequest | None = None
        self.graded_file_present = False

    def run(self, request: ValidationRequest) -> object:
        self.request = request
        self.graded_file_present = (request.workdir / "tests/test_graded.py").is_file()
        raise RunnerFailed("stopped by the test once the request was seen")


def build(
    tmp_path: Path,
    runner: RecordingRunner,
    *,
    patcher: GradingPatcher | None = None,
    fixtures_root: Path = FIXTURES,
    oss_fixtures_root: Path | None = None,
) -> EvaluationService:
    clock = FakeClock()
    return EvaluationService(
        repository=SqliteRunRepository(tmp_path / "af.sqlite3"),
        runner=runner,  # type: ignore[arg-type]
        agent=FakeAgent(SOLUTIONS, clock),
        workspaces=WorkspaceFactory(tmp_path / "runs"),
        clock=clock,
        ids=FakeIdGenerator(),
        fixtures_root=fixtures_root,
        harness_version=None,
        oss_fixtures_root=oss_fixtures_root,
        grading_patcher=patcher,
    )


def graded_task(patch: str = GRADING_PATCH) -> TaskDefinition:
    return replace(load_task(TASK_FILE), test_patch=patch)


def test_the_graded_tests_are_in_place_when_validation_starts(tmp_path: Path) -> None:
    runner = RecordingRunner()

    build(tmp_path, runner, patcher=GitTestPatcher()).execute(graded_task())

    assert runner.graded_file_present


def test_the_graded_tests_are_not_part_of_the_agents_patch(tmp_path: Path) -> None:
    """The patch is what the agent is credited with; the tests are the task's."""
    runner = RecordingRunner()

    build(tmp_path, runner, patcher=GitTestPatcher()).execute(graded_task())

    assert runner.request is not None
    recorded = (runner.request.workdir.parent / "patch.diff").read_text()
    assert "csvkit/fields.py" in recorded
    assert "test_graded" not in recorded


def test_a_test_patch_that_does_not_apply_makes_the_task_invalid(
    tmp_path: Path,
) -> None:
    stale = (
        "--- a/tests/test_basic.py\n+++ b/tests/test_basic.py\n"
        "@@ -1 +1 @@\n-no such line\n+replacement\n"
    )
    runner = RecordingRunner()

    record = build(tmp_path, runner, patcher=GitTestPatcher()).execute(
        graded_task(stale)
    )

    assert record.run.failure is not None
    assert record.run.failure.kind is FailureCategory.TASK_INVALID
    assert "does not apply" in record.run.failure.detail
    assert runner.request is None, "validation must not start"


def test_a_test_patch_with_no_patcher_is_refused_rather_than_skipped(
    tmp_path: Path,
) -> None:
    """Skipping it would validate against tests that were never added."""
    record = build(tmp_path, RecordingRunner()).execute(graded_task())

    assert record.run.failure is not None
    assert record.run.failure.kind is FailureCategory.TASK_INVALID
    assert "no patcher" in record.run.failure.detail


def test_an_oss_task_validates_in_its_image_from_the_oss_fixtures(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    oss = replace(
        load_task(TASK_FILE),
        suite=Suite.OSS,
        environment=Environment(image="afrunner-oss:csv", workdir="/testbed"),
    )

    build(
        tmp_path,
        runner,
        fixtures_root=tmp_path / "no-synthetic-fixtures-here",
        oss_fixtures_root=FIXTURES,
    ).execute(oss)

    assert runner.request is not None
    assert (runner.request.image, runner.request.container_workdir) == (
        "afrunner-oss:csv",
        "/testbed",
    )
