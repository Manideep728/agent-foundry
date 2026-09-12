"""The evaluation run lifecycle, and what to do with a run found mid-flight.

A state earns its place only if a crash in it needs a *distinct* cleanup, or if
an operator can observe it and act differently. Everything else is a reason,
not a state -- reasons live in `FailureCategory`. Without that test the enum
grows to thirty members and none of them mean anything.

    CREATED -> PREPARING -> AGENT_RUNNING -> VALIDATING -> SCORED

with FAILED and ABORTED reachable from every non-terminal state, because both
can happen at any point.

Each non-terminal state leaves something different behind:

    CREATED        nothing
    PREPARING      a partial workspace tree; no money spent yet
    AGENT_RUNNING  money possibly spent, amount unknown
    VALIDATING     a live container holding CPU and memory

There is deliberately no state between AGENT_RUNNING and VALIDATING. One would
let a crashed run resume validation without paying the model again -- but
resume is not a feature yet, and adding a state later is backward-compatible
while removing one is not.

The distinction carrying the most weight is SCORED versus FAILED; see
`outcomes.py`. SCORED means a measurement exists, even if the agent's patch
failed every test. FAILED means the harness broke and no trustworthy
measurement exists.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from types import MappingProxyType

from control_plane.domain.errors import InvalidTransition
from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.outcomes import FailureCategory


@unique
class RunState(StrEnum):
    CREATED = "created"
    """Recorded, nothing done. A crash here leaves nothing to clean up."""

    PREPARING = "preparing"
    """Copying the fixture and verifying its hash. A crash here leaves a
    partial workspace directory, and nothing has been paid for."""

    AGENT_RUNNING = "agent_running"
    """Waiting on the model. Distinct from PREPARING because a crash here may
    already have cost real money, and the amount is unknown rather than zero."""

    VALIDATING = "validating"
    """The runner is executing validation commands in a container. A crash here
    may leave that container alive."""

    SCORED = "scored"
    """Terminal. A measurement exists, whatever it says about the agent."""

    FAILED = "failed"
    """Terminal. The harness broke; no measurement exists."""

    ABORTED = "aborted"
    """Terminal. Cancelled by an operator. Kept separate from FAILED because an
    abort is expected behaviour, not a defect -- merging them would pollute the
    harness-reliability number with every cancelled run."""


TERMINAL_STATES = frozenset({RunState.SCORED, RunState.FAILED, RunState.ABORTED})

# Spelled out per state rather than special-cased in the checking code, so the
# table alone is the entire specification and a reader need not also read the
# function that consults it.
_ANY_EXIT = (RunState.FAILED, RunState.ABORTED)

TRANSITIONS: Mapping[RunState, frozenset[RunState]] = MappingProxyType(
    {
        RunState.CREATED: frozenset({RunState.PREPARING, *_ANY_EXIT}),
        RunState.PREPARING: frozenset({RunState.AGENT_RUNNING, *_ANY_EXIT}),
        RunState.AGENT_RUNNING: frozenset({RunState.VALIDATING, *_ANY_EXIT}),
        RunState.VALIDATING: frozenset({RunState.SCORED, *_ANY_EXIT}),
        RunState.SCORED: frozenset(),
        RunState.FAILED: frozenset(),
        RunState.ABORTED: frozenset(),
    }
)


@unique
class RecoveryAction(StrEnum):
    """What to clean up for a run found mid-flight at startup."""

    NOTHING = "nothing"
    DELETE_WORKSPACE = "delete_workspace"
    REMOVE_CONTAINER = "remove_container"


_RECOVERY: Mapping[RunState, RecoveryAction] = MappingProxyType(
    {
        RunState.CREATED: RecoveryAction.NOTHING,
        RunState.PREPARING: RecoveryAction.DELETE_WORKSPACE,
        # Nothing to clean up locally, and nothing to undo remotely: the tokens
        # are already spent. The usage is unknown, which is not the same as
        # zero, and the run records it as such.
        RunState.AGENT_RUNNING: RecoveryAction.NOTHING,
        # The one that matters on a 1.9 GiB Docker VM. A container left running
        # holds memory and CPU, and the symptom is the *next* run failing for a
        # reason that has nothing to do with it.
        RunState.VALIDATING: RecoveryAction.REMOVE_CONTAINER,
        RunState.SCORED: RecoveryAction.NOTHING,
        RunState.FAILED: RecoveryAction.NOTHING,
        RunState.ABORTED: RecoveryAction.NOTHING,
    }
)


def recovery_for(state: RunState) -> RecoveryAction:
    """What to clean up for a run found in `state` when the control plane starts.

    Total over `RunState` by construction, and a test iterates the enum to keep
    it that way: a state added without a recovery entry would otherwise leak
    whatever that state leaves behind.

    Recovery never retries. It cleans up and marks the run
    `FailureCategory.INTERRUPTED`; re-running is an explicit operator act.
    Automatic retry hides a crash loop, and from AGENT_RUNNING it can spend the
    money twice.
    """
    return _RECOVERY[state]


@dataclass(frozen=True, slots=True)
class HarnessFailure:
    """Why a run has no measurement, and how far it got before that was true.

    `failed_from` is not bookkeeping. Transitioning to FAILED otherwise
    destroys the single most useful fact for both recovery and reporting --
    "most harness failures happen during validation" is a claim this project
    should be able to make or refute from its own records.
    """

    kind: FailureCategory
    failed_from: RunState
    detail: str = ""

    def __post_init__(self) -> None:
        if self.failed_from in TERMINAL_STATES:
            raise InvalidTransition(
                f"a run cannot fail from {self.failed_from}, which is terminal"
            )


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    """One evaluation, as a value.

    Frozen, and transitions return a new instance. A state machine that mutates
    in place can be left half-updated when a check raises partway through --
    state advanced, timestamp not -- and the result is a run that is wrong in a
    way no single assertion catches. Being frozen also means `__post_init__`
    runs on *every* construction path, including rehydration from storage,
    which is the untyped path where a bad row would otherwise slip through.
    """

    run_id: RunId
    task_id: TaskId
    state: RunState
    created_at_unix_ms: int
    updated_at_unix_ms: int
    failure: HarnessFailure | None = None

    def __post_init__(self) -> None:
        # A failed run with no recorded reason is unactionable: nobody can tell
        # afterwards whether to retry it or to go and fix the harness.
        if self.state is RunState.FAILED and self.failure is None:
            raise InvalidTransition(f"{self.run_id} is FAILED without a reason")
        if self.state is not RunState.FAILED and self.failure is not None:
            raise InvalidTransition(
                f"{self.run_id} is {self.state} but carries a failure reason"
            )
        if self.updated_at_unix_ms < self.created_at_unix_ms:
            raise InvalidTransition(f"{self.run_id} was updated before it was created")

    @classmethod
    def create(cls, run_id: RunId, task_id: TaskId, at_unix_ms: int) -> EvaluationRun:
        return cls(
            run_id=run_id,
            task_id=task_id,
            state=RunState.CREATED,
            created_at_unix_ms=at_unix_ms,
            updated_at_unix_ms=at_unix_ms,
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def transition_to(
        self,
        state: RunState,
        at_unix_ms: int,
        failure: HarnessFailure | None = None,
    ) -> EvaluationRun:
        """Return a new run in `state`, or raise `InvalidTransition`."""
        allowed = TRANSITIONS[self.state]
        if state not in allowed:
            if self.is_terminal:
                raise InvalidTransition(
                    f"{self.run_id} is already {self.state}, which is terminal; "
                    f"cannot move to {state}"
                )
            raise InvalidTransition(
                f"{self.run_id} cannot move from {self.state} to {state}; "
                f"allowed: {sorted(allowed)}"
            )
        # Timestamps come from an injected clock. One that went backwards would
        # otherwise produce a negative duration downstream, which is the shape
        # of bug D12 exists to prevent.
        if at_unix_ms < self.updated_at_unix_ms:
            raise InvalidTransition(
                f"{self.run_id} cannot move to {state} at {at_unix_ms}, before "
                f"its last update at {self.updated_at_unix_ms}"
            )
        return dataclasses.replace(
            self, state=state, updated_at_unix_ms=at_unix_ms, failure=failure
        )

    def fail(
        self, kind: FailureCategory, at_unix_ms: int, detail: str = ""
    ) -> EvaluationRun:
        """Fail from wherever the run currently is, recording where that was."""
        return self.transition_to(
            RunState.FAILED,
            at_unix_ms,
            failure=HarnessFailure(kind=kind, failed_from=self.state, detail=detail),
        )

    def abort(self, at_unix_ms: int) -> EvaluationRun:
        return self.transition_to(RunState.ABORTED, at_unix_ms)

    def recovery_action(self) -> RecoveryAction:
        return recovery_for(self.state)
