"""Preparing the isolated copy a run operates on.

The fixture is read-only as far as a run is concerned. Everything happens in
`runs/<run_id>/`, and the copy is re-hashed against the source so a copy that
silently dropped an execute bit or followed a link is caught here rather than
surfacing as an unexplained test failure later.

Layout::

    runs/<run_id>/
      repo/          the working copy: the agent edits here, commands run here
      out/
        logs/        per-command stdout and stderr
      patch.diff     what the agent changed
      manifest.txt   the verified fixture manifest

Not `tempfile.mkdtemp()`. The operating system reaps `/tmp` on its own
schedule, so a failed run's evidence would disappear exactly when it is wanted;
`runs/<run_id>` is inspectable from the repository root and is gitignored.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from control_plane.adapters.fixtures import digest_tree
from control_plane.domain.errors import DomainError
from control_plane.domain.fixture import FixtureEntry, is_excluded, manifest_bytes
from control_plane.domain.ids import RunId


class WorkspaceError(DomainError):
    """A workspace could not be created, copied, or verified."""


@dataclass(frozen=True, slots=True)
class Workspace:
    """Where one run's files live."""

    run_id: RunId
    root: Path

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def logs(self) -> Path:
        return self.out / "logs"

    @property
    def patch_path(self) -> Path:
        return self.root / "patch.diff"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.txt"


def _ignore_excluded(directory: str, names: list[str]) -> set[str]:
    """The copy filter, derived from the same rule the hasher uses.

    Sharing `is_excluded` is not tidiness. If the copier and the hasher ever
    disagree, the copy's digest stops matching the source's and every run fails
    verification -- or worse, the differ later reports `__pycache__` as an
    agent edit, which makes `NO_PATCH_PRODUCED` unreachable.
    """
    return {name for name in names if is_excluded(name)}


class WorkspaceFactory:
    """Creates and tears down run workspaces under a single root."""

    def __init__(self, runs_root: Path) -> None:
        self._runs_root = runs_root

    def create(self, run_id: RunId) -> Workspace:
        workspace = Workspace(run_id=run_id, root=self._runs_root / run_id.value)
        try:
            # exist_ok=False deliberately: a reused run id must be an error,
            # not a silent overwrite of another run's evidence.
            workspace.root.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise WorkspaceError(
                f"{workspace.root} already exists; a run id must not be reused"
            ) from error
        except OSError as error:
            raise WorkspaceError(
                f"could not create {workspace.root}: {error}"
            ) from error

        workspace.logs.mkdir(parents=True)
        return workspace

    def copy_fixture(
        self, workspace: Workspace, fixture_root: Path
    ) -> tuple[str, tuple[FixtureEntry, ...]]:
        """Copy the fixture in and prove the copy is faithful.

        Returns the digest and entries of the *source*, which the caller needs
        again to work out what the agent changed.
        """
        source_digest, source_entries = digest_tree(fixture_root)

        try:
            shutil.copytree(
                fixture_root,
                workspace.repo,
                symlinks=False,
                ignore=_ignore_excluded,
            )
        except OSError as error:
            raise WorkspaceError(
                f"could not copy {fixture_root} into {workspace.repo}: {error}"
            ) from error

        copy_digest, _ = digest_tree(workspace.repo)
        if copy_digest != source_digest:
            # Milliseconds, and it is the only thing that catches a copytree
            # that dropped an execute bit or used a different exclusion rule
            # than the hasher.
            raise WorkspaceError(
                f"the copy of {fixture_root} is not faithful: source "
                f"{source_digest}, copy {copy_digest}"
            )

        workspace.manifest_path.write_bytes(manifest_bytes(source_entries))
        return source_digest, source_entries

    def discard_repo(self, workspace: Workspace) -> None:
        """Remove the working copy, keeping the evidence.

        `repo/` is the only large thing and is reconstructible from the fixture
        plus the patch. The logs, the patch and the manifest are the record of
        what happened and are kept.
        """
        shutil.rmtree(workspace.repo, ignore_errors=True)

    def discard_all(self, workspace: Workspace) -> None:
        shutil.rmtree(workspace.root, ignore_errors=True)
