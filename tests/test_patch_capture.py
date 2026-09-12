"""Capturing what an agent changed."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from control_plane.adapters.diffing import diff_workspace
from control_plane.adapters.fixtures import digest_tree
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.errors import PreconditionViolated
from control_plane.domain.fixture import FixtureEntry
from control_plane.domain.ids import RunId
from control_plane.domain.patch import ChangeKind, FileChange, Patch

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tasks" / "fixtures" / "csv-quoted-fields"
SOLUTION = REPO / "tasks" / "solutions" / "csv-quoted-fields"

SHA_A = "a" * 64
SHA_B = "b" * 64


def prepared(tmp_path: Path) -> tuple[Path, tuple[FixtureEntry, ...]]:
    """A workspace copied the way the real factory copies one.

    The exclusion filter is not optional here. Running the fixture's tests --
    which this project does, in tests/test_fixture_csv_quoted_fields.py --
    leaves __pycache__ inside the fixture directory, and a raw copytree would
    carry it into the workspace. The digest is unaffected either way because
    __pycache__ is excluded from fixture identity, but the copy would no longer
    match what WorkspaceFactory.copy_fixture produces, so these tests would be
    exercising a workspace the harness never creates.
    """
    workspace = tmp_path / "repo"
    factory = WorkspaceFactory(tmp_path / "runs")
    space = factory.create(RunId("run-diff"))
    factory.copy_fixture(space, FIXTURE)
    shutil.move(str(space.repo), str(workspace))
    _, entries = digest_tree(FIXTURE)
    return workspace, entries


# --- the value types ---------------------------------------------------------


def test_a_modified_entry_with_identical_content_is_refused() -> None:
    # The invariant that keeps NO_PATCH_PRODUCED reachable. A differ emitting
    # no-op entries would make is_empty False for an agent that changed
    # nothing, so a do-nothing agent would look like it attempted something.
    with pytest.raises(PreconditionViolated, match="content is identical"):
        FileChange(
            path="a.py",
            kind=ChangeKind.MODIFIED,
            old_sha256=SHA_A,
            new_sha256=SHA_A,
            old_executable=False,
            new_executable=False,
        )


def test_an_added_file_cannot_claim_old_content() -> None:
    with pytest.raises(PreconditionViolated):
        FileChange(
            path="a.py",
            kind=ChangeKind.ADDED,
            old_sha256=SHA_A,
            new_sha256=SHA_B,
            old_executable=False,
            new_executable=False,
        )


def test_a_mode_change_must_change_the_mode() -> None:
    with pytest.raises(PreconditionViolated, match="execute bit"):
        FileChange(
            path="run.sh",
            kind=ChangeKind.MODE_CHANGED,
            old_sha256=SHA_A,
            new_sha256=SHA_A,
            old_executable=True,
            new_executable=True,
        )


def test_changes_must_be_in_path_order() -> None:
    def change(path: str) -> FileChange:
        return FileChange(
            path=path,
            kind=ChangeKind.ADDED,
            old_sha256=None,
            new_sha256=SHA_A,
            old_executable=False,
            new_executable=False,
        )

    with pytest.raises(PreconditionViolated, match="path order"):
        Patch(changes=(change("b.py"), change("a.py")), unified_diff="")


def test_an_empty_patch_is_empty() -> None:
    assert Patch.empty().is_empty


# --- diffing a real workspace ------------------------------------------------


def test_an_untouched_workspace_produces_an_empty_patch(tmp_path: Path) -> None:
    workspace, entries = prepared(tmp_path)
    patch = diff_workspace(entries, FIXTURE, workspace)

    assert patch.is_empty
    assert patch.unified_diff == ""


def test_test_artefacts_do_not_count_as_agent_edits(tmp_path: Path) -> None:
    # Validation writes __pycache__ into the workspace. If the differ and the
    # hasher used different exclusion rules, every run would report a non-empty
    # patch and NO_PATCH_PRODUCED would be unreachable.
    workspace, entries = prepared(tmp_path)
    (workspace / "csvkit" / "__pycache__").mkdir()
    (workspace / "csvkit" / "__pycache__" / "fields.cpython-312.pyc").write_bytes(
        b"\x00"
    )
    (workspace / ".DS_Store").write_bytes(b"Bud1")

    assert diff_workspace(entries, FIXTURE, workspace).is_empty


def test_the_real_solution_produces_the_expected_patch(tmp_path: Path) -> None:
    workspace, entries = prepared(tmp_path)
    shutil.copy2(SOLUTION / "csvkit" / "fields.py", workspace / "csvkit" / "fields.py")

    patch = diff_workspace(entries, FIXTURE, workspace)

    assert not patch.is_empty
    assert patch.paths == ("csvkit/fields.py",)
    assert patch.changes[0].kind is ChangeKind.MODIFIED
    assert "--- a/csvkit/fields.py" in patch.unified_diff
    assert "+++ b/csvkit/fields.py" in patch.unified_diff
    assert "-    return line.split" in patch.unified_diff
    assert "+    in_quotes = False" in patch.unified_diff


def test_an_added_file_is_reported(tmp_path: Path) -> None:
    workspace, entries = prepared(tmp_path)
    (workspace / "csvkit" / "helper.py").write_text("VALUE = 1\n")

    patch = diff_workspace(entries, FIXTURE, workspace)

    assert patch.paths == ("csvkit/helper.py",)
    assert patch.changes[0].kind is ChangeKind.ADDED
    assert patch.changes[0].old_sha256 is None
    assert "/dev/null" in patch.unified_diff


def test_a_deleted_file_is_reported(tmp_path: Path) -> None:
    workspace, entries = prepared(tmp_path)
    (workspace / "README.md").unlink()

    patch = diff_workspace(entries, FIXTURE, workspace)

    assert patch.paths == ("README.md",)
    assert patch.changes[0].kind is ChangeKind.DELETED
    assert patch.changes[0].new_sha256 is None


def test_a_mode_change_alone_is_reported(tmp_path: Path) -> None:
    workspace, entries = prepared(tmp_path)
    (workspace / "README.md").chmod(0o755)

    patch = diff_workspace(entries, FIXTURE, workspace)

    assert patch.changes[0].kind is ChangeKind.MODE_CHANGED
    assert "new mode 100755" in patch.unified_diff


def test_a_binary_change_is_recorded_without_garbage(tmp_path: Path) -> None:
    workspace, entries = prepared(tmp_path)
    (workspace / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00binary")

    patch = diff_workspace(entries, FIXTURE, workspace)

    assert patch.changes[0].is_binary
    assert "Binary files a/logo.png and b/logo.png differ" in patch.unified_diff
    assert "\x00" not in patch.unified_diff


def test_several_changes_come_back_in_path_order(tmp_path: Path) -> None:
    workspace, entries = prepared(tmp_path)
    (workspace / "README.md").write_text("# changed\n")
    (workspace / "csvkit" / "fields.py").write_text(
        "def split_fields(line):\n    pass\n"
    )
    (workspace / "aaa.py").write_text("first\n")

    patch = diff_workspace(entries, FIXTURE, workspace)

    assert patch.paths == ("README.md", "aaa.py", "csvkit/fields.py")


def test_diffing_does_not_touch_the_fixture(tmp_path: Path) -> None:
    before = digest_tree(FIXTURE)[0]
    workspace, entries = prepared(tmp_path)
    (workspace / "csvkit" / "fields.py").write_text("# clobbered\n")

    diff_workspace(entries, FIXTURE, workspace)

    assert digest_tree(FIXTURE)[0] == before
