"""The committed real-repository tasks: the five D40 selected, each loadable.

Loading checks each test patch against its pinned hash. The fixtures are not in
the repository, so nothing here needs an image or the fixture cache.
"""

from __future__ import annotations

from pathlib import Path

from control_plane.adapters.task_toml import base_image, load_tasks
from control_plane.domain.task import Suite

ROOT = Path(__file__).resolve().parents[1]
OSS_TASKS = ROOT / "tasks" / "oss"
SOLUTIONS = ROOT / "tasks" / "solutions"

SELECTED = {
    "pallets-flask-4045",
    "pallets-flask-4992",
    "pallets-flask-5063",
    "pytest-dev-pytest-11143",
    "pytest-dev-pytest-11148",
}


def test_the_suite_is_the_five_instances_the_rule_selected() -> None:
    tasks = load_tasks(OSS_TASKS)

    assert {str(task.task_id) for task in tasks} == SELECTED
    assert all(task.suite is Suite.OSS for task in tasks)


def test_each_task_names_a_pinned_base_image() -> None:
    for path in sorted(OSS_TASKS.glob("*.toml")):
        pinned = base_image(path)
        assert pinned is not None, path.name
        assert "@sha256:" in pinned, path.name


def test_each_task_has_its_reference_fix() -> None:
    """The oracle row runs the fake agent with these; one missing is a 0/1."""
    for task in load_tasks(OSS_TASKS):
        solution = SOLUTIONS / f"{task.fixture_id}.patch"
        assert solution.is_file(), solution.name
        assert solution.read_text(encoding="utf-8").startswith("diff --git")
