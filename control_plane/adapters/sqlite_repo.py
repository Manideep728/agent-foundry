"""SQLite persistence for evaluation runs.

Every measurement is stored as an INTEGER. Tables are STRICT, which rejects a
float like 1.5 outright -- though note it silently coerces an integral float
like 2.0, so STRICT is one layer of three: the schema, a test asserting no
column is declared REAL, and the row builders in `run_rows` refusing a non-int.

What is stored, and how a row becomes a record, is shared with the Postgres
repository in `run_rows`. This module owns the file: the schema, its upgrades,
WAL, and SQLite's own transaction rules.

State advances by compare-and-swap rather than a bare UPDATE, because an UPDATE
that matches no rows is *success* in SQLite. The rowcount check is what makes
the guard real.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, suppress
from pathlib import Path

from control_plane.adapters import run_rows as rows
from control_plane.domain.ids import RunId
from control_plane.domain.model import ModelInvocation
from control_plane.domain.record import RunRecord
from control_plane.domain.states import EvaluationRun, RunState
from control_plane.domain.validation import ValidationReport
from control_plane.services.errors import ConcurrentModification, RunAlreadyExists

SCHEMA_VERSION = 7

_SCHEMA = (
    """
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
  image_digest       TEXT,
  agent_note         TEXT,
  agent_id           TEXT,
  provider_id        TEXT,
  model_id           TEXT,
  prompt_id          TEXT,
  harness_version    TEXT,
  CHECK ((state = 'failed') = (failure_kind IS NOT NULL)),
  CHECK (updated_at_unix_ms >= created_at_unix_ms),
  CHECK (pid1 IN (0, 1))
) STRICT;
""",
    """
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
""",
    """
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
""",
    """
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
""",
)


# SQLite's extended result code for "a row with this key already exists". The
# message text says "UNIQUE constraint failed" even for a primary key, so the
# code is matched rather than the text.
_DUPLICATE_KEY_ERRORS = frozenset({"SQLITE_CONSTRAINT_PRIMARYKEY"})


# Ordered upgrade steps, each naming the version it produces. A step may add a
# column, or repair data from a table that already holds the truth.
#
# A step may NOT add a table-level CHECK: SQLite's ALTER TABLE cannot express
# one, so `_SCHEMA` and this list would go on to describe different databases
# depending on whether a file was created or upgraded. That rule is enforced by
# test_a_migrated_database_matches_a_fresh_one, not by this comment.
_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        2,
        (
            # Repair for the environment columns. `create()` writes them before
            # any container exists -- arch 'unknown', pid1 0 -- and `finish()`
            # did not rewrite them until `351cdd6`, so every run stored before
            # that kept the placeholder. validation_reports has held the true
            # values the whole time, so this copies from an authority rather
            # than guessing. Runs that never reached validation have no report
            # and are left alone: 'unknown' is the honest answer for them.
            "UPDATE runs SET "
            "  arch = (SELECT v.arch FROM validation_reports v "
            "          WHERE v.run_id = runs.run_id), "
            "  pid1 = (SELECT v.pid1 FROM validation_reports v "
            "          WHERE v.run_id = runs.run_id) "
            "WHERE run_id IN (SELECT run_id FROM validation_reports)",
        ),
    ),
    (
        3,
        (
            # Nullable, which is also why this is one line. ALTER TABLE on a
            # STRICT table may only add NOT NULL with a non-null default, and
            # every candidate default here is a lie: the local runner has no
            # image, and a run that never reached validation used none. NULL
            # says exactly that, and cannot be mistaken for a digest.
            "ALTER TABLE runs ADD COLUMN image_digest TEXT",
        ),
    ),
    (
        4,
        (
            # Why a reply could not be read, in the agent's own words.
            # Nullable too: almost every run has nothing to say here, and
            # "" would not be distinguishable from a reason nobody wrote.
            "ALTER TABLE runs ADD COLUMN agent_note TEXT",
        ),
    ),
    (
        5,
        (
            # Which agent, provider and model the run was configured to use.
            # Nullable for the usual reason: every value that could be
            # backfilled here would be a guess. The rows already stored were
            # produced by several configurations and the database never wrote
            # down which, so NULL is the only true answer and the summary
            # groups those runs apart rather than folding them into one.
            "ALTER TABLE runs ADD COLUMN agent_id TEXT",
            "ALTER TABLE runs ADD COLUMN provider_id TEXT",
            "ALTER TABLE runs ADD COLUMN model_id TEXT",
        ),
    ),
    (
        6,
        (
            # The prompt a run was given. Recorded because it decides the
            # measurement: one model under two prompts writes nothing and then
            # writes patches. Backfilled to NULL on purpose -- a run stored
            # before this column cannot be said to have used `tools-v1`, only
            # to have used whatever the text was on that day.
            "ALTER TABLE runs ADD COLUMN prompt_id TEXT",
        ),
    ),
    (
        7,
        (
            # The build that measured a run. NULL for every stored row: the
            # commit that produced them was never written down, and the git
            # history cannot recover it, because a run's timestamp says when it
            # ran and not which checkout it ran from.
            "ALTER TABLE runs ADD COLUMN harness_version TEXT",
        ),
    ),
)


class SqliteRunRepository:
    """Stores runs in one SQLite file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        # Foreign keys are per-connection, not stored in the file.
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        try:
            # Unconditional, where this used to run only when creating the
            # file. WAL persists in the file, so setting it at creation was
            # correct for a database this code made and silently skipped for
            # one it did not -- such as the empty file a crashed create leaves.
            # Re-setting costs one pragma and removes an invisible dependency
            # on who made the file. It cannot run inside a transaction, so it
            # happens before _prepare opens one.
            #
            # It is also the first statement to read page 1, so it is where a
            # file that is not a database announces itself. That wrap covers
            # this line only: a failure inside _prepare is a broken migration,
            # and reporting that as "not a SQLite database" would send the
            # reader to check the wrong thing entirely.
            self._conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError as error:
            # Without this the caller gets a bare "file is not a database"
            # naming nothing, and the connection leaks on the way out.
            self._conn.close()
            raise RuntimeError(f"{path} is not a SQLite database: {error}") from error

        try:
            self._prepare()
        except BaseException:
            self._conn.close()
            raise

    def _prepare(self) -> None:
        """Create the schema, or upgrade it, atomically and without racing.

        IMMEDIATE takes the write lock *before* the version is read, so two
        processes opening the same file cannot both read version 1 and both
        decide to migrate. `af show` and `af list` open the repository without
        the run lock D17 describes, so that is reachable rather than
        theoretical.

        The explicit BEGIN is required rather than stylistic. Python's sqlite3
        opens an implicit transaction only before DML, so DDL under
        `with self._conn` runs in autocommit and a rollback would undo nothing
        -- leaving a database carrying half an upgrade and a version number
        claiming all of it.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                # Refused rather than best-effort read, for the same reason a
                # newer ValidationReport schema is refused: a field may mean
                # something different now, and guessing yields plausible wrong
                # data.
                raise RuntimeError(
                    f"{self._path} was written by a newer build (schema {version}); "
                    f"this one understands {SCHEMA_VERSION}"
                )
            if version == 0 and not self._has_tables():
                self._create()
            else:
                self._migrate(version)
            self._conn.execute("COMMIT")
        except BaseException:
            # A rollback that fails must not replace the real error.
            with suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            raise

    def _has_tables(self) -> bool:
        """Whether this database has a schema yet.

        Freshness is a property of the database, not of the file. A zero-byte
        file is a perfectly valid empty SQLite database, and testing
        `path.exists()` called it old and left it schemaless -- so the first
        query died with `no such table: runs`, blaming the query.
        """
        row = self._conn.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()
        return int(row[0]) > 0

    def _create(self) -> None:
        for statement in _SCHEMA:
            self._conn.execute(statement)
        # A PRAGMA takes no bound parameters, hence the f-string. The value is
        # a module constant int and never reaches here from input.
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate(self, version: int) -> None:
        for target, statements in _MIGRATIONS:
            if target <= version:
                continue
            for statement in statements:
                self._conn.execute(statement)
            # Bumped per step, inside the transaction that ran the step.
            # user_version lives in the file header and is journalled like any
            # other page, so a crash mid-upgrade rolls the version back with
            # the DDL and never leaves a database claiming a schema it lacks.
            self._conn.execute(f"PRAGMA user_version = {target}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteRunRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- writes -------------------------------------------------------------

    def create(self, record: RunRecord) -> None:
        with self._conn:
            self._insert(record, record.run.state)

    def store(self, record: RunRecord) -> None:
        """Write a finished run in one transaction: the row, the report, the costs.

        For a run measured somewhere else and received whole, as the collector
        receives one. `create` then `finish` would be two transactions, and a
        crash between them would leave a terminal row with no report -- a
        measurement that reads as present and holds nothing.

        A run that is not terminal is refused. Stored here, the recovery scan
        would take it for an abandoned run of this machine and delete its
        workspace path, which names a directory on another machine.
        """
        if not record.run.is_terminal:
            raise ValueError(
                f"{record.run.run_id} is {record.run.state.value}; only a "
                f"finished run can be stored whole"
            )
        # Inserted as CREATED and finished inside the same transaction, which
        # is the lifecycle `create` and `finish` already follow. Inserting the
        # terminal state directly is refused by the schema for a failed run:
        # its failure columns are written only by the finish, and the table
        # requires a failed row to carry them.
        with self._conn:
            self._insert(record, RunState.CREATED)
            self._finalise(record, expected=RunState.CREATED)

    def _insert(self, record: RunRecord, state: RunState) -> None:
        try:
            self._conn.execute(
                rows.CREATE_SQL, {**rows.run_columns(record), "state": state.value}
            )
        except sqlite3.IntegrityError as error:
            # Only a key violation means the run exists. A CHECK failure is a
            # row this code built wrongly, and naming it a duplicate sends the
            # diagnosis looking for a second copy that is not there.
            if error.sqlite_errorname not in _DUPLICATE_KEY_ERRORS:
                raise
            raise RunAlreadyExists(
                f"{record.run.run_id} is already recorded"
            ) from error

    def advance(self, run: EvaluationRun, expected: RunState) -> None:
        """Compare-and-swap the state, or raise."""
        with self._conn:
            cursor = self._conn.execute(
                rows.ADVANCE_SQL, rows.advance_columns(run, expected)
            )
        # An UPDATE that matches nothing is success in SQLite. Without this
        # check the compare-and-swap buys nothing at all.
        if cursor.rowcount != 1:
            raise ConcurrentModification(
                f"{run.run_id} was not in state {expected.value} when the write "
                f"to {run.state.value} was attempted"
            )

    def finish(self, record: RunRecord, expected: RunState) -> None:
        """Write the terminal state, the report and the costs in one transaction.

        One transaction, so a partial write cannot commit and a retry is free.

        This writes *every* environment column, not only the measurements.
        `create()` has to guess them -- it runs before a container exists, so
        arch is 'unknown' and pid1 is False -- and for as long as `finish()`
        left them alone, that guess was what every stored run kept. The rule is
        deliberately stated as "finish writes the environment", rather than
        "finish writes the two environment columns that are report-derived
        today", so that the next fact learned during a run cannot quietly
        reproduce the same bug.
        """
        with self._conn:
            self._finalise(record, expected)

    def _finalise(self, record: RunRecord, expected: RunState) -> None:
        cursor = self._conn.execute(
            rows.FINISH_SQL,
            {**rows.run_columns(record), "expected_state": expected.value},
        )
        if cursor.rowcount != 1:
            raise ConcurrentModification(
                f"{record.run.run_id} was not in state {expected.value}"
            )

        run_id = record.run.run_id
        if record.report is not None:
            self._write_report(run_id, record.report)
        for seq, invocation in enumerate(record.invocations):
            self._conn.execute(
                rows.INVOCATION_SQL, rows.invocation_columns(run_id, seq, invocation)
            )

    def _write_report(self, run_id: RunId, report: ValidationReport) -> None:
        self._conn.execute(rows.REPORT_SQL, rows.report_columns(run_id, report))
        for result in report.commands:
            self._conn.execute(rows.COMMAND_SQL, rows.command_columns(run_id, result))

    # --- reads --------------------------------------------------------------

    def get(self, run_id: RunId) -> RunRecord | None:
        row = self._conn.execute(rows.GET_SQL, {"run_id": run_id.value}).fetchone()
        return None if row is None else self._hydrate(row)

    def list_runs(self, limit: int | None = 50) -> tuple[RunRecord, ...]:
        """The most recent runs, newest first. None means every one of them.

        A report needs all of them, and a default limit silently truncating it
        would produce a table that looks complete and is not -- the denominator
        shrinking with no indication that it had.
        """
        if limit is None:
            found = self._conn.execute(rows.LIST_ALL_SQL).fetchall()
        else:
            found = self._conn.execute(rows.LIST_SQL, {"limit": limit}).fetchall()
        return tuple(self._hydrate(row) for row in found)

    def list_unfinished(self) -> tuple[RunRecord, ...]:
        found = self._conn.execute(rows.UNFINISHED_SQL).fetchall()
        return tuple(self._hydrate(row) for row in found)

    def _hydrate(self, row: sqlite3.Row) -> RunRecord:
        run_id = RunId(row["run_id"])
        return rows.hydrate_run(
            row, self._read_report(run_id), self._read_invocations(run_id)
        )

    def _read_report(self, run_id: RunId) -> ValidationReport | None:
        key = {"run_id": run_id.value}
        header = self._conn.execute(rows.REPORT_HEADER_SQL, key).fetchone()
        if header is None:
            return None
        return rows.hydrate_report(
            header, self._conn.execute(rows.COMMANDS_SQL, key).fetchall()
        )

    def _read_invocations(self, run_id: RunId) -> tuple[ModelInvocation, ...]:
        found = self._conn.execute(
            rows.INVOCATIONS_SQL, {"run_id": run_id.value}
        ).fetchall()
        return tuple(rows.hydrate_invocation(row) for row in found)

    def column_types(self) -> Iterator[tuple[str, str, str]]:
        """(table, column, declared type) for every column. Used by a test."""
        for table in rows.TABLES:
            with closing(self._conn.execute(f"PRAGMA table_info({table})")) as cursor:
                for row in cursor.fetchall():
                    yield table, row["name"], row["type"]

    def table_shapes(self) -> dict[str, dict[str, tuple[tuple[object, ...], ...]]]:
        """Every table's normalised structure. Used by a test.

        Deliberately not `sqlite_master.sql`. ALTER TABLE ADD COLUMN patches the
        *stored text* of the CREATE TABLE statement, inserting the new column
        before the closing paren -- which on `runs` puts it after the three
        table-level CHECKs, where a hand-written schema puts it among the
        columns. A migrated table's SQL text therefore never matches a fresh
        one even when the two behave identically, so comparing text would give
        a test that fails on day one for a cosmetic reason and is then deleted.

        These pragmas report the facts that behave: name, declared type, NOT
        NULL, default, primary-key rank, indexes and foreign keys. Known gap,
        stated rather than papered over: table_info does not expose CHECK
        constraints, which is exactly why `_MIGRATIONS` carries the rule that a
        step may not add one.
        """
        return {
            table: {
                pragma: tuple(
                    tuple(row)
                    for row in self._conn.execute(f"PRAGMA {pragma}({table})")
                )
                for pragma in ("table_info", "index_list", "foreign_key_list")
            }
            for table in rows.TABLES
        }
