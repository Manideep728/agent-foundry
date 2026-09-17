"""The orchestrator.

The happy path is one test. The rest are failure branches, because getting a
failure into the wrong category is how an infrastructure problem ends up
depressing an agent's score.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.adapters.fake_agent import FakeAgent
from control_plane.adapters.local_runner import LocalRunner
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.task_toml import load_task
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.errors import PreconditionViolated
from control_plane.domain.model import MicroUsd, ModelInvocation, ModelUsage
from control_plane.domain.outcomes import AgentOutcome, FailureCategory
from control_plane.domain.states import RunState
from control_plane.domain.task import TaskDefinition
from control_plane.domain.validation import (
    SCHEMA_VERSION,
    CommandOutcome,
    CommandResult,
    ExitReason,
    StreamCapture,
    ValidationReport,
)
from control_plane.services.errors import AgentError, RunnerFailed, RunnerUnavailable
from control_plane.services.evaluate import EvaluationService
from control_plane.services.ports import AgentResult, ValidationRequest
from tests.fakes import FakeIdGenerator

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tasks" / "fixtures"
SOLUTIONS = REPO / "tasks" / "solutions"
TASK_FILE = REPO / "tasks" / "csv-quoted-fields.toml"


class RealClock:
    def now_unix_ms(self) -> int:
        import time

        return int(time.time() * 1000)

    def monotonic_ms(self) -> int:
        import time

        return int(time.monotonic() * 1000)


def build(
    tmp_path: Path,
    runner: object | None = None,
    agent: object | None = None,
    solutions: Path | None = None,
) -> tuple[EvaluationService, SqliteRunRepository]:
    clock = RealClock()
    repo = SqliteRunRepository(tmp_path / "af.sqlite3")
    service = EvaluationService(
        repository=repo,
        runner=runner or LocalRunner(clock),  # type: ignore[arg-type]
        agent=agent or FakeAgent(solutions or SOLUTIONS, clock),  # type: ignore[arg-type]
        workspaces=WorkspaceFactory(tmp_path / "runs"),
        clock=clock,
        ids=FakeIdGenerator(),
        fixtures_root=FIXTURES,
    )
    return service, repo


def task() -> TaskDefinition:
    return load_task(TASK_FILE)


# --- the happy path ----------------------------------------------------------


def test_a_solved_task_is_scored_as_a_clean_patch(tmp_path: Path) -> None:
    service, repo = build(tmp_path)
    record = service.execute(task())

    assert record.run.state is RunState.SCORED
    assert record.outcome is AgentOutcome.CLEAN_PATCH
    assert record.score is not None
    assert record.score.resolved
    assert not record.score.regressed
    assert record.patch_is_empty is False
    assert record.total_cost_micros == 0  # the fake model is free
    repo.close()


def test_a_do_nothing_agent_is_scored_not_failed(tmp_path: Path) -> None:
    # An agent that changes nothing is a measurement, not a harness failure.
    service, repo = build(tmp_path, solutions=tmp_path / "no-solutions")
    record = service.execute(task())

    assert record.run.state is RunState.SCORED
    assert record.outcome is AgentOutcome.NO_PATCH_PRODUCED
    assert record.patch_is_empty is True
    assert record.score is not None and not record.score.resolved
    repo.close()


# --- failure branches --------------------------------------------------------


def test_a_fixture_that_does_not_match_its_pin_fails_before_spending_anything(
    tmp_path: Path,
) -> None:
    service, repo = build(tmp_path)
    broken = load_task(TASK_FILE)
    object.__setattr__(broken, "fixture_sha256", "f" * 64)

    record = service.execute(broken)

    assert record.run.state is RunState.FAILED
    assert record.run.failure is not None
    assert record.run.failure.kind is FailureCategory.FIXTURE_HASH_MISMATCH
    assert record.run.failure.failed_from is RunState.PREPARING
    assert record.invocations == ()  # nothing was paid for
    # Named even though WorkspaceFactory.create never ran. A row pointing at a
    # directory that does not exist is harmless; the reverse -- a directory no
    # row names -- is the leak recovery cannot see.
    assert record.workspace_path == str(tmp_path / "runs" / "run-0001")
    assert not (tmp_path / "runs" / "run-0001").exists()
    repo.close()


class SilentAgent:
    """Changes nothing and reports whatever outcome it is given.

    Stands in for a model whose reply could not be read as an edit: the
    workspace is untouched either way, so the only thing separating "the model
    broke the output contract" from "the model chose not to edit" is what the
    agent says about itself.
    """

    provider_id = "stub"

    def __init__(self, outcome: AgentOutcome | None) -> None:
        self._outcome = outcome

    def edit(self, workspace: Path, task: TaskDefinition) -> AgentResult:
        invocation = ModelInvocation(
            provider_id="stub",
            model_id="stub-1",
            usage=ModelUsage(input_tokens=10, output_tokens=5),
            cost=MicroUsd(0),
            latency_ms=1,
        )
        return AgentResult((invocation,), outcome=self._outcome)


def test_a_malformed_reply_is_scored_as_malformed_not_as_no_patch(
    tmp_path: Path,
) -> None:
    """The precedence that makes the malformed-output rate measurable at all.

    Both agents below leave the workspace untouched, so both produce an empty
    patch and `classify` returns NO_PATCH_PRODUCED for each. Only the agent's
    own verdict tells them apart. Let classification win and every malformed
    reply is recorded as an agent that ran cleanly and chose to change nothing,
    and the rate reads zero no matter how badly a model behaves.
    """
    service, repo = build(tmp_path, agent=SilentAgent(AgentOutcome.MALFORMED_OUTPUT))

    record = service.execute(task())

    assert record.run.state is RunState.SCORED
    assert record.outcome is AgentOutcome.MALFORMED_OUTPUT
    assert record.patch_is_empty is True
    repo.close()


def test_an_agent_with_no_verdict_still_gets_the_classified_outcome(
    tmp_path: Path,
) -> None:
    """The override is opt-in. Silence must not change the existing scoring."""
    service, repo = build(tmp_path, agent=SilentAgent(None))

    record = service.execute(task())

    assert record.run.state is RunState.SCORED
    assert record.outcome is AgentOutcome.NO_PATCH_PRODUCED
    repo.close()


def test_every_invocation_an_agent_reports_is_recorded(tmp_path: Path) -> None:
    """A multi-turn agent's cost is the sum of its calls, not its last one."""

    class TwoCallAgent:
        provider_id = "stub"

        def edit(self, workspace: Path, task: TaskDefinition) -> AgentResult:
            def call(cost: int) -> ModelInvocation:
                return ModelInvocation(
                    provider_id="stub",
                    model_id="stub-1",
                    usage=ModelUsage(input_tokens=10, output_tokens=5),
                    cost=MicroUsd(cost),
                    latency_ms=1,
                )

            return AgentResult((call(7), call(11)))

    service, repo = build(tmp_path, agent=TwoCallAgent())
    record = service.execute(task())

    assert len(record.invocations) == 2
    assert record.total_cost_micros == 18
    stored = repo.get(record.run.run_id)
    assert stored is not None
    assert len(stored.invocations) == 2
    repo.close()


def test_an_agent_that_raises_is_an_agent_error(tmp_path: Path) -> None:
    class ExplodingAgent:
        provider_id = "exploding"

        def edit(self, workspace: Path, task: TaskDefinition) -> object:
            raise AgentError("rate limit exhausted")

    service, repo = build(tmp_path, agent=ExplodingAgent())
    record = service.execute(task())

    assert record.run.failure is not None
    assert record.run.failure.kind is FailureCategory.AGENT_ERROR
    assert record.run.failure.failed_from is RunState.AGENT_RUNNING
    repo.close()


def test_an_unavailable_runner_is_infrastructure_not_the_agent(
    tmp_path: Path,
) -> None:
    # The distinction that keeps a Docker outage out of the agent's score.
    class DeadRunner:
        runner_id = "docker"
        runner_version = "docker 0"
        isolated = True
        image_digest = None

        def run(self, request: ValidationRequest) -> ValidationReport:
            raise RunnerUnavailable("cannot connect to the Docker daemon")

    service, repo = build(tmp_path, runner=DeadRunner())
    record = service.execute(task())

    assert record.run.failure is not None
    assert record.run.failure.kind is FailureCategory.INFRASTRUCTURE_FAILED
    assert record.run.failure.failed_from is RunState.VALIDATING
    assert record.outcome is None
    repo.close()


def test_a_runner_that_produces_no_result_is_a_runner_failure(
    tmp_path: Path,
) -> None:
    class BrokenRunner:
        runner_id = "docker"
        runner_version = "docker 0"
        isolated = True
        image_digest = None

        def run(self, request: ValidationRequest) -> ValidationReport:
            raise RunnerFailed("result.json was truncated")

    service, repo = build(tmp_path, runner=BrokenRunner())
    record = service.execute(task())

    assert record.run.failure is not None
    assert record.run.failure.kind is FailureCategory.RUNNER_FAILED
    repo.close()


def stub_report(
    commands: tuple[CommandResult, ...], aborted: bool = False
) -> ValidationReport:
    return ValidationReport(
        schema_version=SCHEMA_VERSION,
        runner_version="stub",
        arch="arm64",
        pid1=False,
        shell="/bin/sh",
        workdir="/work",
        started_at_unix_ms=1,
        wall_ms=1,
        exit_reason=ExitReason.SIGNAL if aborted else ExitReason.COMPLETED,
        aborted=aborted,
        orphans_reaped_total=0,
        commands=commands,
    )


def result(index: int, command: str, **overrides: object) -> CommandResult:
    empty = StreamCapture(
        bytes_total=0, bytes_written=0, truncated=False, path="p", tail=""
    )
    fields: dict[str, object] = {
        "index": index,
        "command": command,
        "started_at_unix_ms": 1,
        "duration_ms": 1,
        "outcome": CommandOutcome.EXITED,
        "stdout": empty,
        "stderr": empty,
        "exit_code": 0,
    }
    fields.update(overrides)
    return CommandResult(**fields)  # type: ignore[arg-type]


class StubRunner:
    runner_id = "stub"
    runner_version = "stub 0"
    isolated = False
    image_digest = None

    def __init__(self, report: ValidationReport) -> None:
        self._report = report

    def run(self, request: ValidationRequest) -> ValidationReport:
        return self._report


def test_a_command_that_never_ran_is_incomplete_not_a_low_score(
    tmp_path: Path,
) -> None:
    # A docker stop mid-benchmark would otherwise turn every interrupted run
    # into a row saying the agent failed.
    lines = task().command_file_lines()
    report = stub_report(
        (
            result(0, lines[0]),
            result(
                1,
                lines[1],
                outcome=CommandOutcome.NOT_RUN,
                exit_code=None,
                duration_ms=0,
            ),
        )
    )
    service, repo = build(tmp_path, runner=StubRunner(report))
    record = service.execute(task())

    assert record.run.state is RunState.FAILED
    assert record.run.failure is not None
    assert record.run.failure.kind is FailureCategory.VALIDATION_INCOMPLETE
    assert record.outcome is None
    repo.close()


def test_a_command_that_never_started_is_a_runner_failure(tmp_path: Path) -> None:
    # EAGAIN against a pids limit is the host's problem, not the patch's.
    lines = task().command_file_lines()
    report = stub_report(
        (
            result(0, lines[0]),
            result(
                1,
                lines[1],
                outcome=CommandOutcome.SPAWN_FAILED,
                exit_code=None,
                spawn_error="EAGAIN",
            ),
        )
    )
    service, repo = build(tmp_path, runner=StubRunner(report))
    record = service.execute(task())

    assert record.run.failure is not None
    assert record.run.failure.kind is FailureCategory.RUNNER_FAILED
    repo.close()


def test_a_task_whose_empty_patch_resolves_it_is_marked_invalid(
    tmp_path: Path,
) -> None:
    # The continuous benchmark audit. The evidence is kept, because without it
    # the defect is unactionable.
    lines = task().command_file_lines()
    report = stub_report((result(0, lines[0]), result(1, lines[1])))
    service, repo = build(
        tmp_path, runner=StubRunner(report), solutions=tmp_path / "none"
    )
    record = service.execute(task())

    assert record.run.failure is not None
    assert record.run.failure.kind is FailureCategory.TASK_INVALID
    assert record.task_defect
    assert record.score is not None  # evidence kept
    assert record.report is not None
    assert record.outcome is None  # but no verdict
    repo.close()


def test_a_regression_is_reported_as_such(tmp_path: Path) -> None:
    lines = task().command_file_lines()
    report = stub_report((result(0, lines[0]), result(1, lines[1], exit_code=1)))
    service, repo = build(tmp_path, runner=StubRunner(report))
    record = service.execute(task())

    assert record.run.state is RunState.SCORED
    assert record.outcome is AgentOutcome.PATCH_WITH_REGRESSIONS
    repo.close()


# --- ordering and persistence ------------------------------------------------


def test_every_transition_is_persisted_in_order(tmp_path: Path) -> None:
    # Persisting VALIDATING *before* invoking the runner is what makes orphan
    # container recovery possible. Recorded after, a crash mid-validation
    # leaves a live container under a run whose recovery action is NOTHING.
    seen: list[RunState] = []

    class RecordingRunner(LocalRunner):
        def run(self, request: ValidationRequest) -> ValidationReport:
            stored = repo.get(request.run_id)
            assert stored is not None
            seen.append(stored.run.state)
            return super().run(request)

    clock = RealClock()
    repo = SqliteRunRepository(tmp_path / "af.sqlite3")
    service = EvaluationService(
        repository=repo,
        runner=RecordingRunner(clock),
        agent=FakeAgent(SOLUTIONS, clock),
        workspaces=WorkspaceFactory(tmp_path / "runs"),
        clock=clock,
        ids=FakeIdGenerator(),
        fixtures_root=FIXTURES,
    )
    service.execute(task())

    assert seen == [RunState.VALIDATING]
    repo.close()


def test_the_stored_run_matches_the_returned_record(tmp_path: Path) -> None:
    service, repo = build(tmp_path)
    record = service.execute(task())

    stored = repo.get(record.run.run_id)
    assert stored is not None
    assert stored.run.state is record.run.state
    assert stored.outcome is record.outcome
    assert stored.score == record.score
    repo.close()


def test_the_working_copy_is_removed_on_success_but_evidence_is_kept(
    tmp_path: Path,
) -> None:
    service, repo = build(tmp_path)
    record = service.execute(task())

    assert record.workspace_path is not None
    root = Path(record.workspace_path)
    assert not (root / "repo").exists()
    assert (root / "patch.diff").is_file()
    assert (root / "manifest.txt").is_file()
    repo.close()


def test_a_failed_run_keeps_its_workspace_for_inspection(tmp_path: Path) -> None:
    class BrokenRunner:
        runner_id = "stub"
        runner_version = "stub 0"
        isolated = False
        image_digest = None

        def run(self, request: ValidationRequest) -> ValidationReport:
            raise RunnerFailed("nothing came back")

    service, repo = build(tmp_path, runner=BrokenRunner())
    record = service.execute(task())

    assert (tmp_path / "runs" / "run-0001" / "repo").is_dir()
    # Keeping the evidence is only useful if the record says where it is. A
    # failed run never reaches the scoring path, which is the only place that
    # used to supply this.
    assert record.workspace_path == str(tmp_path / "runs" / "run-0001")
    stored = repo.get(record.run.run_id)
    assert stored is not None
    assert stored.workspace_path == record.workspace_path
    repo.close()


def test_a_precondition_violation_is_not_swallowed(tmp_path: Path) -> None:
    # PreconditionViolated means this code called the domain wrongly. Catching
    # it and mapping it to a category is how a forgotten check would start
    # producing plausible numbers instead of crashing.
    class LyingRunner:
        runner_id = "stub"
        runner_version = "stub 0"
        isolated = False
        image_digest = None

        def run(self, request: ValidationRequest) -> ValidationReport:
            raise PreconditionViolated("the domain was called wrongly")

    service, repo = build(tmp_path, runner=LyingRunner())
    with pytest.raises(PreconditionViolated):
        service.execute(task())
    repo.close()


# --- the image digest reaches the row ---------------------------------------


class ImagedRunner(LocalRunner):
    """A runner that names its image, as the Docker runner does.

    Subclasses the real local runner rather than stubbing a report, so the
    digest travels the whole path a real run takes: adapter property ->
    _base_record -> finish() -> the row -> _hydrate.
    """

    @property
    def image_digest(self) -> str | None:
        return "sha256:" + "b" * 64


def test_a_run_that_validated_records_the_image_it_validated_in(
    tmp_path: Path,
) -> None:
    """The digest must survive the round trip, not merely reach the record.

    Every environment fact that has gone wrong in this project went wrong in
    storage rather than in memory, and was invisible because the assertion was
    made on the object the test had just built.
    """
    service, repo = build(tmp_path, runner=ImagedRunner(RealClock()))
    record = service.execute(load_task(TASK_FILE))

    stored = repo.get(record.run.run_id)
    assert stored is not None
    assert stored.image_digest == "sha256:" + "b" * 64


def test_a_run_on_a_runner_with_no_image_records_none(tmp_path: Path) -> None:
    # The local runner has no image. Storing "" or "none" here would let an
    # unisolated run satisfy `image_digest IS NOT NULL`.
    service, repo = build(tmp_path)
    record = service.execute(load_task(TASK_FILE))

    stored = repo.get(record.run.run_id)
    assert stored is not None
    assert stored.image_digest is None
