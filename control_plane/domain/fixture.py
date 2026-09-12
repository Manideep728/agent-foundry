"""Canonical identity for a fixture repository.

A task pins its fixture by content hash and refuses to run on a mismatch. That
is only worth anything if the same tree produces the same hash on every machine
the project runs on -- an M1 Mac on APFS and an x86-64 Linux box on ext4 -- so
every rule below exists to remove a way those two could disagree.

The layering is deliberate. This module owns *canonicalisation*: which files
count, how paths are spelled, what order they go in, and how the entries are
framed into bytes. An adapter owns the filesystem walk and the hashing of file
contents. The split keeps this module pure, makes the manifest something a
human can diff to see *what* changed rather than only *that* it changed, and
keeps memory proportional to the number of files rather than their total size.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from control_plane.domain.errors import InvalidFixture

MANIFEST_VERSION = 1
MANIFEST_PREFIX = b"agentfoundry-fixture-v1\n"
"""Prefix hashed ahead of the entries.

A future change to the framing -- adding symlinks, recording empty directories
-- must invalidate every stored hash loudly. Without a version in the hashed
bytes, a format change is indistinguishable from a fixture change: the digest
differs, and nobody can tell which happened.
"""

EXCLUDED_DIRS = frozenset(
    {
        # The one that matters most. .git/index embeds per-file stat data
        # (inode, ctime, uid) and packfiles differ after any repack, so two
        # clones of the same commit hash differently -- meaning fixture_sha256
        # would never match twice, on any machine.
        ".git",
        # Produced by running the tests, which happens after the copy. A .pyc
        # embeds the source mtime and an interpreter magic number, so it
        # differs across Python versions and across machines.
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        # Enormous, and their configs embed absolute host paths.
        ".venv",
        "venv",
        "node_modules",
        ".tox",
        ".idea",
        ".vscode",
    }
)

EXCLUDED_SUFFIXES = (".pyc", ".pyo")

EXCLUDED_NAMES = frozenset(
    {
        # macOS Finder writes this when someone merely opens the folder. The
        # fixture's identity must not depend on whether the author looked at it.
        ".DS_Store",
    }
)


def is_excluded(relative_path: str) -> bool:
    """Whether a path is outside a fixture's identity.

    Shared by the hasher, the copier and the differ. If those three ever
    disagree, a run that writes `__pycache__` produces a non-empty patch and
    `AgentOutcome.NO_PATCH_PRODUCED` becomes unreachable.
    """
    segments = relative_path.split("/")
    if any(segment in EXCLUDED_DIRS for segment in segments[:-1]):
        return True
    name = segments[-1]
    return (
        name in EXCLUDED_DIRS
        or name in EXCLUDED_NAMES
        or name.endswith(EXCLUDED_SUFFIXES)
    )


def validate_fixture_path(path: str) -> None:
    """Reject any path that cannot be spelled identically on two machines."""
    if not path:
        raise InvalidFixture("a fixture entry has an empty path")
    if path.startswith("/"):
        raise InvalidFixture(f"fixture paths are relative; got {path!r}")
    if path.startswith("./") or "//" in path or path.endswith("/"):
        raise InvalidFixture(f"fixture path is not canonical: {path!r}")
    if "\\" in path:
        # A backslash is a separator on Windows and a literal elsewhere, so the
        # same tree would produce two different manifests.
        raise InvalidFixture(f"fixture paths use '/' only; got {path!r}")
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        raise InvalidFixture(f"fixture path has a traversal segment: {path!r}")
    # ASCII-only, and this is the strictest rule here. APFS can return
    # NFD-decomposed Unicode filenames where ext4 returns whatever bytes were
    # written, so "café.py" hashes differently on macOS and Linux -- a
    # difference that is close to undebuggable unless you already know it
    # exists. Space is allowed; it is unambiguous in the framing below.
    for character in path:
        if not (0x20 <= ord(character) <= 0x7E):
            raise InvalidFixture(
                f"fixture paths must be printable ASCII; {path!r} is not"
            )


@dataclass(frozen=True, slots=True)
class FixtureEntry:
    """One file's contribution to a fixture's identity."""

    path: str
    """Relative, '/'-separated, printable ASCII."""

    executable: bool
    """The owner-execute bit only.

    Group and other bits vary with umask between machines. The owner bit is the
    one that changes behaviour: a fixture whose command is `./scripts/check.sh`
    works for its author and then fails with `Permission denied` if a copy
    drops the bit.
    """

    content_sha256: str
    """64 lowercase hex characters, computed by the adapter."""

    def __post_init__(self) -> None:
        validate_fixture_path(self.path)
        if len(self.content_sha256) != 64 or not all(
            c in "0123456789abcdef" for c in self.content_sha256
        ):
            raise InvalidFixture(
                f"{self.path}: content_sha256 must be 64 lowercase hex "
                f"characters, got {self.content_sha256!r}"
            )


def manifest_bytes(entries: Iterable[FixtureEntry]) -> bytes:
    """Render entries as the canonical bytes whose hash is the fixture's id.

    Sorts internally rather than trusting the caller. A pure function that
    depended on argument order would be a hash that depended on the order the
    filesystem happened to return directory entries in, which is exactly the
    difference between two machines this module exists to remove.

    One line per entry: ``<sha256> <x|-> <path>\\n``. The path goes last
    because it is the only variable-width field, and newline and the control
    characters are already rejected from paths, so the framing is unambiguous
    without escaping.
    """
    unique = sorted(entries, key=lambda entry: entry.path)

    seen: set[str] = set()
    for entry in unique:
        if entry.path in seen:
            raise InvalidFixture(f"duplicate fixture entry: {entry.path}")
        seen.add(entry.path)
        if is_excluded(entry.path):
            raise InvalidFixture(
                f"{entry.path} is excluded from fixture identity and must not "
                f"be in the manifest"
            )

    if not unique:
        # Every empty fixture would otherwise hash to the same constant and
        # therefore match every other empty fixture's pin.
        raise InvalidFixture("a fixture must contain at least one file")

    lines = [
        f"{entry.content_sha256} {'x' if entry.executable else '-'} {entry.path}\n"
        for entry in unique
    ]
    return MANIFEST_PREFIX + "".join(lines).encode("ascii")
