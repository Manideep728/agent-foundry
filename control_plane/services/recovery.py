"""Dealing with runs the control plane died in the middle of.

Recovery never retries. It cleans up what the state says was left behind, marks
the run INTERRUPTED, and stops. Automatic retry would hide a crash loop, and
from AGENT_RUNNING it could spend the money a second time -- the first
attempt's token usage is unknown, which is not the same as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.workspace import Workspace, WorkspaceFactory
from control_plane.domain.ids import RunId
from control_plane.domain.outcomes import FailureCategory
from control_plane.domain.ports import Clock
from control_plane.domain.states import RecoveryAction, recovery_for
from control_plane.services.errors import ConcurrentModification


@dataclass(frozen=True, slots=True)
class Recovered:
    run_id: RunId
    action: RecoveryAction


def recover_unfinished(
    repository: SqliteRunRepository,
    workspaces: WorkspaceFactory,
    clock: Clock,
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

        if action is RecoveryAction.DELETE_WORKSPACE and record.workspace_path:
            workspaces.discard_repo(
                Workspace(run_id=run.run_id, root=Path(record.workspace_path))
            )
        # REMOVE_CONTAINER has no implementation yet: the local runner starts
        # no containers. It becomes real with the Docker runner, and the
        # dispatch is here so that arrives as a fill-in rather than a redesign.

        try:
            repository.advance(
                run.fail(FailureCategory.INTERRUPTED, clock.now_unix_ms()),
                expected=run.state,
            )
        except ConcurrentModification:
            # The original process finished after all. Its result stands.
            continue

        recovered.append(Recovered(run_id=run.run_id, action=action))

    return tuple(recovered)
