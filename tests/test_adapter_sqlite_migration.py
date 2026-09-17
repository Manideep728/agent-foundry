"""Opening a database this build did not create.

The schema moves, and the file outlives the build that wrote it. Until this
existed, an older database was neither upgraded nor refused: `_check_version`
only compared `version > SCHEMA_VERSION`, so an older file fell straight
through and was used against whatever schema it happened to have. The failure
surfaced later, as a query error blaming the query.

`_V1_SCHEMA` below is a frozen copy of the version 1 DDL, written out in full
on purpose. Deriving it from the current `_SCHEMA` by removing whatever the
latest migration added would test the transformation against itself rather
than against history, which is the defect `notes/BUGS.md` records as "a test
that reimplements the thing it is testing is testing the reimplementation".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from control_plane.adapters import sqlite_repo
from control_plane.adapters.sqlite_repo import SCHEMA_VERSION, SqliteRunRepository

_V1_SCHEMA = """
CREATE TABLE runs (
  run_id             TEXT PRIMARY KEY,
  task_id            TEXT NOT NULL,
  suite              TEXT NOT NULL,
  state              TEXT NOT NULL,
  created_at_unix_ms INTEGER NOT NULL,
  updated_at_unix_ms INTEGER NOT NULL,
  failure_kind       TEXT,
  failure_from       TEXT,
  failure_detail     TEXT NOT NULL DEFAULT '',
  fixture_sha256     TEXT NOT NULL,
  runner_id          TEXT NOT NULL,
  runner_version     TEXT NOT NULL,
  arch               TEXT NOT NULL,
  pid1               INTEGER NOT NULL,
  outcome            TEXT,
  resolved           INTEGER,
  regressed          INTEGER,
  f2p_passed         INTEGER,
  f2p_total          INTEGER,
  p2p_passed         INTEGER,
  p2p_total          INTEGER,
  timed_out_commands INTEGER,
  task_defect        INTEGER NOT NULL DEFAULT 0,
  patch_sha256       TEXT,
  patch_bytes        INTEGER NOT NULL DEFAULT 0,
  patch_is_empty     INTEGER,
  total_cost_micros  INTEGER NOT NULL DEFAULT 0,
  agent_ms           INTEGER,
  validation_ms      INTEGER,
  workspace_path     TEXT,
  CHECK ((state = 'failed') = (failure_kind IS NOT NULL)),
  CHECK (updated_at_unix_ms >= created_at_unix_ms),
  CHECK (pid1 IN (0, 1))
) STRICT;

CREATE TABLE validation_reports (
  run_id               TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
  schema_version       INTEGER NOT NULL,
  runner_version       TEXT NOT NULL,
  arch                 TEXT NOT NULL,
  pid1                 INTEGER NOT NULL,
  shell                TEXT NOT NULL,
  workdir              TEXT NOT NULL,
  started_at_unix_ms   INTEGER NOT NULL,
  wall_ms              INTEGER NOT NULL,
  exit_reason          TEXT NOT NULL,
  aborted              INTEGER NOT NULL,
  orphans_reaped_total INTEGER NOT NULL
) STRICT;

CREATE TABLE command_results (
  run_id               TEXT NOT NULL
      REFERENCES validation_reports(run_id) ON DELETE CASCADE,
  idx                  INTEGER NOT NULL,
  command              TEXT NOT NULL,
  started_at_unix_ms   INTEGER NOT NULL,
  duration_ms          INTEGER NOT NULL,
  outcome              TEXT NOT NULL,
  exit_code            INTEGER,
  signal               INTEGER,
  timed_out            INTEGER NOT NULL,
  deadline_signal_sent INTEGER NOT NULL,
  killed_after_grace   INTEGER NOT NULL,
  group_escaped        INTEGER NOT NULL,
  drain_deadline_hit   INTEGER NOT NULL,
  orphans_reaped       INTEGER NOT NULL,
  spawn_error          TEXT,
  stdout_bytes_total   INTEGER NOT NULL,
  stdout_bytes_written INTEGER NOT NULL,
  stdout_truncated     INTEGER NOT NULL,
  stdout_path          TEXT NOT NULL,
  stdout_tail          TEXT NOT NULL,
  stderr_bytes_total   INTEGER NOT NULL,
  stderr_bytes_written INTEGER NOT NULL,
  stderr_truncated     INTEGER NOT NULL,
  stderr_path          TEXT NOT NULL,
  stderr_tail          TEXT NOT NULL,
  PRIMARY KEY (run_id, idx)
) STRICT;

CREATE TABLE model_invocations (
  run_id             TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq                INTEGER NOT NULL,
  provider_id        TEXT NOT NULL,
  model_id           TEXT NOT NULL,
  input_tokens       INTEGER NOT NULL,
  output_tokens      INTEGER NOT NULL,
  cache_write_tokens INTEGER NOT NULL,
  cache_read_tokens  INTEGER NOT NULL,
  cost_micros        INTEGER NOT NULL,
  latency_ms         INTEGER NOT NULL,
  PRIMARY KEY (run_id, seq)
) STRICT;
"""


def build_v1(path: Path, *, journal_mode: str = "wal") -> None:
    """A database exactly as the version 1 build left it."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA journal_mode = {journal_mode}")
        conn.executescript(_V1_SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


def insert_v1_run(path: Path, *, stored_arch: str, report_arch: str) -> None:
    """A scored run as the buggy build stored it: placeholder environment.

    `create()` wrote arch 'unknown' and pid1 0 before any container existed and
    `finish()` never rewrote them, while validation_reports recorded the truth.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, task_id, suite, state, created_at_unix_ms,"
            " updated_at_unix_ms, fixture_sha256, runner_id, runner_version,"
            " arch, pid1) VALUES ('r1', 't1', 'synthetic', 'scored', 1, 2,"
            f" '{'a' * 64}', 'docker', 'afrunner', '{stored_arch}', 0)"
        )
        conn.execute(
            "INSERT INTO validation_reports VALUES"
            f" ('r1', 1, 'afrunner 0.1.0', '{report_arch}', 1, '/bin/sh', '/work',"
            " 1, 5, 'completed', 0, 0)"
        )
        conn.commit()
    finally:
        conn.close()


def read(path: Path, sql: str) -> object:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(sql).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_an_older_database_is_upgraded_rather_than_used_as_is(tmp_path: Path) -> None:
    """Version 1 fell through `version > SCHEMA_VERSION` and was used anyway.

    Nothing upgraded it and nothing refused it, so the mismatch surfaced later
    as a query error naming a column instead of an error naming the database.
    """
    path = tmp_path / "af.sqlite3"
    build_v1(path)
    insert_v1_run(path, stored_arch="unknown", report_arch="x86_64")

    with SqliteRunRepository(path):
        pass

    assert read(path, "PRAGMA user_version") == SCHEMA_VERSION


def test_the_upgrade_repairs_the_environment_a_run_was_stored_with(
    tmp_path: Path,
) -> None:
    """The v2 step backfills from validation_reports, which held the truth.

    Fixing `finish()` stops new rows being wrong; it cannot reach the rows the
    broken build already wrote. Those are repaired from the report table rather
    than guessed, so a run that never validated keeps 'unknown' honestly.
    """
    path = tmp_path / "af.sqlite3"
    build_v1(path)
    insert_v1_run(path, stored_arch="unknown", report_arch="x86_64")

    with SqliteRunRepository(path):
        pass

    assert read(path, "SELECT arch FROM runs WHERE run_id = 'r1'") == "x86_64"
    assert read(path, "SELECT pid1 FROM runs WHERE run_id = 'r1'") == 1


def test_a_run_with_no_report_keeps_its_unknown_environment(tmp_path: Path) -> None:
    # A run that failed before validation genuinely has no architecture to
    # name. Inventing one would put a run that measured nothing into a
    # benchmark table filtered on arch.
    path = tmp_path / "af.sqlite3"
    build_v1(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO runs (run_id, task_id, suite, state, created_at_unix_ms,"
        " updated_at_unix_ms, failure_kind, failure_detail, fixture_sha256,"
        " runner_id, runner_version, arch, pid1) VALUES"
        f" ('r2', 't1', 'synthetic', 'failed', 1, 2, 'runner_failed', 'x',"
        f" '{'a' * 64}', 'docker', 'afrunner', 'unknown', 0)"
    )
    conn.commit()
    conn.close()

    with SqliteRunRepository(path):
        pass

    assert read(path, "SELECT arch FROM runs WHERE run_id = 'r2'") == "unknown"


def test_a_migrated_database_matches_a_fresh_one(tmp_path: Path) -> None:
    """A file's structure must not depend on which build created it.

    The failure this is shaped to catch is `_SCHEMA` and `_MIGRATIONS` drifting
    apart -- a column declared `TEXT NOT NULL DEFAULT ''` in one and added as
    plain `TEXT` in the other. The two databases then store different things
    for the same run and a domain invariant fires on one machine and not the
    other.

    It compares `table_shapes()`, never `sqlite_master.sql`: ADD COLUMN
    rewrites the stored CREATE TABLE text in a way a hand-written schema never
    matches, so a text comparison would fail for a cosmetic reason.
    """
    migrated = tmp_path / "migrated.sqlite3"
    build_v1(migrated)
    fresh = tmp_path / "fresh.sqlite3"

    with SqliteRunRepository(migrated) as upgraded, SqliteRunRepository(fresh) as new:
        assert upgraded.table_shapes() == new.table_shapes()

    assert read(migrated, "PRAGMA user_version") == read(fresh, "PRAGMA user_version")
    assert read(migrated, "PRAGMA journal_mode") == read(fresh, "PRAGMA journal_mode")


def test_a_failed_upgrade_leaves_the_database_on_the_old_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upgrade is one transaction, so a half-applied one cannot commit.

    This is the highest-consequence failure in the mechanism. Python's sqlite3
    opens an implicit transaction only before DML, so DDL under
    `with conn:` runs in autocommit and a rollback undoes nothing. Without the
    explicit BEGIN, a step that failed halfway would leave the file carrying
    part of an upgrade and a version number claiming all of it -- and every
    later open would trust that number.

    It also proves, rather than assumes, that `PRAGMA user_version` is
    transactional.
    """
    path = tmp_path / "af.sqlite3"
    build_v1(path)

    monkeypatch.setattr(
        sqlite_repo,
        "_MIGRATIONS",
        ((2, ("ALTER TABLE runs ADD COLUMN scratch TEXT", "NOT VALID SQL")),),
    )

    with pytest.raises(sqlite3.Error):
        SqliteRunRepository(path)

    assert read(path, "PRAGMA user_version") == 1
    columns = sqlite3.connect(path).execute("PRAGMA table_info(runs)").fetchall()
    assert "scratch" not in [column[1] for column in columns]


def test_a_zero_byte_file_is_given_a_schema_rather_than_used_empty(
    tmp_path: Path,
) -> None:
    """Freshness is a property of the database, not of the file.

    A zero-byte file is a valid empty SQLite database, so `not path.exists()`
    called it old, skipped the schema, and left the first query to fail with
    `no such table: runs`.
    """
    path = tmp_path / "af.sqlite3"
    path.touch()

    with SqliteRunRepository(path):
        pass

    assert read(path, "PRAGMA user_version") == SCHEMA_VERSION
    assert read(path, "SELECT count(*) FROM runs") == 0


def test_a_file_that_is_not_a_database_is_refused_by_name(tmp_path: Path) -> None:
    # The bare sqlite3 error says "file is not a database" and names nothing,
    # which is unhelpful when the path came from --db.
    path = tmp_path / "af.sqlite3"
    path.write_bytes(b"this is not a database" * 64)

    with pytest.raises(RuntimeError, match="not a SQLite database"):
        SqliteRunRepository(path)


def test_an_older_database_is_switched_to_wal(tmp_path: Path) -> None:
    """Low value on its own, kept to prove the pragma runs on the used path.

    No observed defect: a v1 file was always created by this code, which always
    set WAL. It exists because the pragma moved from the create-only branch to
    every open, and a line that only ever runs in tests that never assert on it
    is a line nobody notices has stopped working.
    """
    path = tmp_path / "af.sqlite3"
    build_v1(path, journal_mode="delete")
    assert read(path, "PRAGMA journal_mode") == "delete"

    with SqliteRunRepository(path):
        pass

    assert read(path, "PRAGMA journal_mode") == "wal"
