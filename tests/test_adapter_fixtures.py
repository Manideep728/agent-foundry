"""Walking a real fixture tree on a real filesystem."""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.adapters.fixtures import digest_tree, walk_entries
from control_plane.domain.errors import InvalidFixture


def build_fixture(root: Path) -> Path:
    (root / "csvkit").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "csvkit" / "__init__.py").write_text("")
    (root / "csvkit" / "fields.py").write_text("def split_fields(line):\n    ...\n")
    (root / "tests" / "test_basic.py").write_text("def test_x():\n    pass\n")
    (root / "README.md").write_text("# csvkit\n")
    return root


def test_the_same_tree_hashes_the_same_twice(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "fx")
    assert digest_tree(root)[0] == digest_tree(root)[0]


def test_two_identical_trees_hash_identically(tmp_path: Path) -> None:
    a = build_fixture(tmp_path / "a")
    b = build_fixture(tmp_path / "b")
    assert digest_tree(a)[0] == digest_tree(b)[0]


def test_differing_git_directories_leave_the_digest_unchanged(tmp_path: Path) -> None:
    # The single most important exclusion. .git/index embeds per-file stat data
    # -- inode, ctime, uid -- and packfiles differ after any repack, so two
    # clones of the same commit have byte-different .git contents. Without this
    # exclusion a task's fixture_sha256 would never match twice, on any
    # machine, and every run would die with FIXTURE_HASH_MISMATCH.
    a = build_fixture(tmp_path / "a")
    b = build_fixture(tmp_path / "b")
    for root, payload in ((a, b"index-from-clone-one"), (b, b"totally-different")):
        (root / ".git").mkdir()
        (root / ".git" / "index").write_bytes(payload)

    assert digest_tree(a)[0] == digest_tree(b)[0]


def test_running_the_tests_does_not_change_the_digest(tmp_path: Path) -> None:
    # __pycache__ appears the first time anything imports the fixture, which
    # happens after the copy. Without the exclusion, a fixture hashes fine
    # until someone runs its tests once in the source tree, then never again.
    root = build_fixture(tmp_path / "fx")
    before = digest_tree(root)[0]

    (root / "csvkit" / "__pycache__").mkdir()
    (root / "csvkit" / "__pycache__" / "fields.cpython-312.pyc").write_bytes(
        b"\x00\x01"
    )
    (root / ".pytest_cache").mkdir()
    (root / ".pytest_cache" / "lastfailed").write_text("{}")

    assert digest_tree(root)[0] == before


def test_opening_the_folder_in_finder_does_not_change_the_digest(
    tmp_path: Path,
) -> None:
    root = build_fixture(tmp_path / "fx")
    before = digest_tree(root)[0]
    (root / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    assert digest_tree(root)[0] == before


def test_changed_content_changes_the_digest(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "fx")
    before = digest_tree(root)[0]
    (root / "csvkit" / "fields.py").write_text("def split_fields(line):\n    pass\n")
    assert digest_tree(root)[0] != before


def test_a_new_file_changes_the_digest(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "fx")
    before = digest_tree(root)[0]
    (root / "csvkit" / "extra.py").write_text("")
    assert digest_tree(root)[0] != before


def test_the_executable_bit_changes_the_digest(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "fx")
    script = root / "check.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o644)
    before = digest_tree(root)[0]

    script.chmod(0o755)
    assert digest_tree(root)[0] != before


def test_a_symlink_is_refused(tmp_path: Path) -> None:
    # Following one escapes the fixture; a cycle would hang the walk.
    root = build_fixture(tmp_path / "fx")
    (root / "link.py").symlink_to(root / "csvkit" / "fields.py")

    with pytest.raises(InvalidFixture, match="symlink"):
        digest_tree(root)


def test_an_empty_directory_is_not_part_of_identity(tmp_path: Path) -> None:
    # Documented limitation, shared with git -- which is what keeps
    # fixture-as-committed and fixture-as-hashed in agreement.
    root = build_fixture(tmp_path / "fx")
    before = digest_tree(root)[0]
    (root / "build").mkdir()
    assert digest_tree(root)[0] == before


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidFixture, match="not a directory"):
        digest_tree(tmp_path / "nope")


def test_entries_come_back_sorted_and_exclude_noise(tmp_path: Path) -> None:
    root = build_fixture(tmp_path / "fx")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    paths = [e.path for e in walk_entries(root)]
    assert paths == sorted(paths)
    assert paths == [
        "README.md",
        "csvkit/__init__.py",
        "csvkit/fields.py",
        "tests/test_basic.py",
    ]
