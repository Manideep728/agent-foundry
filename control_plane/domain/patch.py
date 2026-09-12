"""What an agent changed, as a value.

Two representations, because they answer different questions. `changes` is
structured and is what the harness reasons about -- in particular whether the
agent did anything at all. `unified_diff` is text for a human to read and for
the report to show.

The patch is produced by comparing the fixture's manifest with the workspace's,
not by running `git diff`. Git's diff output depends on `diff.algorithm`,
`core.autocrlf`, `diff.noprefix`, `diff.external` and whatever is in the
machine's `~/.gitconfig`, and `git init` would also put a `.git/` directory
inside the workspace where the validation commands could see it. For a project
whose product is reproducibility, neither is a good trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique

from control_plane.domain.errors import PreconditionViolated
from control_plane.domain.fixture import validate_fixture_path


@unique
class ChangeKind(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    MODE_CHANGED = "mode_changed"
    """Content identical, execute bit different. Its own kind because a script
    losing +x changes behaviour while changing no bytes."""


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    kind: ChangeKind
    old_sha256: str | None
    new_sha256: str | None
    old_executable: bool
    new_executable: bool
    is_binary: bool = False

    def __post_init__(self) -> None:
        validate_fixture_path(self.path)

        if self.kind is ChangeKind.ADDED:
            self._require(
                self.old_sha256 is None and self.new_sha256 is not None,
                "an added file has no old content and must have new content",
            )
        elif self.kind is ChangeKind.DELETED:
            self._require(
                self.old_sha256 is not None and self.new_sha256 is None,
                "a deleted file has old content and no new content",
            )
        elif self.kind is ChangeKind.MODIFIED:
            self._require(
                self.old_sha256 is not None and self.new_sha256 is not None,
                "a modified file has both old and new content",
            )
            # The important one. A differ that emitted entries for unchanged
            # files would make Patch.is_empty False for an agent that changed
            # nothing, and AgentOutcome.NO_PATCH_PRODUCED would become
            # unreachable -- so a do-nothing agent would be scored as having
            # attempted something.
            self._require(
                self.old_sha256 != self.new_sha256,
                f"{self.path} is listed as modified but its content is identical",
            )
        else:
            self._require(
                self.old_sha256 == self.new_sha256 and self.old_sha256 is not None,
                "a mode change leaves content identical",
            )
            self._require(
                self.old_executable != self.new_executable,
                "a mode change must change the execute bit",
            )

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise PreconditionViolated(f"{self.path}: {message}")


@dataclass(frozen=True, slots=True)
class Patch:
    changes: tuple[FileChange, ...]
    unified_diff: str

    def __post_init__(self) -> None:
        paths = [change.path for change in self.changes]
        if len(set(paths)) != len(paths):
            raise PreconditionViolated("a patch lists the same path twice")
        if paths != sorted(paths):
            raise PreconditionViolated("patch changes must be in path order")

    @property
    def is_empty(self) -> bool:
        """Whether the agent changed nothing.

        Drives `AgentOutcome.NO_PATCH_PRODUCED` and the task-defect audit, so
        it must be true exactly when no file differs.
        """
        return not self.changes

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.changes)

    @staticmethod
    def empty() -> Patch:
        return Patch(changes=(), unified_diff="")
