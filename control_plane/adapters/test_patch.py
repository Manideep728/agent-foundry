"""Applying a task's test patch to a workspace (D39).

The way SWE-bench's own harness does it: every file the patch touches is first
put back the way the fixture has it, and then the patch is applied. An agent
that edited a graded test therefore cannot change what it is graded on, and a
patch that still does not apply is a fault in the task, not in the agent.

`git apply` rather than an applier written here, because applying hunks with
offsets and fuzz is exactly the code that is wrong in ways tests do not find.
Git is run as a plain patch tool: the workspace holds no `.git`, and
`GIT_CEILING_DIRECTORIES` stops git from finding the repository that the runs
directory may sit inside. Without that, `git apply` would resolve the patch's
paths against that repository's root.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from control_plane.domain.errors import DomainError
from control_plane.domain.patch import paths_in_unified_diff
from control_plane.services.errors import GradingPatchNotApplied

_GIT_TIMEOUT_S = 60


class GitTestPatcher:
    """A `GradingPatcher` that restores from the fixture and runs `git apply`."""

    def apply(self, repo: Path, fixture_root: Path, patch: str) -> None:
        try:
            paths = paths_in_unified_diff(patch)
        except DomainError as error:
            raise GradingPatchNotApplied(
                f"the test patch is unusable: {error}"
            ) from error
        for path in paths:
            _restore(repo / path, fixture_root / path)
        apply_patch(repo, patch, what="the test patch")


def _restore(target: Path, original: Path) -> None:
    """Make one path what the fixture has: its content, or absent."""
    if original.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
    elif target.is_file() or target.is_symlink():
        target.unlink()


def apply_patch(repo: Path, patch: str, *, what: str) -> None:
    """Apply a unified diff inside `repo`, which must not be a git work tree."""
    environment = dict(os.environ)
    environment["GIT_CEILING_DIRECTORIES"] = str(repo.resolve().parent)
    try:
        completed = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            input=patch.encode("utf-8"),
            cwd=repo,
            env=environment,
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as error:
        raise GradingPatchNotApplied(
            f"git is not installed, so {what} cannot be applied"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise GradingPatchNotApplied(f"git apply did not finish {what}") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:500]
        raise GradingPatchNotApplied(f"{what} does not apply: {detail}")
