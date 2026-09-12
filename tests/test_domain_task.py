"""Task definition invariants.

Each of these guards a way a malformed task would produce a plausible but
wrong benchmark number rather than an obvious error.
"""

from __future__ import annotations

import pytest

from control_plane.domain.errors import DomainError, InvalidTaskDefinition
from control_plane.domain.ids import TaskId
from control_plane.domain.task import (
    CommandRole,
    CommandSpec,
    Suite,
    TaskDefinition,
)

VALID_SHA = "a" * 64


def make_task(**overrides: object) -> TaskDefinition:
    """A valid task, with named fields overridden per test."""
    fields: dict[str, object] = {
        "task_id": TaskId("csv-parser-001"),
        "suite": Suite.SYNTHETIC,
        "description": "Fix the quoted-field parser.",
        "fixture_id": "csv-parser",
        "fixture_sha256": VALID_SHA,
        "commands": TaskDefinition.index_commands(
            [
                ("pytest -q tests/test_parser.py", CommandRole.FAIL_TO_PASS),
                ("pytest -q tests/test_regress.py", CommandRole.PASS_TO_PASS),
            ]
        ),
        "per_command_timeout_ms": 120_000,
        "total_timeout_ms": 600_000,
    }
    fields.update(overrides)
    return TaskDefinition(**fields)  # type: ignore[arg-type]


def test_a_valid_task_is_accepted() -> None:
    task = make_task()
    assert len(task.commands) == 2
    assert task.commands_with_role(CommandRole.FAIL_TO_PASS)[0].index == 0
    assert task.command_file_lines() == (
        "pytest -q tests/test_parser.py",
        "pytest -q tests/test_regress.py",
    )


def test_task_without_a_fail_to_pass_command_is_rejected() -> None:
    # The important one. Such a task cannot tell a working agent apart from one
    # that changed nothing, so every agent "succeeds" on it and the benchmark's
    # success rate goes up for free.
    with pytest.raises(InvalidTaskDefinition, match=r"fail_to_pass"):
        make_task(
            commands=TaskDefinition.index_commands(
                [("pytest -q tests/", CommandRole.PASS_TO_PASS)]
            )
        )


def test_commands_must_be_indexed_contiguously_from_zero() -> None:
    # Indices are line numbers in the command file. A gap means a result is
    # matched back to the wrong command, and therefore scored against the
    # wrong role -- silently.
    with pytest.raises(InvalidTaskDefinition, match="indices"):
        make_task(
            commands=(
                CommandSpec(0, "pytest -q a", CommandRole.FAIL_TO_PASS),
                CommandSpec(2, "pytest -q b", CommandRole.PASS_TO_PASS),
            )
        )


def test_duplicate_command_indices_are_rejected() -> None:
    with pytest.raises(InvalidTaskDefinition, match="indices"):
        make_task(
            commands=(
                CommandSpec(0, "pytest -q a", CommandRole.FAIL_TO_PASS),
                CommandSpec(0, "pytest -q b", CommandRole.PASS_TO_PASS),
            )
        )


def test_a_task_with_no_commands_is_rejected() -> None:
    with pytest.raises(InvalidTaskDefinition, match="no commands"):
        make_task(commands=())


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q\nrm -rf /",  # the injection shape
        "pytest -q\r\npytest -q other",  # CRLF, from a Windows-authored file
        "trailing\n",
    ],
)
def test_a_command_containing_a_line_break_is_rejected(command: str) -> None:
    # The command file is one command per line. A newline here silently becomes
    # two commands, the second running with an index that belongs to something
    # else. CRLF is not hypothetical: the other dev machine is Windows.
    with pytest.raises(InvalidTaskDefinition, match="line break"):
        CommandSpec(0, command, CommandRole.FAIL_TO_PASS)


@pytest.mark.parametrize("command", ["", "   ", "\t"])
def test_an_empty_command_is_rejected(command: str) -> None:
    with pytest.raises(InvalidTaskDefinition, match="empty"):
        CommandSpec(0, command, CommandRole.FAIL_TO_PASS)


@pytest.mark.parametrize(
    "sha",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,  # uppercase: hashlib emits lowercase, so this is a typo
        "g" * 64,  # not hex
        "  " + "a" * 62,
    ],
)
def test_a_malformed_fixture_hash_is_rejected(sha: str) -> None:
    # The hash is the reproducibility invariant -- it is re-checked when the
    # task is loaded, and a mismatch refuses the run. A malformed hash could
    # never match, so the task would be permanently unrunnable for a reason
    # nobody would look for.
    with pytest.raises(InvalidTaskDefinition, match="fixture_sha256"):
        make_task(fixture_sha256=sha)


@pytest.mark.parametrize("fixture_id", ["../etc", "has space", "", "a/b"])
def test_a_dangerous_fixture_id_is_rejected(fixture_id: str) -> None:
    # It becomes a directory name, so it carries identifier constraints.
    # InvalidIdentifier, not InvalidTaskDefinition -- both are DomainError, and
    # the caller catches the base class.
    with pytest.raises(DomainError):
        make_task(fixture_id=fixture_id)


def test_an_empty_description_is_rejected() -> None:
    with pytest.raises(InvalidTaskDefinition, match="description"):
        make_task(description="   ")


@pytest.mark.parametrize("timeout", [0, -1])
def test_non_positive_timeouts_are_rejected(timeout: int) -> None:
    with pytest.raises(InvalidTaskDefinition, match="per_command_timeout_ms"):
        make_task(per_command_timeout_ms=timeout)
    with pytest.raises(InvalidTaskDefinition, match="total_timeout_ms"):
        make_task(total_timeout_ms=timeout)


def test_total_timeout_below_per_command_timeout_is_rejected() -> None:
    # Unsatisfiable: the run is cut off partway through the first command every
    # time, and it looks like a slow test rather than a misconfigured task.
    with pytest.raises(InvalidTaskDefinition, match="below per_command"):
        make_task(per_command_timeout_ms=600_000, total_timeout_ms=120_000)


def test_index_commands_assigns_indices_in_order() -> None:
    commands = TaskDefinition.index_commands(
        [
            ("a", CommandRole.FAIL_TO_PASS),
            ("b", CommandRole.PASS_TO_PASS),
            ("c", CommandRole.PASS_TO_PASS),
        ]
    )
    assert [c.index for c in commands] == [0, 1, 2]
    assert [c.command for c in commands] == ["a", "b", "c"]


def test_roles_and_suites_serialise_as_their_string_values() -> None:
    # StrEnum, so a task file and a stored record hold "fail_to_pass" rather
    # than "CommandRole.FAIL_TO_PASS". Asserted through .value and str()
    # rather than `== "fail_to_pass"`, which mypy rejects as a non-overlapping
    # literal comparison even though StrEnum members are str instances.
    assert isinstance(CommandRole.FAIL_TO_PASS, str)
    assert CommandRole.FAIL_TO_PASS.value == "fail_to_pass"
    assert str(Suite.OSS) == "oss"
    assert CommandRole("pass_to_pass") is CommandRole.PASS_TO_PASS
