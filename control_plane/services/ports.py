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

    def run(self, request: ValidationRequest) -> ValidationReport: ...


class AgentPort(Protocol):
    """Edits a workspace in place and reports what it consumed.

    It returns usage, not a patch. Capturing the patch is a separate concern,
    so an agent implementation cannot misreport what it changed.
    """

    @property
    def provider_id(self) -> str: ...

    def edit(self, workspace: Path, task: TaskDefinition) -> ModelInvocation: ...
