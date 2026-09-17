"""Every task in the suite must measure something.

A benchmark task whose "failing" test actually passes is worthless: every
agent, including one that changes nothing, scores a success on it. That is the
same defect `is_task_defect` catches at run time -- caught here at authoring
time instead, where it is cheap.

This file is parametrised over `load_tasks`, not written against one fixture.
The single-fixture version of it guarded `csv-quoted-fields` only, so the
second task authored would have been the first task with no guard at all, and
a task that measures nothing looks exactly like a task an agent is good at.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from control_plane.adapters.fixtures import digest_tree
from control_plane.adapters.task_toml import fixture_root_for, load_tasks
from control_plane.domain.task import CommandRole, TaskDefinition

REPO = Path(__file__).resolve().parent.parent
TASKS = REPO / "tasks"
FIXTURES = TASKS / "fixtures"
SOLUTIONS = TASKS / "solutions"

ALL_TASKS = tuple(load_tasks(TASKS))
TASK_IDS = tuple(str(task.task_id) for task in ALL_TASKS)

every_task = pytest.mark.parametrize("task", ALL_TASKS, ids=TASK_IDS)


def commands_for(task: TaskDefinition, role: CommandRole) -> list[str]:
    return [command.command for command in task.commands if command.role is role]


def run_command(workdir: Path, command: str) -> subprocess.CompletedProcess[str]:
    # shell=True on purpose: the task file holds a shell string and the runner
    # executes it under `/bin/sh -c` (D5). Rebuilding an argv here would test a
    # command line the runner never runs.
    #
    # subprocess.run closes its pipes, which matters because pyproject sets
    # filterwarnings = ["error"]: a leaked Popen raises ResourceWarning at an
    # arbitrary later collection and fails an unrelated test.
    return subprocess.run(
        command,
        shell=True,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def prepared_workspace(task: TaskDefinition, tmp_path: Path) -> Path:
    """A copy of the fixture with the known solution overlaid.

    `dirs_exist_ok`, and keyed on `fixture_id`, because that is exactly what
    `FakeAgent.edit` does. A guard that applied the solution some other way
    would pass while the fake agent path was broken.
    """
    workspace = tmp_path / "repo"
    shutil.copytree(fixture_root_for(task, FIXTURES), workspace)
    solution = SOLUTIONS / task.fixture_id
    assert solution.is_dir(), f"{task.task_id} has no solution directory"
    shutil.copytree(solution, workspace, dirs_exist_ok=True)
    return workspace


def test_the_suite_is_not_empty() -> None:
    # A globbing mistake that found no task files would make every
    # parametrised test below silently vacuous.
    assert len(ALL_TASKS) >= 2


@every_task
def test_a_task_pins_the_current_digest_of_its_fixture(task: TaskDefinition) -> None:
    # Editing a fixture without re-pinning fails the run at load time with
    # FIXTURE_HASH_MISMATCH, which is a confusing place to learn it.
    digest, _ = digest_tree(fixture_root_for(task, FIXTURES))
    assert digest == task.fixture_sha256, (
        f"{task.task_id}: fixture digest is {digest}, task pins "
        f"{task.fixture_sha256}; re-run "
        f"`af fixture-hash tasks/fixtures/{task.fixture_id}`"
    )


@every_task
def test_a_task_hides_paths_that_exist(task: TaskDefinition) -> None:
    # A typo in hidden_paths hides nothing, and the prompt then carries the
    # fail-to-pass test. That is silent: the run still works, and the score is
    # simply no longer comparable with one taken without the answer (D18).
    root = fixture_root_for(task, FIXTURES)
    for hidden in task.hidden_paths:
        assert (root / hidden).is_file(), (
            f"{task.task_id} hides {hidden}, which is not a file in the fixture"
        )


@every_task
def test_the_fail_to_pass_commands_fail_on_the_untouched_fixture(
    task: TaskDefinition,
) -> None:
    # If this ever passes, the task measures nothing and every agent resolves
    # it for free.
    root = fixture_root_for(task, FIXTURES)
    commands = commands_for(task, CommandRole.FAIL_TO_PASS)
    assert commands, f"{task.task_id} has no fail-to-pass command"
    for command in commands:
        result = run_command(root, command)
        assert result.returncode != 0, (
            f"{task.task_id}: `{command}` passes on the unpatched fixture, so "
            f"the task cannot tell a working agent from one that changed nothing"
        )


@every_task
def test_the_pass_to_pass_commands_pass_on_the_untouched_fixture(
    task: TaskDefinition,
) -> None:
    # A regression guard that is already failing cannot detect a regression.
    root = fixture_root_for(task, FIXTURES)
    for command in commands_for(task, CommandRole.PASS_TO_PASS):
        result = run_command(root, command)
        assert result.returncode == 0, (
            f"{task.task_id}: `{command}` already fails on the unpatched "
            f"fixture\n{result.stdout}\n{result.stderr}"
        )


@every_task
def test_the_solution_makes_the_fail_to_pass_commands_pass(
    task: TaskDefinition, tmp_path: Path
) -> None:
    # Without this, a task could be unsolvable and every agent would score
    # zero on it for a reason that is nothing to do with the agent.
    workspace = prepared_workspace(task, tmp_path)
    for command in commands_for(task, CommandRole.FAIL_TO_PASS):
        result = run_command(workspace, command)
        assert result.returncode == 0, (
            f"{task.task_id}: the known solution does not satisfy `{command}`\n"
            f"{result.stdout}\n{result.stderr}"
        )


@every_task
def test_the_solution_does_not_regress_the_pass_to_pass_commands(
    task: TaskDefinition, tmp_path: Path
) -> None:
    # The regression guard earns its place only if a plausible wrong fix could
    # break it. Each task's pass-to-pass module is chosen so that one can:
    # paginating with an extra page, merging intervals by aliasing the
    # caller's lists, clamping every backoff wait to the cap, or escaping the
    # finished page instead of each value.
    workspace = prepared_workspace(task, tmp_path)
    for command in commands_for(task, CommandRole.PASS_TO_PASS):
        result = run_command(workspace, command)
        assert result.returncode == 0, (
            f"{task.task_id}: the known solution regresses `{command}`\n"
            f"{result.stdout}\n{result.stderr}"
        )
