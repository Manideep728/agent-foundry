"""Failures adapters raise, and the run outcome each one means.

Typed rather than stringly, because the orchestrator maps each to a specific
`FailureCategory` and the mapping is the thing that keeps an infrastructure
problem out of the agent's score.
"""

from __future__ import annotations


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
    """
