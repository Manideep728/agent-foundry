"""Workspace preparation.

The invariant under test throughout: a run never touches the fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.adapters.fixtures import digest_tree
from control_plane.adapters.workspace import WorkspaceError, WorkspaceFactory
from control_plane.domain.ids import RunId

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tasks" / "fixtures" / "csv-quoted-fields"

RUN = RunId("run-0001")


def factory(tmp_path: Path) -> WorkspaceFactory:
    return WorkspaceFactory(tmp_path / "runs")


def test_create_lays_out_the_run_directory(tmp_path: Path) -> None:
    workspace = factory(tmp_path).create(RUN)

    assert workspace.root.is_dir()
    assert workspace.logs.is_dir()
    assert workspace.root.name == "run-0001"


def test_a_reused_run_id_is_refused(tmp_path: Path) -> None:
    # Silently overwriting would destroy another run's evidence.
    workspaces = factory(tmp_path)
    workspaces.create(RUN)
    with pytest.raises(WorkspaceError, match="must not be reused"):
        workspaces.create(RUN)


def test_the_copy_matches_the_source_digest(tmp_path: Path) -> None:
    workspaces = factory(tmp_path)
    workspace = workspaces.create(RUN)

    digest, entries = workspaces.copy_fixture(workspace, FIXTURE)

    assert digest == digest_tree(FIXTURE)[0]
    assert digest_tree(workspace.repo)[0] == digest
    assert len(entries) == 7


def test_the_fixture_is_untouched_by_preparing_a_workspace(tmp_path: Path) -> None:
    # The invariant the whole design protects. A run that mutated its fixture
    # would change the meaning of every later run of every task using it.
    before = digest_tree(FIXTURE)[0]

    workspaces = factory(tmp_path)
    workspace = workspaces.create(RUN)
    workspaces.copy_fixture(workspace, FIXTURE)
    (workspace.repo / "csvkit" / "fields.py").write_text("# clobbered\n")

    assert digest_tree(FIXTURE)[0] == before


def test_the_manifest_is_written_for_inspection(tmp_path: Path) -> None:
    workspaces = factory(tmp_path)
    workspace = workspaces.create(RUN)
    workspaces.copy_fixture(workspace, FIXTURE)

    manifest = workspace.manifest_path.read_text()
    assert manifest.startswith("agentfoundry-fixture-v1\n")
    assert "csvkit/fields.py" in manifest


def test_excluded_files_are_not_copied(tmp_path: Path) -> None:
    # The copier and the hasher share one rule. If they diverged, the copy's
    # digest would stop matching the source's -- or worse, the differ would
    # later report __pycache__ as an agent edit.
    source = tmp_path / "fx"
    (source / "pkg" / "__pycache__").mkdir(parents=True)
    (source / "pkg" / "a.py").write_text("x = 1\n")
    (source / "pkg" / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    (source / ".git").mkdir()
    (source / ".git" / "index").write_bytes(b"stat-data")
    (source / ".DS_Store").write_bytes(b"Bud1")

    workspaces = factory(tmp_path)
    workspace = workspaces.create(RUN)
    workspaces.copy_fixture(workspace, source)

    assert (workspace.repo / "pkg" / "a.py").is_file()
    assert not (workspace.repo / "pkg" / "__pycache__").exists()
    assert not (workspace.repo / ".git").exists()
    assert not (workspace.repo / ".DS_Store").exists()


def test_the_execute_bit_survives_the_copy(tmp_path: Path) -> None:
    # If it did not, the verification would catch it -- which is the point of
    # verifying rather than trusting copytree.
    source = tmp_path / "fx"
    source.mkdir()
    script = source / "check.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    (source / "a.py").write_text("x = 1\n")

    workspaces = factory(tmp_path)
    workspace = workspaces.create(RUN)
    workspaces.copy_fixture(workspace, source)

    assert (workspace.repo / "check.sh").stat().st_mode & 0o100


def test_copying_a_missing_fixture_is_refused(tmp_path: Path) -> None:
    workspaces = factory(tmp_path)
    workspace = workspaces.create(RUN)
    with pytest.raises(Exception):  # noqa: B017 -- InvalidFixture or WorkspaceError
        workspaces.copy_fixture(workspace, tmp_path / "absent")


def test_discard_repo_keeps_the_evidence(tmp_path: Path) -> None:
    # repo/ is the only large thing and is reconstructible from fixture plus
    # patch. Logs and manifest are the record of what happened.
    workspaces = factory(tmp_path)
    workspace = workspaces.create(RUN)
    workspaces.copy_fixture(workspace, FIXTURE)
    workspace.patch_path.write_text("--- a\n+++ b\n")

    workspaces.discard_repo(workspace)

    assert not workspace.repo.exists()
    assert workspace.logs.is_dir()
    assert workspace.patch_path.is_file()
    assert workspace.manifest_path.is_file()


def test_discard_repo_twice_does_not_raise(tmp_path: Path) -> None:
    # Cleanup runs in a finally block; raising there would mask the run's real
    # failure with an unrelated OSError.
    workspaces = factory(tmp_path)
    workspace = workspaces.create(RUN)
    workspaces.copy_fixture(workspace, FIXTURE)

    workspaces.discard_repo(workspace)
    workspaces.discard_repo(workspace)
