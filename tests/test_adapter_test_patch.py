"""Applying a test patch after the agent: restore first, then `git apply`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from control_plane.adapters.test_patch import GitTestPatcher
from control_plane.services.errors import GradingPatchNotApplied

PATCH = """\
diff --git a/tests/test_a.py b/tests/test_a.py
--- a/tests/test_a.py
+++ b/tests/test_a.py
@@ -1 +1,2 @@
 def test_old(): pass
+def test_new(): assert False
diff --git a/tests/test_b.py b/tests/test_b.py
new file mode 100644
--- /dev/null
+++ b/tests/test_b.py
@@ -0,0 +1 @@
+def test_b(): pass
"""


def tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def read(root: Path, name: str) -> str:
    return (root / name).read_text(encoding="utf-8")


def test_the_patch_lands_on_a_workspace_the_agent_left_alone(tmp_path: Path) -> None:
    fixture = tree(tmp_path / "fixture", {"tests/test_a.py": "def test_old(): pass\n"})
    repo = tree(tmp_path / "repo", {"tests/test_a.py": "def test_old(): pass\n"})

    GitTestPatcher().apply(repo, fixture, PATCH)

    assert read(repo, "tests/test_a.py").endswith("def test_new(): assert False\n")
    assert read(repo, "tests/test_b.py") == "def test_b(): pass\n"


def test_an_agent_edit_to_a_graded_test_is_discarded(tmp_path: Path) -> None:
    """Without the restore, an agent could delete the test it is graded on."""
    fixture = tree(tmp_path / "fixture", {"tests/test_a.py": "def test_old(): pass\n"})
    repo = tree(
        tmp_path / "repo",
        {
            "tests/test_a.py": "# emptied by the agent\n",
            "tests/test_b.py": "def test_b(): assert True  # planted\n",
            "src/fix.py": "FIXED = True\n",
        },
    )

    GitTestPatcher().apply(repo, fixture, PATCH)

    assert "def test_new(): assert False" in read(repo, "tests/test_a.py")
    assert read(repo, "tests/test_b.py") == "def test_b(): pass\n"
    assert read(repo, "src/fix.py") == "FIXED = True\n", "the agent's fix stays"


def test_a_workspace_inside_a_git_repository_is_patched_in_place(
    tmp_path: Path,
) -> None:
    """The default runs directory sits inside this project's own checkout."""
    outer = tmp_path / "project"
    outer.mkdir()
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    fixture = tree(tmp_path / "fixture", {"tests/test_a.py": "def test_old(): pass\n"})
    repo = tree(
        outer / "runs" / "r1" / "repo", {"tests/test_a.py": "def test_old(): pass\n"}
    )

    GitTestPatcher().apply(repo, fixture, PATCH)

    assert (repo / "tests" / "test_b.py").is_file()
    assert not (outer / "tests").exists(), "applied against the outer repository"


def test_a_patch_that_does_not_fit_the_fixture_is_refused(tmp_path: Path) -> None:
    fixture = tree(tmp_path / "fixture", {"tests/test_a.py": "something else\n"})
    repo = tree(tmp_path / "repo", {"tests/test_a.py": "something else\n"})

    with pytest.raises(GradingPatchNotApplied, match="does not apply"):
        GitTestPatcher().apply(repo, fixture, PATCH)


def test_a_patch_that_names_a_path_outside_the_workspace_is_refused(
    tmp_path: Path,
) -> None:
    escaping = "--- a/../x\n+++ b/../x\n@@ -1 +1 @@\n-a\n+b\n"

    with pytest.raises(GradingPatchNotApplied, match="unusable"):
        GitTestPatcher().apply(tmp_path, tmp_path, escaping)
