"""Failures adapters raise, and the run outcome each one means.

Typed rather than stringly, because the orchestrator maps each to a specific
`FailureCategory` and the mapping is the thing that keeps an infrastructure
problem out of the agent's score.
"""

from __future__ import annotations

from control_plane.domain.model import ModelInvocation


class ServiceError(Exception):
    """Base for failures raised by an adapter into the orchestrator."""


class RunnerUnavailable(ServiceError):
    """The runner could not be started at all.

    Docker unreachable, image missing, or the container killed by the host.
    Becomes `FailureCategory.INFRASTRUCTURE_FAILED` -- never an agent result.
    """


class RunnerFailed(ServiceError):
    """The runner started but produced no trustworthy result.

    A non-zero runner exit, or a missing, truncated or unparseable result file.
    Becomes `FailureCategory.RUNNER_FAILED`.
    """


class AgentError(ServiceError):
    """The agent adapter raised.

    A network error, an auth failure, an exhausted rate limit. The model's
    answer being poor is not this -- that is a measurement.

    `invocations` are the calls that completed before the failure. A loop that
    fails on its third turn has already been paid for two, and a run recorded
    without them under-reports what the model consumed.
    """

    def __init__(
        self, message: str, *, invocations: tuple[ModelInvocation, ...] = ()
    ) -> None:
        super().__init__(message)
        self.invocations = invocations


class RunAlreadyExists(ServiceError):
    """A run id was recorded twice.

    Raised rather than overwriting. `INSERT OR REPLACE` would silently destroy
    a previous run's record, which for an evaluation harness is exactly the
    data loss the project exists to prevent.
    """


class ConcurrentModification(ServiceError):
    """A run's state changed underneath a write.

    The scenario that matters: a recovery scan at startup decides a run is
    orphaned and marks it interrupted, while the original process is in fact
    alive and goes on to write a result. Without the compare-and-swap the run
    would silently become SCORED, and a measurement from a process that was
    declared dead would enter the benchmark.
    """
