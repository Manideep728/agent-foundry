"""Turning a SWE-bench instance into a task file, and making it runnable."""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

import pytest

from control_plane.adapters.fake_agent import FakeAgent
from control_plane.adapters.oss_images import extract_fixture
from control_plane.adapters.swebench import (
    fixture_id_for,
    ids_in,
    image_for,
    pytest_command,
    render_task,
    runnable,
)
from control_plane.adapters.task_toml import base_image, load_task
from control_plane.domain.task import CommandRole, Suite
from control_plane.services.errors import AgentError
from tests.docker_support import IMAGE, requires_image
from tests.fakes import FakeClock
from tests.test_domain_task import make_task

TEST_PATCH = "--- a/tests/t.py\n+++ b/tests/t.py\n@@ -1 +1 @@\n-a\n+b\n"

ROW = {
    "instance_id": "pallets__flask-4045",
    "problem_statement": (
        'Raise error when blueprint name contains a dot\n\n"quoted" \\ ok\n'
    ),
    "FAIL_TO_PASS": json.dumps(["tests/test_bp.py::test_dotted_name_not_allowed"]),
    "PASS_TO_PASS": json.dumps(["tests/test_bp.py::test_custom_endpoint[a b]"]),
    "test_patch": TEST_PATCH,
    "patch": "the upstream fix",
}


def test_instance_names_map_to_the_published_image_and_a_safe_fixture_id() -> None:
    assert image_for("pallets__flask-4045") == (
        "swebench/sweb.eval.x86_64.pallets_1776_flask-4045:latest"
    )
    assert fixture_id_for("pallets__flask-4045") == "pallets-flask-4045"


def test_a_parametrised_test_id_survives_the_shell() -> None:
    """Brackets and a space: unquoted, the shell would split or glob it."""
    command = pytest_command(["t.py::test[a b]", "t.py::other"])

    assert shlex.split(command)[-2:] == ["t.py::test[a b]", "t.py::other"]
    assert command.startswith("HOME=/tmp /opt/miniconda3/envs/testbed/bin/python ")


def test_an_id_the_dataset_cut_at_a_space_widens_to_its_function() -> None:
    """Regression: pytest-11143's pass-to-pass command failed on these.

    The dataset stores `test_exprs[multi-line string]` as `test_exprs[multi-line`,
    pytest reports it "not found", and the whole command failed, so the
    instance's own fix scored as a regression.
    """
    ids = [
        "t.py::test_exprs[multi-line",
        "t.py::test_exprs[backslash",
        "t.py::test_exprs[complete]",
        "t.py::test_other",
    ]

    assert runnable(ids) == (
        "t.py::test_exprs",
        "t.py::test_exprs[complete]",
        "t.py::test_other",
    )


def test_a_rendered_command_never_names_a_cut_id() -> None:
    row = ROW | {"PASS_TO_PASS": json.dumps(["t.py::test_exprs[multi-line"])}

    text = render_task(
        row,
        fixture_sha256="d" * 64,
        base_image="img@sha256:" + "c" * 64,
        test_patch_file="p.patch",
        test_patch_sha256="e" * 64,
    )

    assert "test_exprs[multi-line" not in text
    assert "t.py::test_exprs" in text


def test_a_test_list_must_be_a_list_of_ids() -> None:
    with pytest.raises(ValueError, match="list of test ids"):
        ids_in({"FAIL_TO_PASS": json.dumps({"not": "a list"})}, "FAIL_TO_PASS")


def test_a_rendered_task_loads_as_the_instance_it_came_from(tmp_path: Path) -> None:
    (tmp_path / "patches").mkdir()
    (tmp_path / "patches" / "p.test.patch").write_text(TEST_PATCH, encoding="utf-8")
    pinned = f"swebench/sweb.eval.x86_64.pallets_1776_flask-4045@sha256:{'c' * 64}"
    path = tmp_path / "pallets-flask-4045.toml"
    path.write_text(
        render_task(
            ROW,
            fixture_sha256="d" * 64,
            base_image=pinned,
            test_patch_file="patches/p.test.patch",
            test_patch_sha256=hashlib.sha256(TEST_PATCH.encode()).hexdigest(),
        ),
        encoding="utf-8",
    )

    task = load_task(path)

    assert str(task.task_id) == "pallets-flask-4045"
    assert task.suite is Suite.OSS
    assert task.description.startswith("Raise error when blueprint name")
    assert '"quoted" \\ ok' in task.description
    assert task.environment is not None
    assert task.environment.workdir == "/testbed"
    assert base_image(path) == pinned
    assert [c.role for c in task.commands] == [
        CommandRole.FAIL_TO_PASS,
        CommandRole.PASS_TO_PASS,
    ]
    assert "test_dotted_name_not_allowed" in task.commands[0].command


def test_an_instance_with_no_regression_tests_has_one_command(tmp_path: Path) -> None:
    (tmp_path / "p.patch").write_text(TEST_PATCH, encoding="utf-8")
    path = tmp_path / "t.toml"
    path.write_text(
        render_task(
            ROW | {"PASS_TO_PASS": "[]"},
            fixture_sha256="d" * 64,
            base_image="img@sha256:" + "c" * 64,
            test_patch_file="p.patch",
            test_patch_sha256=hashlib.sha256(TEST_PATCH.encode()).hexdigest(),
        ),
        encoding="utf-8",
    )

    assert len(load_task(path).commands) == 1


# --- the reference fix, as a patch --------------------------------------------


def test_the_fake_agent_applies_a_patch_form_solution(tmp_path: Path) -> None:
    solutions = tmp_path / "solutions"
    solutions.mkdir()
    (solutions / "csv-parser.patch").write_text(
        "--- /dev/null\n+++ b/fixed.py\n@@ -0,0 +1 @@\n+FIXED = True\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()

    FakeAgent(solutions, FakeClock()).edit(workspace, make_task())

    assert (workspace / "fixed.py").read_text() == "FIXED = True\n"


def test_a_solution_that_does_not_apply_is_an_agent_error(tmp_path: Path) -> None:
    solutions = tmp_path / "solutions"
    solutions.mkdir()
    (solutions / "csv-parser.patch").write_text(TEST_PATCH, encoding="utf-8")
    workspace = tmp_path / "repo"
    workspace.mkdir()

    with pytest.raises(AgentError, match="does not apply"):
        FakeAgent(solutions, FakeClock()).edit(workspace, make_task())


# --- extracting a fixture from an image ---------------------------------------


@requires_image
def test_a_fixture_is_copied_out_of_an_image_without_running_it(
    tmp_path: Path,
) -> None:
    """The runner image's binary directory stands in for an instance's source tree."""
    destination = tmp_path / "fixture"

    extract_fixture(IMAGE, "/usr/local/bin", destination)

    assert (destination / "afrunner").is_file()


@requires_image
def test_extracting_again_replaces_what_was_there(tmp_path: Path) -> None:
    destination = tmp_path / "fixture"
    destination.mkdir()
    (destination / "stale.txt").write_text("from an older image\n")

    extract_fixture(IMAGE, "/usr/local/bin", destination)

    assert not (destination / "stale.txt").exists()
