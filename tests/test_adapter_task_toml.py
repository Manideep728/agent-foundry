"""Loading task definitions.

Every rejection guards against a task that loads with a wrong value and then
produces a plausible number, which is worse than a crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.adapters.fixtures import digest_tree
from control_plane.adapters.task_toml import (
    fixture_root_for,
    load_task,
    load_tasks,
)
from control_plane.domain.errors import InvalidTaskDefinition
from control_plane.domain.task import CommandRole, Suite

REPO = Path(__file__).resolve().parent.parent
TASK_FILE = REPO / "tasks" / "csv-quoted-fields.toml"
FIXTURES = REPO / "tasks" / "fixtures"

VALID = """
schema_version = 1

[task]
id = "demo-001"
suite = "synthetic"
description = "Fix the thing."

[fixture]
id = "demo"
sha256 = "{sha}"

[timeouts]
per_command_ms = 30000
total_ms = 120000

[[commands]]
role = "fail_to_pass"
command = "python3 -m unittest tests.test_a"

[[commands]]
role = "pass_to_pass"
command = "python3 -m unittest tests.test_b"
""".format(sha="a" * 64)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "task.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_committed_task_file_loads(tmp_path: Path) -> None:
    task = load_task(TASK_FILE)
    assert task.suite is Suite.SYNTHETIC
    assert [c.index for c in task.commands] == [0, 1]
    assert task.commands[0].role is CommandRole.FAIL_TO_PASS
    assert task.commands[1].role is CommandRole.PASS_TO_PASS


def test_the_committed_task_pins_the_fixture_actually_on_disk() -> None:
    # The most likely real drift: someone edits the fixture and forgets to
    # re-hash. Without this the failure appears much later as a
    # FIXTURE_HASH_MISMATCH at run time, with nothing pointing at the cause.
    task = load_task(TASK_FILE)
    digest, _ = digest_tree(fixture_root_for(task, FIXTURES))
    assert task.fixture_sha256 == digest, (
        "tasks/csv-quoted-fields.toml pins a digest the fixture no longer has; "
        "re-run `af fixture-hash tasks/fixtures/csv-quoted-fields`"
    )


def test_a_valid_file_loads(tmp_path: Path) -> None:
    task = load_task(write(tmp_path, VALID))
    assert str(task.task_id) == "demo-001"
    assert task.per_command_timeout_ms == 30000


# --- rejections --------------------------------------------------------------


def test_an_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidTaskDefinition, match="unknown key"):
        load_task(write(tmp_path, VALID + '\nextra = "?"\n'))


def test_a_typo_in_a_timeout_key_is_rejected(tmp_path: Path) -> None:
    # The case this rule exists for. Silently defaulting would run the task
    # under a budget nobody wrote down.
    text = VALID.replace("per_command_ms = 30000", "pre_command_ms = 30000")
    with pytest.raises(InvalidTaskDefinition, match="unknown key"):
        load_task(write(tmp_path, text))


def test_a_float_timeout_is_rejected(tmp_path: Path) -> None:
    text = VALID.replace("per_command_ms = 30000", "per_command_ms = 30000.0")
    with pytest.raises(InvalidTaskDefinition, match="float"):
        load_task(write(tmp_path, text))


def test_a_boolean_timeout_is_rejected(tmp_path: Path) -> None:
    # bool is a subclass of int in Python, so a naive isinstance check would
    # load `true` as 1 millisecond.
    text = VALID.replace("per_command_ms = 30000", "per_command_ms = true")
    with pytest.raises(InvalidTaskDefinition, match="integer"):
        load_task(write(tmp_path, text))


def test_an_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidTaskDefinition, match="schema_version"):
        load_task(
            write(tmp_path, VALID.replace("schema_version = 1", "schema_version = 2"))
        )


def test_an_unknown_role_names_the_valid_ones(tmp_path: Path) -> None:
    text = VALID.replace('role = "pass_to_pass"', 'role = "should_pass"')
    with pytest.raises(InvalidTaskDefinition, match="fail_to_pass"):
        load_task(write(tmp_path, text))


def test_an_unknown_suite_names_the_valid_ones(tmp_path: Path) -> None:
    text = VALID.replace('suite = "synthetic"', 'suite = "smoke"')
    with pytest.raises(InvalidTaskDefinition, match="synthetic"):
        load_task(write(tmp_path, text))


def test_a_task_with_no_commands_is_rejected(tmp_path: Path) -> None:
    text = VALID[: VALID.index("[[commands]]")]
    with pytest.raises(InvalidTaskDefinition, match="commands"):
        load_task(write(tmp_path, text))


def test_a_task_with_only_pass_to_pass_commands_is_rejected(tmp_path: Path) -> None:
    # The domain rule surfacing through the loader: such a task cannot tell a
    # working agent from one that changed nothing.
    text = VALID.replace('role = "fail_to_pass"', 'role = "pass_to_pass"')
    with pytest.raises(InvalidTaskDefinition, match="fail_to_pass"):
        load_task(write(tmp_path, text))


def test_a_malformed_fixture_hash_is_rejected(tmp_path: Path) -> None:
    text = VALID.replace("a" * 64, "not-a-hash")
    with pytest.raises(InvalidTaskDefinition, match="fixture_sha256"):
        load_task(write(tmp_path, text))


def test_invalid_toml_names_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, "this is not toml =\n")
    with pytest.raises(InvalidTaskDefinition, match=str(path)):
        load_task(path)


def test_a_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidTaskDefinition, match="no such task file"):
        load_task(tmp_path / "absent.toml")


# --- fixture resolution ------------------------------------------------------


def test_fixture_resolution_stays_inside_the_fixtures_root() -> None:
    task = load_task(TASK_FILE)
    root = fixture_root_for(task, FIXTURES)
    assert root == (FIXTURES / "csv-quoted-fields").resolve()


def test_load_tasks_reads_a_directory() -> None:
    tasks = load_tasks(REPO / "tasks")
    assert [str(t.task_id) for t in tasks] == ["csv-quoted-fields"]
