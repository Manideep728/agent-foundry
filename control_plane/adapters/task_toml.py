"""Loading task definitions from TOML.

Every rejection here exists because the alternative is a task that loads with
a wrong value and produces a plausible number. A typo'd key that silently
falls back to a default is worse than a crash: the run completes, the report
looks fine, and the budget it ran under was not the one anyone wrote down.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from control_plane.domain.errors import DomainError, InvalidTaskDefinition
from control_plane.domain.ids import TaskId
from control_plane.domain.task import CommandRole, Suite, TaskDefinition

SCHEMA_VERSION = 1

_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "task", "fixture", "timeouts", "commands"}
)
_TASK_KEYS = frozenset({"id", "suite", "description"})
_FIXTURE_KEYS = frozenset({"id", "sha256", "hidden_paths"})
_TIMEOUT_KEYS = frozenset({"per_command_ms", "total_ms"})
_COMMAND_KEYS = frozenset({"role", "command"})


def _string_list(where: str, table: dict[str, Any], key: str) -> tuple[str, ...]:
    """An optional list of strings. Absent is empty; present and wrong is an error.

    A bare string is refused rather than wrapped. TOML makes `hidden_paths =
    "tests/a.py"` easy to write, and silently treating it as a one-element list
    would teach a habit that breaks the day someone adds a second path.
    """
    if key not in table:
        return ()
    value = table[key]
    if not isinstance(value, list):
        raise InvalidTaskDefinition(
            f"{where}: {key} must be a list of strings, got {type(value).__name__}"
        )
    for entry in value:
        if not isinstance(entry, str):
            raise InvalidTaskDefinition(
                f"{where}: {key} must contain only strings, got {type(entry).__name__}"
            )
    return tuple(value)


def _reject_unknown(where: str, table: dict[str, Any], allowed: frozenset[str]) -> None:
    """Unknown keys are an error, never ignored.

    The highest-value rule in this loader for the lowest cost. `pre_command_ms`
    instead of `per_command_ms` would otherwise fall back to a default, and the
    task would run under a budget nobody chose.
    """
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise InvalidTaskDefinition(
            f"{where}: unknown key(s) {unknown}; allowed are {sorted(allowed)}"
        )


def _table(where: str, parent: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in parent:
        raise InvalidTaskDefinition(f"{where}: missing [{key}] table")
    value = parent[key]
    if not isinstance(value, dict):
        raise InvalidTaskDefinition(f"{where}: [{key}] must be a table")
    return value


def _string(where: str, table: dict[str, Any], key: str) -> str:
    if key not in table:
        raise InvalidTaskDefinition(f"{where}: missing key {key!r}")
    value = table[key]
    if not isinstance(value, str):
        raise InvalidTaskDefinition(
            f"{where}: {key} must be a string, got {type(value).__name__}"
        )
    return value


def _integer(where: str, table: dict[str, Any], key: str) -> int:
    if key not in table:
        raise InvalidTaskDefinition(f"{where}: missing key {key!r}")
    value = table[key]
    # `type(...) is int` rather than isinstance, because bool is a subclass of
    # int in Python: `per_command_ms = true` would otherwise load as 1 ms.
    if type(value) is not int:
        if isinstance(value, float):
            raise InvalidTaskDefinition(
                f"{where}: {key} must be an integer number of milliseconds, got "
                f"the float {value!r}; no measurement in this system is a float"
            )
        raise InvalidTaskDefinition(
            f"{where}: {key} must be an integer, got {type(value).__name__}"
        )
    return value


def load_task(path: Path) -> TaskDefinition:
    """Read one task file, or raise `InvalidTaskDefinition` naming the problem."""
    where = str(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InvalidTaskDefinition(f"{where}: no such task file") from error
    except tomllib.TOMLDecodeError as error:
        raise InvalidTaskDefinition(f"{where}: not valid TOML: {error}") from error
    except UnicodeDecodeError as error:
        raise InvalidTaskDefinition(f"{where}: not valid UTF-8: {error}") from error

    _reject_unknown(where, raw, _TOP_LEVEL_KEYS)

    version = _integer(where, raw, "schema_version")
    if version != SCHEMA_VERSION:
        # Refused rather than best-effort parsed, for the same reason
        # ValidationReport refuses one: a newer file may have changed what a
        # field means, and guessing yields a plausible wrong task.
        raise InvalidTaskDefinition(
            f"{where}: unsupported schema_version {version}; this build "
            f"understands {SCHEMA_VERSION}"
        )

    task = _table(where, raw, "task")
    fixture = _table(where, raw, "fixture")
    timeouts = _table(where, raw, "timeouts")
    _reject_unknown(f"{where} [task]", task, _TASK_KEYS)
    _reject_unknown(f"{where} [fixture]", fixture, _FIXTURE_KEYS)
    _reject_unknown(f"{where} [timeouts]", timeouts, _TIMEOUT_KEYS)

    commands = raw.get("commands")
    if not isinstance(commands, list) or not commands:
        raise InvalidTaskDefinition(f"{where}: at least one [[commands]] is required")

    pairs: list[tuple[str, CommandRole]] = []
    for position, entry in enumerate(commands):
        label = f"{where} [[commands]] #{position}"
        if not isinstance(entry, dict):
            raise InvalidTaskDefinition(f"{label}: must be a table")
        _reject_unknown(label, entry, _COMMAND_KEYS)
        role_name = _string(label, entry, "role")
        try:
            role = CommandRole(role_name)
        except ValueError as error:
            valid = [member.value for member in CommandRole]
            raise InvalidTaskDefinition(
                f"{label}: unknown role {role_name!r}; valid roles are {valid}"
            ) from error
        pairs.append((_string(label, entry, "command"), role))

    suite_name = _string(f"{where} [task]", task, "suite")
    try:
        suite = Suite(suite_name)
    except ValueError as error:
        valid = [member.value for member in Suite]
        raise InvalidTaskDefinition(
            f"{where} [task]: unknown suite {suite_name!r}; valid suites are {valid}"
        ) from error

    try:
        return TaskDefinition(
            task_id=TaskId(_string(f"{where} [task]", task, "id")),
            suite=suite,
            description=_string(f"{where} [task]", task, "description"),
            fixture_id=_string(f"{where} [fixture]", fixture, "id"),
            fixture_sha256=_string(f"{where} [fixture]", fixture, "sha256"),
            hidden_paths=_string_list(f"{where} [fixture]", fixture, "hidden_paths"),
            commands=TaskDefinition.index_commands(pairs),
            per_command_timeout_ms=_integer(
                f"{where} [timeouts]", timeouts, "per_command_ms"
            ),
            total_timeout_ms=_integer(f"{where} [timeouts]", timeouts, "total_ms"),
        )
    except InvalidTaskDefinition:
        raise
    except DomainError as error:
        # The domain's messages are better than anything this layer would
        # write; the path is what it cannot know.
        raise InvalidTaskDefinition(f"{where}: {error}") from error


def load_tasks(directory: Path) -> tuple[TaskDefinition, ...]:
    """Every `*.toml` directly inside `directory`, in filename order."""
    if not directory.is_dir():
        raise InvalidTaskDefinition(f"{directory} is not a directory")
    return tuple(load_task(path) for path in sorted(directory.glob("*.toml")))


def fixture_root_for(task: TaskDefinition, fixtures_root: Path) -> Path:
    """Where a task's fixture lives.

    `fixture_id` is already constrained to a safe path segment by
    `TaskDefinition.__post_init__` -- that validation exists for this call
    site. The containment check below is defence in depth, and both sides are
    resolved because on macOS a temporary directory is reached through a
    `/var` symlink and an unresolved comparison fails spuriously.
    """
    root = fixtures_root.resolve()
    candidate = (root / task.fixture_id).resolve()
    if not candidate.is_relative_to(root):
        raise InvalidTaskDefinition(
            f"{task.task_id}: fixture {task.fixture_id!r} resolves outside {root}"
        )
    return candidate
