"""Real-repository tasks: the environment they name and the test patch they carry."""

from __future__ import annotations

import pytest

from control_plane.domain.errors import DomainError, InvalidTaskDefinition
from control_plane.domain.patch import paths_in_unified_diff
from control_plane.domain.task import Environment, Suite
from tests.test_domain_task import make_task

ENVIRONMENT = Environment(image="afrunner-oss:pallets-flask-4045", workdir="/testbed")


def test_an_oss_task_names_its_environment() -> None:
    task = make_task(suite=Suite.OSS, environment=ENVIRONMENT)

    assert task.environment == ENVIRONMENT


def test_an_oss_task_without_an_environment_is_refused() -> None:
    """The default image has no real repository's interpreter or dependencies."""
    with pytest.raises(InvalidTaskDefinition, match="names no environment"):
        make_task(suite=Suite.OSS)


@pytest.mark.parametrize("workdir", ["testbed", "/", "/a/../b", "/a,b", "/a//b"])
def test_a_workdir_that_is_not_a_plain_absolute_path_is_refused(workdir: str) -> None:
    with pytest.raises(InvalidTaskDefinition, match="workdir"):
        Environment(image="img:1", workdir=workdir)


@pytest.mark.parametrize("image", ["", "img 1"])
def test_an_unusable_image_reference_is_refused(image: str) -> None:
    with pytest.raises(InvalidTaskDefinition, match="image"):
        Environment(image=image, workdir="/testbed")


def test_an_empty_test_patch_is_refused() -> None:
    with pytest.raises(InvalidTaskDefinition, match="empty test patch"):
        make_task(test_patch="  \n")


PATCH = """\
diff --git a/tests/test_blueprints.py b/tests/test_blueprints.py
--- a/tests/test_blueprints.py
+++ b/tests/test_blueprints.py
@@ -1 +1 @@
-old
+new
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1 @@
+added
"""


def test_a_diff_names_each_file_it_touches_once() -> None:
    assert paths_in_unified_diff(PATCH) == (
        "tests/test_blueprints.py",
        "tests/test_new.py",
    )


def test_a_diff_that_leaves_the_workspace_is_refused() -> None:
    escaping = "--- a/../etc/passwd\n+++ b/../etc/passwd\n@@ -1 +1 @@\n-a\n+b\n"

    with pytest.raises(DomainError):
        paths_in_unified_diff(escaping)


def test_text_that_names_no_file_is_not_a_diff() -> None:
    with pytest.raises(DomainError, match="names no file"):
        paths_in_unified_diff("just some text\n")
