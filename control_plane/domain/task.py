"""Benchmark task definitions.

A task names a fixture repository, describes the change wanted, and lists the
commands that decide whether the agent succeeded. Roles are deliberately the
same two SWE-bench uses, because D7 makes the second benchmark suite SWE-bench
Lite instances: mapping those in should be an adapter, not a redesign.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from control_plane.domain.errors import InvalidTaskDefinition
from control_plane.domain.ids import TaskId, validate_identifier

_SHA256_HEX = re.compile(r"\A[0-9a-f]{64}\Z")


class Suite(StrEnum):
    """Which benchmark a task belongs to.

    Reported separately and never averaged together (D7): a synthetic fixture
    and a real repository are not comparable measurements.
    """

    SYNTHETIC = "synthetic"
    OSS = "oss"


class CommandRole(StrEnum):
    """What a validation command is evidence of."""

    FAIL_TO_PASS = "fail_to_pass"
    """Must fail before the patch and pass after. This is the task itself."""

    PASS_TO_PASS = "pass_to_pass"
    """Must pass both before and after. This is the regression check."""


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One validation command.

    `index` is the position in the newline-delimited command file handed to the
    runner, and is how a result is matched back to its role. The runner does
    not invent labels; the control plane wrote the file, so it already knows
    what each line means (D3).
    """

    index: int
    command: str
    role: CommandRole

    def __post_init__(self) -> None:
        if self.index < 0:
            raise InvalidTaskDefinition(f"command index must be >= 0, got {self.index}")
        if not self.command.strip():
            raise InvalidTaskDefinition(f"command {self.index} is empty")
        # The command file is one command per line. A newline inside a command
        # would silently split it into two, and the second half would run as a
        # command of its own -- with the wrong index, and therefore scored
        # against the wrong role. Multi-line logic belongs in a script inside
        # the fixture, invoked as a single command.
        if "\n" in self.command or "\r" in self.command:
            raise InvalidTaskDefinition(
                f"command {self.index} contains a line break; put multi-line "
                f"logic in a script inside the fixture and invoke that instead"
            )


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    """An immutable, content-pinned benchmark task."""

    task_id: TaskId
    suite: Suite
    description: str
    fixture_id: str
    fixture_sha256: str
    commands: tuple[CommandSpec, ...]
    per_command_timeout_ms: int
    total_timeout_ms: int

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise InvalidTaskDefinition(f"{self.task_id} has an empty description")

        # The fixture id becomes a directory name, so it carries the same
        # constraints as any other identifier.
        validate_identifier(self.fixture_id, "fixture_id")

        if not _SHA256_HEX.fullmatch(self.fixture_sha256):
            raise InvalidTaskDefinition(
                f"{self.task_id} fixture_sha256 must be 64 lowercase hex "
                f"characters, got {self.fixture_sha256!r}"
            )

        self._validate_commands()
        self._validate_timeouts()

    def _validate_commands(self) -> None:
        if not self.commands:
            raise InvalidTaskDefinition(f"{self.task_id} has no commands")

        # Indices must be exactly 0..n-1 in order, because they are line
        # numbers in a file the control plane writes. A gap or a duplicate
        # means a result would be matched to the wrong command's role, and the
        # run would be scored against the wrong evidence.
        expected = list(range(len(self.commands)))
        actual = [command.index for command in self.commands]
        if actual != expected:
            raise InvalidTaskDefinition(
                f"{self.task_id} command indices must be {expected}, got {actual}"
            )

        # A task with nothing that must newly pass cannot distinguish a working
        # agent from one that changed nothing, so it measures nothing. Such a
        # task entering the benchmark would quietly inflate the success rate.
        if not any(c.role is CommandRole.FAIL_TO_PASS for c in self.commands):
            raise InvalidTaskDefinition(
                f"{self.task_id} has no {CommandRole.FAIL_TO_PASS} command, so a "
                f"no-op patch would score as success"
            )

    def _validate_timeouts(self) -> None:
        if self.per_command_timeout_ms <= 0:
            raise InvalidTaskDefinition(
                f"{self.task_id} per_command_timeout_ms must be > 0, "
                f"got {self.per_command_timeout_ms}"
            )
        if self.total_timeout_ms <= 0:
            raise InvalidTaskDefinition(
                f"{self.task_id} total_timeout_ms must be > 0, "
                f"got {self.total_timeout_ms}"
            )
        # A total budget below a single command's budget is unsatisfiable: the
        # run would be cut off partway through command one every time, and the
        # failure would look like a slow test rather than a misconfigured task.
        if self.total_timeout_ms < self.per_command_timeout_ms:
            raise InvalidTaskDefinition(
                f"{self.task_id} total_timeout_ms ({self.total_timeout_ms}) is "
                f"below per_command_timeout_ms ({self.per_command_timeout_ms})"
            )

    def commands_with_role(self, role: CommandRole) -> tuple[CommandSpec, ...]:
        return tuple(c for c in self.commands if c.role is role)

    def command_file_lines(self) -> tuple[str, ...]:
        """The command file contents, in index order.

        Lives here rather than in an adapter because the ordering *is* the
        index contract: whatever writes the file must not reorder it.
        """
        return tuple(c.command for c in self.commands)

    @staticmethod
    def index_commands(
        specs: Iterable[tuple[str, CommandRole]],
    ) -> tuple[CommandSpec, ...]:
        """Build correctly-indexed commands from (command, role) pairs.

        Callers that assign indices by hand get them wrong; this is the only
        place indices are chosen.
        """
        return tuple(
            CommandSpec(index=i, command=command, role=role)
            for i, (command, role) in enumerate(specs)
        )
