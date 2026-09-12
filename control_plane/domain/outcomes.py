"""Two separate vocabularies: why the harness broke, and what it measured.

Keeping these apart is the most important modelling decision in the project.

A run where the agent wrote a patch that fails every test is a **successful
evaluation** -- the harness did its job and produced a measurement. A run where
Docker was unreachable, or the container was OOM-killed, produced no
measurement at all.

Collapsing both into one enum makes the two expressible as the same value, and
the failure mode is expensive and quiet: a container hitting the 1.9 GiB memory
limit on the development machine would be recorded as "the agent failed",
depressing every number in the benchmark while the report still looks
plausible. Nothing crashes, and the numbers are simply wrong.

This is the same principle as the runner's exit-code rule -- the exit code
describes the runner, not the commands -- one level up.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class FailureCategory(StrEnum):
    """Why a run reached FAILED. The harness broke; there is no measurement.

    A run carrying one of these must never contribute to a success rate, a
    latency figure or a cost total.
    """

    WORKSPACE_SETUP_FAILED = "workspace_setup_failed"
    """The fixture could not be copied, or the workspace could not be created."""

    FIXTURE_HASH_MISMATCH = "fixture_hash_mismatch"
    """The fixture on disk is not the one the task pins. Running anyway would
    produce a result that cannot be reproduced."""

    AGENT_ERROR = "agent_error"
    """The model API or the agent adapter raised -- a network error, an auth
    failure, a rate limit. Not the model's answer being poor; that is measured."""

    RUNNER_FAILED = "runner_failed"
    """The supervisor itself exited non-zero, or wrote no parseable result.
    Its exit code describes the runner, so this is never a command failure."""

    INFRASTRUCTURE_FAILED = "infrastructure_failed"
    """Docker was unreachable, the image was missing, or the container was
    killed by the host -- exit 137 being the case that matters most here."""

    VALIDATION_INCOMPLETE = "validation_incomplete"
    """Validation ran but did not finish: the runner was aborted, or a command
    was never run. A command that never ran cannot be said to have failed --
    the patch was never given the chance."""

    TASK_INVALID = "task_invalid"
    """The task itself is broken. Includes the case where an empty patch scores
    as resolved, which means the task cannot measure anything."""

    INTERRUPTED = "interrupted"
    """The control plane died mid-run. Found by the recovery scan at startup,
    never set by a running process."""


@unique
class AgentOutcome(StrEnum):
    """What a SCORED run showed. Every value here is a real measurement."""

    CLEAN_PATCH = "clean_patch"
    """Every fail-to-pass command passed and nothing regressed."""

    PATCH_WITH_REGRESSIONS = "patch_with_regressions"
    """The target tests pass, but a previously-passing test now fails. Not a
    success: the agent traded one failure for another."""

    TESTS_STILL_FAILING = "tests_still_failing"
    """A patch was produced and validation ran; the target tests still fail."""

    VALIDATION_TIMEOUT = "validation_timeout"
    """A validation command exceeded its budget. Distinguished from plain
    failure because a timeout may mean the patch hangs rather than that it is
    wrong, and because timeout rate is reported separately."""

    NO_PATCH_PRODUCED = "no_patch_produced"
    """The agent ran to completion and changed nothing. An agent failure, so it
    is measured -- not a harness failure."""

    MALFORMED_OUTPUT = "malformed_output"
    """The model's response could not be interpreted as an edit. The agent
    failed, which is a result worth counting, so it is scored rather than
    treated as an error."""
