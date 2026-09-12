"""Walking a fixture tree and reducing it to a digest.

This is the half of fixture identity that touches the disk. Everything that
could differ between two machines -- ordering, path spelling, which files
count, framing -- lives in `control_plane.domain.fixture`; this module only
reads bytes and calls `stat`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from control_plane.domain.errors import InvalidFixture
from control_plane.domain.fixture import (
    FixtureEntry,
    is_excluded,
    manifest_bytes,
)

_CHUNK_BYTES = 64 * 1024


def _file_sha256(path: Path) -> str:
    """Stream the file so a large fixture does not have to fit in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def walk_entries(root: Path) -> tuple[FixtureEntry, ...]:
    """Every file that contributes to `root`'s identity.

    Raises `InvalidFixture` on a symlink. Following one can escape the fixture
    (a link to /etc/passwd would be hashed and then copied into the workspace),
    a cycle would hang the walk, and hashing the target string instead would
    add a third kind of entry for no current benefit.
    """
    if not root.is_dir():
        raise InvalidFixture(f"{root} is not a directory")

    entries: list[FixtureEntry] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if is_excluded(relative):
            continue
        if path.is_symlink():
            raise InvalidFixture(
                f"{relative} is a symlink; fixtures must be plain files and "
                f"directories so their identity is self-contained"
            )
        if path.is_dir():
            # Directories are not entries. An empty one is therefore not
            # representable -- the same limitation git has, which is what keeps
            # fixture-as-committed and fixture-as-hashed in agreement. Use a
            # .gitkeep when a fixture genuinely needs an empty directory.
            continue
        if not path.is_file():
            raise InvalidFixture(f"{relative} is neither a file nor a directory")

        entries.append(
            FixtureEntry(
                path=relative,
                executable=bool(path.stat().st_mode & 0o100),
                content_sha256=_file_sha256(path),
            )
        )
    return tuple(entries)


def digest_tree(root: Path) -> tuple[str, tuple[FixtureEntry, ...]]:
    """Return `(digest, entries)` for a fixture tree.

    The entries come back with the digest because the caller needs them twice:
    once to verify a copy is faithful, and once to work out which files an
    agent changed without walking the tree a third time.
    """
    entries = walk_entries(root)
    return hashlib.sha256(manifest_bytes(entries)).hexdigest(), entries


def digest_entries(entries: tuple[FixtureEntry, ...]) -> str:
    return hashlib.sha256(manifest_bytes(entries)).hexdigest()
