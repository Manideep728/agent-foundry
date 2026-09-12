"""Computing what an agent changed.

The workspace is walked again and its manifest compared against the fixture's.
Hash comparison says which files differ; only those are read on both sides and
rendered. The fixture is only ever read, so "the fixture is never mutated"
holds by construction rather than by care.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from control_plane.adapters.fixtures import walk_entries
from control_plane.domain.fixture import FixtureEntry
from control_plane.domain.patch import ChangeKind, FileChange, Patch

_BINARY_SNIFF_BYTES = 8000


def _is_binary(data: bytes) -> bool:
    """Git's heuristic: a NUL byte early in the file."""
    return b"\x00" in data[:_BINARY_SNIFF_BYTES]


def _read(path: Path) -> bytes:
    return path.read_bytes()


def _lines(data: bytes) -> list[str]:
    return data.decode("utf-8", errors="replace").splitlines(keepends=True)


def diff_workspace(
    fixture_entries: tuple[FixtureEntry, ...],
    fixture_root: Path,
    workspace_repo: Path,
) -> Patch:
    """Compare the workspace against the fixture it was copied from."""
    before = {entry.path: entry for entry in fixture_entries}
    after = {entry.path: entry for entry in walk_entries(workspace_repo)}

    changes: list[FileChange] = []
    diff_chunks: list[str] = []

    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)

        if old is not None and new is not None:
            if old.content_sha256 == new.content_sha256:
                if old.executable == new.executable:
                    continue
                changes.append(
                    FileChange(
                        path=path,
                        kind=ChangeKind.MODE_CHANGED,
                        old_sha256=old.content_sha256,
                        new_sha256=new.content_sha256,
                        old_executable=old.executable,
                        new_executable=new.executable,
                    )
                )
                diff_chunks.append(
                    f"diff --agentfoundry a/{path} b/{path}\n"
                    f"old mode {'100755' if old.executable else '100644'}\n"
                    f"new mode {'100755' if new.executable else '100644'}\n"
                )
                continue
            kind, old_bytes, new_bytes = (
                ChangeKind.MODIFIED,
                _read(fixture_root / path),
                _read(workspace_repo / path),
            )
        elif new is not None:
            kind, old_bytes, new_bytes = (
                ChangeKind.ADDED,
                b"",
                _read(workspace_repo / path),
            )
        else:
            kind, old_bytes, new_bytes = (
                ChangeKind.DELETED,
                _read(fixture_root / path),
                b"",
            )

        binary = _is_binary(old_bytes) or _is_binary(new_bytes)
        changes.append(
            FileChange(
                path=path,
                kind=kind,
                old_sha256=old.content_sha256 if old else None,
                new_sha256=new.content_sha256 if new else None,
                old_executable=old.executable if old else False,
                new_executable=new.executable if new else False,
                is_binary=binary,
            )
        )

        if binary:
            # Recorded but not encoded. The structured FileChange still carries
            # both hashes, so the change is visible and auditable; producing an
            # applyable binary patch is a Phase 7 problem the synthetic suite
            # does not have.
            diff_chunks.append(f"Binary files a/{path} and b/{path} differ\n")
            continue

        diff_chunks.extend(
            difflib.unified_diff(
                _lines(old_bytes),
                _lines(new_bytes),
                fromfile=f"a/{path}" if old else "/dev/null",
                tofile=f"b/{path}" if new else "/dev/null",
                n=3,
            )
        )

    return Patch(changes=tuple(changes), unified_diff="".join(diff_chunks))
