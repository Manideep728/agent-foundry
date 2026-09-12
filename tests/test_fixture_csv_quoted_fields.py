"""The fixture must measure something.

A benchmark task whose "failing" test actually passes is worthless: every
agent, including one that changes nothing, scores a success on it. That is the
same defect `is_task_defect` catches at run time -- caught here at authoring
time instead, where it is cheap.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tasks" / "fixtures" / "csv-quoted-fields"
SOLUTION = REPO / "tasks" / "solutions" / "csv-quoted-fields"

FAIL_TO_PASS = "tests.test_quoting"
PASS_TO_PASS = "tests.test_basic"


def run_unittest(workdir: Path, module: str) -> subprocess.CompletedProcess[str]:
    # subprocess.run closes its pipes, which matters because pyproject sets
    # filterwarnings = ["error"]: a leaked Popen raises ResourceWarning at an
    # arbitrary later collection and fails an unrelated test.
    return subprocess.run(
        [sys.executable, "-m", "unittest", module],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_fixture_exists_with_both_test_modules() -> None:
    assert (FIXTURE / "csvkit" / "fields.py").is_file()
    assert (FIXTURE / "tests" / "test_quoting.py").is_file()
    assert (FIXTURE / "tests" / "test_basic.py").is_file()


def test_the_fail_to_pass_command_fails_on_the_untouched_fixture() -> None:
    # If this ever passes, the task measures nothing and every agent resolves
    # it for free.
    result = run_unittest(FIXTURE, FAIL_TO_PASS)
    assert result.returncode != 0, (
        "the fail-to-pass tests pass on the unpatched fixture, so the task "
        "cannot distinguish a working agent from one that changed nothing"
    )
    assert "FAILED" in result.stderr


def test_the_pass_to_pass_command_passes_on_the_untouched_fixture() -> None:
    # A regression guard that is already failing cannot detect a regression.
    result = run_unittest(FIXTURE, PASS_TO_PASS)
    assert result.returncode == 0, result.stderr


def test_the_solution_makes_the_fail_to_pass_tests_pass(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    shutil.copytree(FIXTURE, workspace)
    shutil.copy2(SOLUTION / "csvkit" / "fields.py", workspace / "csvkit" / "fields.py")

    assert run_unittest(workspace, FAIL_TO_PASS).returncode == 0


def test_the_solution_does_not_regress_the_pass_to_pass_tests(tmp_path: Path) -> None:
    # The regression guard earns its place only if a plausible wrong fix could
    # break it -- stripping quotes unconditionally, or a state machine that
    # drops a trailing empty field, each break test_basic.
    workspace = tmp_path / "repo"
    shutil.copytree(FIXTURE, workspace)
    shutil.copy2(SOLUTION / "csvkit" / "fields.py", workspace / "csvkit" / "fields.py")

    assert run_unittest(workspace, PASS_TO_PASS).returncode == 0


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ('a,"b,c",d', ["a", "b,c", "d"]),
        ('"he said ""hi""",x', ['he said "hi"', "x"]),
        ('"",a', ["", "a"]),
        ("a,b,c", ["a", "b", "c"]),
        ("", [""]),
        ("a,,c", ["a", "", "c"]),
        ("a,b,", ["a", "b", ""]),
    ],
)
def test_the_solution_is_correct_in_isolation(record: str, expected: list[str]) -> None:
    # Imported by path rather than installed, so the solution is exercised
    # without putting the fixture on sys.path for the whole suite.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_solution_fields", SOLUTION / "csvkit" / "fields.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.split_fields(record) == expected
