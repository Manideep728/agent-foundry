"""Fixture canonicalisation.

Every rule here removes a way two machines could disagree about the identity
of the same tree. The tests name the disagreement.
"""

from __future__ import annotations

import pytest

from control_plane.domain.errors import InvalidFixture
from control_plane.domain.fixture import (
    MANIFEST_PREFIX,
    FixtureEntry,
    is_excluded,
    manifest_bytes,
    validate_fixture_path,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def entry(path: str, sha: str = SHA_A, executable: bool = False) -> FixtureEntry:
    return FixtureEntry(path=path, executable=executable, content_sha256=sha)


def test_manifest_does_not_depend_on_the_order_it_was_given() -> None:
    # The whole point. If this sorted only when the caller remembered to, the
    # digest would depend on the order the filesystem returned directory
    # entries, which differs between filesystems and between checkouts.
    forwards = [entry("a.py"), entry("b.py", SHA_B), entry("z/c.py")]
    assert manifest_bytes(forwards) == manifest_bytes(list(reversed(forwards)))


def test_the_manifest_is_prefixed_with_its_format_version() -> None:
    # A future framing change must invalidate stored hashes loudly. Without the
    # prefix, a format change is indistinguishable from a fixture change.
    assert manifest_bytes([entry("a.py")]).startswith(MANIFEST_PREFIX)


def test_the_executable_bit_changes_the_digest() -> None:
    # A fixture whose command is ./scripts/check.sh works for its author and
    # then fails with Permission denied if a copy silently drops the bit.
    plain = manifest_bytes([entry("run.sh")])
    executable = manifest_bytes([entry("run.sh", executable=True)])
    assert plain != executable


def test_content_and_path_both_affect_the_digest() -> None:
    assert manifest_bytes([entry("a.py")]) != manifest_bytes([entry("a.py", SHA_B)])
    assert manifest_bytes([entry("a.py")]) != manifest_bytes([entry("b.py")])


def test_paths_containing_spaces_do_not_break_the_framing() -> None:
    # The path is last on the line precisely so a space in it is unambiguous.
    manifest = manifest_bytes([entry("my dir/my file.py"), entry("a.py")])
    assert b"my dir/my file.py\n" in manifest
    assert manifest.count(b"\n") == 3  # prefix + two entries


def test_an_empty_fixture_is_rejected() -> None:
    # Otherwise every empty fixture hashes to the same constant and matches
    # every other empty fixture's pin.
    with pytest.raises(InvalidFixture, match="at least one file"):
        manifest_bytes([])


def test_duplicate_entries_are_rejected() -> None:
    with pytest.raises(InvalidFixture, match="duplicate"):
        manifest_bytes([entry("a.py"), entry("a.py", SHA_B)])


def test_an_excluded_path_cannot_reach_the_manifest() -> None:
    with pytest.raises(InvalidFixture, match="excluded"):
        manifest_bytes([entry("__pycache__/a.cpython-312.pyc")])


# --- what counts as part of a fixture ---------------------------------------


@pytest.mark.parametrize(
    ("path", "why"),
    [
        (".git/index", "embeds per-file stat data, so two clones differ"),
        (".git/objects/pack/pack-abc.pack", "repacking changes the bytes"),
        ("__pycache__/parser.cpython-312.pyc", "created by running the tests"),
        ("src/__pycache__/x.pyc", "nested"),
        ("src/parser.pyc", "by suffix"),
        (".DS_Store", "macOS writes it when the folder is opened"),
        ("src/.DS_Store", "nested"),
        (".venv/pyvenv.cfg", "embeds absolute host paths"),
        (".pytest_cache/v/cache/lastfailed", "test-run state"),
        ("node_modules/x/index.js", "enormous"),
    ],
)
def test_excluded_paths_are_outside_fixture_identity(path: str, why: str) -> None:
    assert is_excluded(path), f"{path} must be excluded: {why}"


@pytest.mark.parametrize(
    "path",
    ["src/parser.py", "tests/test_parser.py", "README.md", "scripts/check.sh"],
)
def test_real_source_files_are_included(path: str) -> None:
    assert not is_excluded(path)


def test_a_file_merely_named_like_a_cache_is_still_included() -> None:
    # `.gitattributes` starts with ".git" as a string but is a real file the
    # fixture depends on -- it pins line endings. Matching on path segments
    # rather than prefixes is what keeps it.
    assert not is_excluded(".gitattributes")
    assert not is_excluded("src/gitignore_parser.py")


# --- path spelling -----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("", "empty"),
        ("/etc/passwd", "absolute"),
        ("./a.py", "not canonical"),
        ("a//b.py", "empty segment"),
        ("src/", "trailing slash"),
        ("src\\parser.py", "backslash is a separator on Windows only"),
        ("../outside.py", "traversal"),
        ("src/../../x.py", "traversal"),
        ("café.py", "non-ASCII hashes differently on APFS and ext4"),
        ("emoji\U0001f600.py", "non-ASCII"),
        ("tab\there.py", "control character"),
        ("newline\n.py", "would break the framing"),
    ],
)
def test_unreproducible_paths_are_rejected(path: str, reason: str) -> None:
    with pytest.raises(InvalidFixture):
        validate_fixture_path(path)


@pytest.mark.parametrize(
    "path", ["a.py", "src/parser.py", "a/b/c/d.txt", "with space.py", "a.b/c.py"]
)
def test_reproducible_paths_are_accepted(path: str) -> None:
    validate_fixture_path(path)


def test_a_malformed_content_hash_is_rejected() -> None:
    with pytest.raises(InvalidFixture, match="content_sha256"):
        FixtureEntry(path="a.py", executable=False, content_sha256="A" * 64)
    with pytest.raises(InvalidFixture, match="content_sha256"):
        FixtureEntry(path="a.py", executable=False, content_sha256="abc")
