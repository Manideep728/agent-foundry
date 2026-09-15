"""Dealing with runs the control plane died in the middle of.

Recovery never retries. It cleans up what the state says was left behind, marks
the run INTERRUPTED, and stops. Automatic retry would hide a crash loop, and
from AGENT_RUNNING it could spend the money a second time -- the first
attempt's token usage is unknown, which is not the same as zero.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from control_plane.adapters.docker_runner import RUNNER_ID as DOCKER_RUNNER_ID
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.workspace import Workspace, WorkspaceFactory
from control_plane.domain.ids import RunId
from control_plane.domain.outcomes import FailureCategory
from control_plane.domain.ports import Clock
from control_plane.domain.states import RecoveryAction, recovery_for
from control_plane.services.errors import ConcurrentModification, ServiceError

ContainerRemover = Callable[[RunId], None]


@dataclass(frozen=True, slots=True)
class Recovered:
    run_id: RunId
    action: RecoveryAction
    # Set when the cleanup the action calls for could not be done. The run is
    # still marked interrupted: leaving it unfinished would retry the same
    # failing cleanup on every start and never record what happened.
    cleanup_error: str | None = None


def recover_unfinished(
    repository: SqliteRunRepository,
    workspaces: WorkspaceFactory,
    clock: Clock,
    remove_container: ContainerRemover,
) -> tuple[Recovered, ...]:
    """Mark every unfinished run interrupted, cleaning up as its state requires.

    Idempotent: a second pass finds nothing, because the runs it touched are
    now terminal. The compare-and-swap is what makes it safe against a process
    that is in fact still alive -- if the original finishes first, this write
    matches no rows and is skipped rather than overwriting a real result.
    """
    recovered: list[Recovered] = []

    for record in repository.list_unfinished():
        run = record.run
        action = recovery_for(run.state)

        # The state is claimed before anything is cleaned up. Cleaning first
        # would remove the container or workspace of a process that is still
        # alive and about to finish, spoiling a result the compare-and-swap
        # below would otherwise have let stand.
        try:
            repository.advance(
                run.fail(FailureCategory.INTERRUPTED, clock.now_unix_ms()),
                expected=run.state,
            )
        except ConcurrentModification:
            # The original process finished after all. Its result stands.
            continue

        cleanup_error: str | None = None
        if action is RecoveryAction.DELETE_WORKSPACE and record.workspace_path:
            workspaces.discard_repo(
                Workspace(run_id=run.run_id, root=Path(record.workspace_path))
            )
        # Only a Docker run can have left a container. A local run in
        # VALIDATING left processes, which died with the control plane.
        if (
            action is RecoveryAction.REMOVE_CONTAINER
            and record.runner_id == DOCKER_RUNNER_ID
        ):
            try:
                remove_container(run.run_id)
            except ServiceError as error:
                cleanup_error = str(error)

        recovered.append(
            Recovered(run_id=run.run_id, action=action, cleanup_error=cleanup_error)
        )

    return tuple(recovered)
