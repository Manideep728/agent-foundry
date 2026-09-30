"""The build identity every run records.

Each test is one way `harness_version` could name the wrong build, or name
one when it cannot know.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from control_plane import __version__
from control_plane.adapters.system import harness_version


def git(checkout: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A repository with one commit and one tracked file."""
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "--quiet")
    (root / "tracked.txt").write_text("one\n")
    git(root, "add", "tracked.txt")
    git(root, "commit", "--quiet", "-m", "One")
    return root


def test_a_clean_checkout_is_named_by_version_and_commit(checkout: Path) -> None:
    commit = git(checkout, "rev-parse", "--short=12", "HEAD")

    assert harness_version(checkout) == f"{__version__}+{commit}"


def test_a_tag_does_not_change_the_name(checkout: Path) -> None:
    # Without `--exclude '*'`, describe answers with the tag, so one commit
    # would be named two ways depending on which tags a machine had fetched.
    git(checkout, "tag", "v0.1.0")

    assert harness_version(checkout) == (
        f"{__version__}+{git(checkout, 'rev-parse', '--short=12', 'HEAD')}"
    )


def test_uncommitted_changes_are_named_as_such(checkout: Path) -> None:
    (checkout / "tracked.txt").write_text("two\n")

    version = harness_version(checkout)

    assert version is not None
    assert re.fullmatch(rf"{re.escape(__version__)}\+[0-9a-f]{{12}}\.dirty", version)


def test_a_directory_inside_another_repository_names_no_build(
    checkout: Path,
) -> None:
    # git would walk up and report the enclosing repository's commit, which
    # describes some other tree entirely -- an installation inside a project
    # checkout, say.
    inner = checkout / "site-packages"
    inner.mkdir()

    assert harness_version(inner) is None


def test_no_git_executable_names_no_build(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")

    assert harness_version(checkout) is None
