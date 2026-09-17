"""Interfaces the orchestrator depends on.

These live here rather than in the domain because the domain never calls them
-- only the use case does, and an interface belongs with its caller. They
return domain types, which is ordinary: the dependency still points inward.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from control_plane.domain.ids import RunId
from control_plane.domain.model import ModelInvocation
from control_plane.domain.outcomes import AgentOutcome
from control_plane.domain.task import TaskDefinition
from control_plane.domain.validation import ValidationReport


@dataclass(frozen=True, slots=True)
class ValidationRequest:
    """Everything a runner needs, and nothing it should know.

    `commands` is a tuple of strings rather than the task, deliberately. The
    runner does not invent labels: the control plane wrote the command file and
    already knows what each line is evidence of. Passing roles in would create
    a second place that knows the index-to-role mapping, and the first bug
    would be the two disagreeing.
    """

    run_id: RunId
    workdir: Path
    output_dir: Path
    log_dir: Path
    commands: tuple[str, ...]
    per_command_timeout_ms: int
    total_timeout_ms: int
    stop_on_failure: bool = False


class ValidationRunner(Protocol):
    """Executes validation commands and reports what happened."""

    @property
    def runner_id(self) -> str: ...

    @property
    def runner_version(self) -> str: ...

    @property
    def isolated(self) -> bool:
        """Whether this runner provides an isolation boundary.

        False for the local runner. Reports and benchmark tables filter on it,
        so a development run cannot be mistaken for a measured one.
        """
        ...

    @property
    def image_digest(self) -> str | None:
        """The content address of the image the most recent `run()` used.

        None for a runner with no image, before `run()` has been called, and
        when the image could not be inspected. A benchmark table can require
        `image_digest IS NOT NULL`, a stronger provenance filter than `pid1`
        because it names the exact bytes rather than the arrangement.

        A property rather than a second value returned from `run()`. The
        alternative threads an outcome object through `_base_record`, `_fail`
        and `_score`, and the paths that would forget to pass it are the
        failure paths -- the least exercised, and the ones whose provenance is
        most often asked for. Read where `runner_id` and `runner_version` are
        already read, it cannot be forgotten by a caller.

        Re-resolved at the top of every `run()`, so a runner reused across
        tasks cannot report a previous task's image.
        """
        ...

    def run(self, request: ValidationRequest) -> ValidationReport: ...


@dataclass(frozen=True, slots=True)
class AgentResult:
    """What one agent run consumed, and what it says about its own output.

    `invocations` is a tuple rather than a single call because an agent that
    edits over several turns makes several calls, and each one is a measurement
    in its own right. A single-shot agent returns a tuple of one.

    `outcome` is set only when the agent knows something the validation cannot
    tell. The case it exists for is `MALFORMED_OUTPUT`: a reply that could not
    be read as an edit writes nothing, so the patch is empty and scoring would
    otherwise call it `NO_PATCH_PRODUCED` -- an agent that deliberately changed
    nothing. Those are different measurements and must not be confused. An
    agent that has nothing to add leaves this None and lets scoring decide.

    It is not an error channel. `AgentError` means the adapter itself failed --
    a network fault, a bad key -- and becomes a harness failure. A model
    answering badly is a result, and results are scored.
    """

    invocations: tuple[ModelInvocation, ...]
    outcome: AgentOutcome | None = None

    raw_reply: str | None = None
    """What the model actually sent, when that is worth keeping.

    Set on the path where the reply could not be read as an edit. Until this
    existed the text was a local variable in the agent, discarded by the
    `except` that classified the run -- so a `malformed_output` run recorded
    that the model had answered badly and kept no way of seeing how. A rate
    nobody can inspect cannot be acted on.

    It travels back through the result rather than being written by the agent
    itself, because the agent is handed only the repository copy and everything
    written there becomes part of the patch.
    """

    outcome_detail: str | None = None
    """One line saying why, in the agent's own words.

    The `MalformedReply` message -- "the block for x.py was never closed", "the
    reply contained no tagged file block" -- which was dropped along with the
    text. It is the difference between a model that cannot follow the format
    and one that was simply cut off, and those call for different fixes.
    """


class AgentPort(Protocol):
    """Edits a workspace in place and reports what it consumed.

    It returns usage, not a patch. Capturing the patch is a separate concern,
    so an agent implementation cannot misreport what it changed.
    """

    @property
    def provider_id(self) -> str: ...

    def edit(self, workspace: Path, task: TaskDefinition) -> AgentResult: ...
