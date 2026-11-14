"""Loading a real-repository task: its environment and its pinned test patch."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from control_plane.adapters.task_toml import base_image, load_task
from control_plane.domain.errors import InvalidTaskDefinition
from control_plane.domain.task import Environment, Suite

PATCH = "--- a/tests/t.py\n+++ b/tests/t.py\n@@ -1 +1 @@\n-a\n+b\n"
PATCH_SHA = hashlib.sha256(PATCH.encode()).hexdigest()

TEMPLATE = """
schema_version = 1

[task]
id = "pallets-flask-4045"
suite = "oss"
description = "Raise an error for a blueprint name with a dot."

[fixture]
id = "pallets-flask-4045"
sha256 = "{fixture}"
{patch_keys}

[environment]
base_image = "swebench/sweb.eval.x86_64.pallets_1776_flask-4045@sha256:{base}"
image = "afrunner-oss:pallets-flask-4045"
workdir = "/testbed"

[timeouts]
per_command_ms = 300000
total_ms = 600000

[[commands]]
role = "fail_to_pass"
command = "python -m pytest tests/test_blueprints.py"
"""


def write(
    tmp_path: Path, *, patch_keys: str | None = None, patch_text: str = PATCH
) -> Path:
    (tmp_path / "patches").mkdir(exist_ok=True)
    (tmp_path / "patches" / "flask.test.patch").write_text(patch_text, encoding="utf-8")
    if patch_keys is None:
        patch_keys = (
            'test_patch = "patches/flask.test.patch"\n'
            f'test_patch_sha256 = "{PATCH_SHA}"'
        )
    path = tmp_path / "task.toml"
    path.write_text(
        TEMPLATE.format(fixture="a" * 64, base="b" * 64, patch_keys=patch_keys),
        encoding="utf-8",
    )
    return path


def test_an_oss_task_loads_with_its_environment_and_test_patch(tmp_path: Path) -> None:
    task = load_task(write(tmp_path))

    assert task.suite is Suite.OSS
    assert task.environment == Environment(
        image="afrunner-oss:pallets-flask-4045", workdir="/testbed"
    )
    assert task.test_patch == PATCH


def test_a_test_patch_that_changed_since_it_was_pinned_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(InvalidTaskDefinition, match="the task pins"):
        load_task(write(tmp_path, patch_text=PATCH + "+one more line\n"))


def test_a_test_patch_outside_the_task_directory_is_refused(tmp_path: Path) -> None:
    keys = f'test_patch = "../elsewhere.patch"\ntest_patch_sha256 = "{PATCH_SHA}"'

    with pytest.raises(InvalidTaskDefinition, match="leaves"):
        load_task(write(tmp_path, patch_keys=keys))


def test_a_pin_without_a_patch_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidTaskDefinition, match="without test_patch"):
        load_task(write(tmp_path, patch_keys=f'test_patch_sha256 = "{PATCH_SHA}"'))


def test_the_base_image_is_readable_for_fetching(tmp_path: Path) -> None:
    assert base_image(write(tmp_path)) == (
        f"swebench/sweb.eval.x86_64.pallets_1776_flask-4045@sha256:{'b' * 64}"
    )


def test_a_synthetic_task_has_no_base_image() -> None:
    repo = Path(__file__).resolve().parent.parent
    assert base_image(repo / "tasks" / "csv-quoted-fields.toml") is None
