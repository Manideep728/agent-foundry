"""Driving one task from nothing to a measurement.

Two rules shape the order of operations here.

**Persist before acting.** Each state is written before the step it authorises
begins. The one that matters most on a small Docker VM is VALIDATING: written
after invoking the runner instead of before, a crash mid-validation leaves a
container alive and a run recorded as AGENT_RUNNING, whose recovery action is
`NOTHING` -- so nothing cleans the container up, and the symptom is the *next*
run failing for an unrelated reason.

**Never catch what you cannot classify.** `PreconditionViolated` means this
code called a domain function wrongly; letting it escape as a crash is what
stops a forgotten check from producing a plausible number. An unexpected
exception is likewise not mapped to a category -- doing so would record an
internal bug as an infrastructure failure forever. The run is left non-terminal
and the recovery scan marks it INTERRUPTED.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from control_plane.adapters.diffing import diff_workspace
from control_plane.adapters.fixtures import digest_tree
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.errors import DomainError
from control_plane.domain.model import total_micros
from control_plane.domain.outcomes import AgentOutcome, FailureCategory
from control_plane.domain.patch import Patch
from control_plane.domain.ports import Clock, IdGenerator
from control_plane.domain.record import RunRecord
from control_plane.domain.scoring import (
    Score,
    check_report_matches_task,
    classify,
    is_task_defect,
    score,
)
from control_plane.domain.states import EvaluationRun, RunState
from control_plane.domain.task import TaskDefinition
from control_plane.domain.validation import ValidationReport
from control_plane.services.errors import (
    AgentError,
    RunnerFailed,
    RunnerUnavailable,
)
from control_plane.services.ports import (
    AgentPort,
    AgentResult,
    ValidationRequest,
    ValidationRunner,
)

UNKNOWN_ARCH = "unknown"
"""Recorded until a report supplies the real one.

A run that never reached validation genuinely does not know what architecture
it would have run on, and a benchmark table that filters on arch will exclude
it -- which is correct, because it produced no measurement.
"""


class EvaluationService:
    def __init__(
        self,
        *,
        repository: SqliteRunRepository,
        runner: ValidationRunner,
        agent: AgentPort,
        workspaces: WorkspaceFactory,
        clock: Clock,
        ids: IdGenerator,
        fixtures_root: Path,
    ) -> None:
        self._repo = repository
        self._runner = runner
        self._agent = agent
        self._workspaces = workspaces
        self._clock = clock
        self._ids = ids
        self._fixtures_root = fixtures_root

    def execute(self, task: TaskDefinition) -> RunRecord:
        run = EvaluationRun.create(
            self._ids.new_run_id(), task.task_id, self._clock.now_unix_ms()
        )
        base = self._base_record(run, task)
        # Written before anything creates external state, and already naming
        # the workspace it is about to create: a directory nothing knows about
        # is invisible to the recovery scan, while a row naming a directory
        # that was never made is harmless -- the cleanup is an rmtree that
        # ignores a missing path.
        self._repo.create(base)

        workspace = None
        record: RunRecord | None = None
        try:
            run = self._advance(run, RunState.PREPARING)

            fixture_root = self._fixtures_root / task.fixture_id
            try:
                digest, entries = digest_tree(fixture_root)
            except DomainError as error:
                record = self._fail(
                    run, task, FailureCategory.WORKSPACE_SETUP_FAILED, str(error)
                )
                return record
            if digest != task.fixture_sha256:
                record = self._fail(
                    run,
                    task,
                    FailureCategory.FIXTURE_HASH_MISMATCH,
                    f"fixture is {digest}, task pins {task.fixture_sha256}",
                )
                return record

            try:
                workspace = self._workspaces.create(run.run_id)
                self._workspaces.copy_fixture(workspace, fixture_root)
            except DomainError as error:
                record = self._fail(
                    run, task, FailureCategory.WORKSPACE_SETUP_FAILED, str(error)
                )
                return record

            run = self._advance(run, RunState.AGENT_RUNNING)
            agent_started = self._clock.monotonic_ms()
            try:
                agent_result = self._agent.edit(workspace.repo, task)
            except AgentError as error:
                record = self._fail(run, task, FailureCategory.AGENT_ERROR, str(error))
                return record
            agent_ms = self._clock.monotonic_ms() - agent_started

            # Captured before validation runs, so the test artefacts validation
            # writes cannot reach the patch even if an exclusion rule slips.
            patch = diff_workspace(entries, fixture_root, workspace.repo)
            workspace.patch_path.write_text(patch.unified_diff, encoding="utf-8")

            run = self._advance(run, RunState.VALIDATING)
            validation_started = self._clock.monotonic_ms()
            try:
                report = self._runner.run(
                    ValidationRequest(
                        run_id=run.run_id,
                        workdir=workspace.repo,
                        output_dir=workspace.out,
                        log_dir=workspace.logs,
                        commands=task.command_file_lines(),
                        per_command_timeout_ms=task.per_command_timeout_ms,
                        total_timeout_ms=task.total_timeout_ms,
                    )
                )
            except RunnerUnavailable as error:
                record = self._fail(
                    run, task, FailureCategory.INFRASTRUCTURE_FAILED, str(error)
                )
                return record
            except RunnerFailed as error:
                record = self._fail(
                    run, task, FailureCategory.RUNNER_FAILED, str(error)
                )
                return record
            validation_ms = self._clock.monotonic_ms() - validation_started

            record = self._score(
                run,
                task,
                report,
                patch,
                agent_result,
                agent_ms,
                validation_ms,
            )
            return record
        except KeyboardInterrupt:
            # An abort is expected operator behaviour, not a defect. Kept out
            # of FAILED so harness reliability is not polluted by every ^C.
            aborted = run.abort(self._clock.now_unix_ms())
            self._repo.finish(self._base_record(aborted, task), expected=run.state)
            raise
        finally:
            # Keyed off the record that was actually produced, not off `run`:
            # the SCORED transition happens inside _score on its own value, so
            # `run` is still VALIDATING here and the working copy would never
            # be reclaimed. A failed run keeps everything, deliberately -- a
            # harness failure is a bug to debug, and deleting the evidence is
            # the worst available default.
            if (
                workspace is not None
                and record is not None
                and record.run.state is RunState.SCORED
            ):
                self._workspaces.discard_repo(workspace)

    # --- steps ---------------------------------------------------------------

    def _advance(self, run: EvaluationRun, state: RunState) -> EvaluationRun:
        previous = run.state
        moved = run.transition_to(state, self._clock.now_unix_ms())
        self._repo.advance(moved, expected=previous)
        return moved

    def _score(
        self,
        run: EvaluationRun,
        task: TaskDefinition,
        report: ValidationReport,
        patch: Patch,
        agent_result: AgentResult,
        agent_ms: int,
        validation_ms: int,
    ) -> RunRecord:
        mismatch = check_report_matches_task(task, report)
        if mismatch is not None:
            # The mismatch carries its own category: a NOT_RUN command is
            # VALIDATION_INCOMPLETE, a SPAWN_FAILED one is RUNNER_FAILED.
            # Neither is an agent result.
            return self._fail(
                run,
                task,
                mismatch.category,
                mismatch.reason,
                report=report,
                patch=patch,
                agent_result=agent_result,
                agent_ms=agent_ms,
                validation_ms=validation_ms,
            )

        result = score(task, report)
        patch_is_empty = patch.is_empty

        if is_task_defect(result, patch_is_empty=patch_is_empty):
            # Evidence is kept: FAILED forbids an outcome but not a report or a
            # score, and without them the defect is unactionable.
            return self._fail(
                run,
                task,
                FailureCategory.TASK_INVALID,
                "an empty patch resolved the task, so it measures nothing",
                report=report,
                score_result=result,
                patch=patch,
                agent_result=agent_result,
                agent_ms=agent_ms,
                validation_ms=validation_ms,
                task_defect=True,
            )

        scored = run.transition_to(RunState.SCORED, self._clock.now_unix_ms())
        record = self._base_record(
            scored,
            task,
            report=report,
            score_result=result,
            # The agent's verdict outranks classification, and the order is
            # load-bearing. A reply that could not be read as an edit writes
            # nothing, so the patch is empty and classify would return
            # NO_PATCH_PRODUCED -- an agent that ran fine and chose to change
            # nothing. Reversing these two silently relabels every malformed
            # reply as well-behaved restraint, and the malformed-output rate,
            # which is the honest measure of whether a model can follow the
            # output contract at all, reads zero forever.
            outcome=agent_result.outcome
            or classify(result, patch_is_empty=patch_is_empty),
            patch=patch,
            agent_result=agent_result,
            agent_ms=agent_ms,
            validation_ms=validation_ms,
        )
        self._repo.finish(record, expected=RunState.VALIDATING)
        return record

    def _fail(
        self,
        run: EvaluationRun,
        task: TaskDefinition,
        kind: FailureCategory,
        detail: str,
        *,
        report: ValidationReport | None = None,
        score_result: Score | None = None,
        patch: Patch | None = None,
        agent_result: AgentResult | None = None,
        agent_ms: int | None = None,
        validation_ms: int | None = None,
        task_defect: bool = False,
    ) -> RunRecord:
        expected = run.state
        failed = run.fail(kind, self._clock.now_unix_ms(), detail)
        record = self._base_record(
            failed,
            task,
            report=report,
            score_result=score_result,
            patch=patch,
            agent_result=agent_result,
            agent_ms=agent_ms,
            validation_ms=validation_ms,
            task_defect=task_defect,
        )
        self._repo.finish(record, expected=expected)
        return record

    def _base_record(
        self,
        run: EvaluationRun,
        task: TaskDefinition,
        *,
        report: ValidationReport | None = None,
        score_result: Score | None = None,
        outcome: AgentOutcome | None = None,
        patch: Patch | None = None,
        agent_result: AgentResult | None = None,
        agent_ms: int | None = None,
        validation_ms: int | None = None,
        task_defect: bool = False,
    ) -> RunRecord:
        invocations = agent_result.invocations if agent_result is not None else ()
        unified = patch.unified_diff if patch is not None else None

        return RunRecord(
            run=run,
            suite=task.suite,
            fixture_sha256=task.fixture_sha256,
            runner_id=self._runner.runner_id,
            runner_version=self._runner.runner_version,
            arch=report.arch if report is not None else UNKNOWN_ARCH,
            pid1=report.pid1 if report is not None else False,
            report=report,
            score=score_result,
            outcome=outcome,
            task_defect=task_defect,
            patch_sha256=(
                hashlib.sha256(unified.encode("utf-8")).hexdigest()
                if unified is not None
                else None
            ),
            patch_bytes=len(unified.encode("utf-8")) if unified is not None else 0,
            patch_is_empty=patch.is_empty if patch is not None else None,
            invocations=invocations,
            total_cost_micros=total_micros(i.cost for i in invocations),
            agent_ms=agent_ms,
            validation_ms=validation_ms,
            # Derived, never passed in. Every record names its workspace from
            # CREATED onwards, including the ones that fail: a run that dies in
            # PREPARING is exactly the run whose directory recovery must delete,
            # and it is also the run that never reached the code that used to
            # supply this. A parameter here would be a second place that knows
            # the layout, free to disagree with WorkspaceFactory.
            workspace_path=str(self._workspaces.path_for(run.run_id)),
        )
