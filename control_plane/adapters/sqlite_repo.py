"""SQLite persistence for evaluation runs.

Every measurement is stored as an INTEGER. Tables are STRICT, which rejects a
float like 1.5 outright -- though note it silently coerces an integral float
like 2.0, so STRICT is one layer of three: the schema, a test asserting no
column is declared REAL, and the row builder below refusing a non-int.

State advances by compare-and-swap rather than a bare UPDATE, because an UPDATE
that matches no rows is *success* in SQLite. The rowcount check is what makes
the guard real.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, suppress
from pathlib import Path
from types import MappingProxyType

from control_plane.domain.ids import RunId, TaskId
from control_plane.domain.model import MicroUsd, ModelInvocation, ModelUsage
from control_plane.domain.outcomes import AgentOutcome, FailureCategory
from control_plane.domain.record import RunRecord
from control_plane.domain.scoring import Score
from control_plane.domain.states import EvaluationRun, HarnessFailure, RunState
from control_plane.domain.task import Suite
from control_plane.domain.validation import (
    CommandOutcome,
    CommandResult,
    ExitReason,
    StreamCapture,
    ValidationReport,
)
from control_plane.services.errors import ConcurrentModification, RunAlreadyExists

SCHEMA_VERSION = 6

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


_TABLES = ("runs", "validation_reports", "command_results", "model_invocations")

# SQLite's extended result code for "a row with this key already exists". The
# message text says "UNIQUE constraint failed" even for a primary key, so the
# code is matched rather than the text.
_DUPLICATE_KEY_ERRORS = frozenset({"SQLITE_CONSTRAINT_PRIMARYKEY"})


_CREATE_SQL = (
    "INSERT INTO runs "
    "(run_id, task_id, suite, state, created_at_unix_ms, "
    " updated_at_unix_ms, fixture_sha256, runner_id, "
    " runner_version, arch, pid1, workspace_path, "
    " agent_id, provider_id, model_id, prompt_id) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_ADVANCE_SQL = (
    "UPDATE runs SET state = ?, updated_at_unix_ms = ?, "
    "failure_kind = ?, failure_from = ?, failure_detail = ? "
    "WHERE run_id = ? AND state = ?"
)

_FINISH_SQL = (
    "UPDATE runs SET state = ?, updated_at_unix_ms = ?, "
    "failure_kind = ?, failure_from = ?, failure_detail = ?, "
    "runner_version = ?, arch = ?, pid1 = ?, "
    "outcome = ?, resolved = ?, regressed = ?, f2p_passed = ?, "
    "f2p_total = ?, p2p_passed = ?, p2p_total = ?, "
    "timed_out_commands = ?, task_defect = ?, patch_sha256 = ?, "
    "patch_bytes = ?, patch_is_empty = ?, total_cost_micros = ?, "
    "agent_ms = ?, validation_ms = ?, workspace_path = ?, "
    "image_digest = ?, agent_note = ? "
    "WHERE run_id = ? AND state = ?"
)
"""The three statements that write `runs`, lifted out of their methods.

Named so a test can read the column names back out of the SQL. Three bugs have
now been a column missing from one of these lists while the value that belongs
in it was computed correctly -- `arch` and `pid1`, `workspace_path` before
them. A comment asking the next author to keep the lists in step is what was
there when each of those shipped.
"""


_FIELD_COLUMNS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        # One `RunRecord` field does not mean one column: `run` carries the
        # state machine and fans out to eight, `score` to seven.
        "run": frozenset(
            {
                "run_id",
                "task_id",
                "state",
                "created_at_unix_ms",
                "updated_at_unix_ms",
                "failure_kind",
                "failure_from",
                "failure_detail",
            }
        ),
        "suite": frozenset({"suite"}),
        "fixture_sha256": frozenset({"fixture_sha256"}),
        "runner_id": frozenset({"runner_id"}),
        "runner_version": frozenset({"runner_version"}),
        "arch": frozenset({"arch"}),
        "pid1": frozenset({"pid1"}),
        "score": frozenset(
            {
                "resolved",
                "regressed",
                "f2p_passed",
                "f2p_total",
                "p2p_passed",
                "p2p_total",
                "timed_out_commands",
            }
        ),
        "outcome": frozenset({"outcome"}),
        "task_defect": frozenset({"task_defect"}),
        "patch_sha256": frozenset({"patch_sha256"}),
        "patch_bytes": frozenset({"patch_bytes"}),
        "patch_is_empty": frozenset({"patch_is_empty"}),
        "agent_id": frozenset({"agent_id"}),
        "provider_id": frozenset({"provider_id"}),
        "model_id": frozenset({"model_id"}),
        "prompt_id": frozenset({"prompt_id"}),
        "total_cost_micros": frozenset({"total_cost_micros"}),
        "agent_ms": frozenset({"agent_ms"}),
        "validation_ms": frozenset({"validation_ms"}),
        "workspace_path": frozenset({"workspace_path"}),
        "image_digest": frozenset({"image_digest"}),
        "agent_note": frozenset({"agent_note"}),
    }
)
"""Which columns of `runs` each `RunRecord` field reaches.

Declared rather than derived, because the mapping is not one to one and a
generator able to express it would be this same table spelled as code that
emits SQL. Stating it once lets a test compare it against the record, against
the table and against the statements above -- which is the guard the three
bugs needed, and which no amount of care in the SQL itself provides.
"""

_CHILD_TABLE_FIELDS = frozenset({"report", "invocations"})
"""Fields stored in a table of their own rather than in a `runs` column."""

_CREATE_ONLY = frozenset(
    {
        "run_id",
        "task_id",
        "created_at_unix_ms",
        "suite",
        "fixture_sha256",
        "runner_id",
        "agent_id",
        "provider_id",
        "model_id",
        "prompt_id",
    }
)
"""Columns written once, at creation, and never revised.

Identity and configuration: what the run is and what was being measured. Every
other column describes what happened, so `finish()` must write it.
"""


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
)


def _flag(value: bool) -> int:
    return 1 if value else 0


def _measure(name: str, value: int | None) -> int | None:
    """Refuse a non-integer measurement.

    The third float guard. STRICT rejects 1.5 but silently coerces 2.0, and
    `isinstance(True, int)` is True in Python -- so a boolean written into a
    count would pass every other check.
    """
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    return value


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
                _CREATE_SQL,
                (
                    record.run.run_id.value,
                    record.run.task_id.value,
                    record.suite.value,
                    state.value,
                    _measure("created_at", record.run.created_at_unix_ms),
                    _measure("updated_at", record.run.updated_at_unix_ms),
                    record.fixture_sha256,
                    record.runner_id,
                    record.runner_version,
                    record.arch,
                    _flag(record.pid1),
                    # Recorded at creation, not at finish. The row must name
                    # the directory before the run can die leaving one
                    # behind, or the recovery scan has nothing to delete.
                    record.workspace_path,
                    # Also at creation. A run that dies before the agent is
                    # reached is exactly the run a reliability figure is
                    # about, and it must still say which configuration was
                    # being measured when it died.
                    record.agent_id,
                    record.provider_id,
                    record.model_id,
                    record.prompt_id,
                ),
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
                _ADVANCE_SQL,
                (
                    run.state.value,
                    _measure("updated_at", run.updated_at_unix_ms),
                    run.failure.kind.value if run.failure else None,
                    run.failure.failed_from.value if run.failure else None,
                    run.failure.detail if run.failure else "",
                    run.run_id.value,
                    expected.value,
                ),
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
            _FINISH_SQL,
            (
                record.run.state.value,
                _measure("updated_at", record.run.updated_at_unix_ms),
                record.run.failure.kind.value if record.run.failure else None,
                record.run.failure.failed_from.value if record.run.failure else None,
                record.run.failure.detail if record.run.failure else "",
                record.runner_version,
                record.arch,
                _flag(record.pid1),
                record.outcome.value if record.outcome else None,
                _flag(record.score.resolved) if record.score else None,
                _flag(record.score.regressed) if record.score else None,
                record.score.fail_to_pass_passed if record.score else None,
                record.score.fail_to_pass_total if record.score else None,
                record.score.pass_to_pass_passed if record.score else None,
                record.score.pass_to_pass_total if record.score else None,
                record.score.timed_out_commands if record.score else None,
                _flag(record.task_defect),
                record.patch_sha256,
                _measure("patch_bytes", record.patch_bytes),
                None if record.patch_is_empty is None else _flag(record.patch_is_empty),
                _measure("total_cost_micros", int(record.total_cost_micros)),
                _measure("agent_ms", record.agent_ms),
                _measure("validation_ms", record.validation_ms),
                record.workspace_path,
                record.image_digest,
                record.agent_note,
                record.run.run_id.value,
                expected.value,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrentModification(
                f"{record.run.run_id} was not in state {expected.value}"
            )

        if record.report is not None:
            self._write_report(record.run.run_id, record.report)
        for seq, invocation in enumerate(record.invocations):
            self._write_invocation(record.run.run_id, seq, invocation)

    def _write_report(self, run_id: RunId, report: ValidationReport) -> None:
        self._conn.execute(
            "INSERT INTO validation_reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id.value,
                report.schema_version,
                report.runner_version,
                report.arch,
                _flag(report.pid1),
                report.shell,
                report.workdir,
                _measure("started_at", report.started_at_unix_ms),
                _measure("wall_ms", report.wall_ms),
                report.exit_reason.value,
                _flag(report.aborted),
                _measure("orphans", report.orphans_reaped_total),
            ),
        )
        for result in report.commands:
            self._conn.execute(
                "INSERT INTO command_results VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id.value,
                    result.index,
                    result.command,
                    _measure("started_at", result.started_at_unix_ms),
                    _measure("duration_ms", result.duration_ms),
                    result.outcome.value,
                    result.exit_code,
                    result.signal,
                    _flag(result.timed_out),
                    _flag(result.deadline_signal_sent),
                    _flag(result.killed_after_grace),
                    _flag(result.group_escaped),
                    _flag(result.drain_deadline_hit),
                    _measure("orphans", result.orphans_reaped),
                    result.spawn_error,
                    _measure("stdout_total", result.stdout.bytes_total),
                    _measure("stdout_written", result.stdout.bytes_written),
                    _flag(result.stdout.truncated),
                    result.stdout.path,
                    result.stdout.tail,
                    _measure("stderr_total", result.stderr.bytes_total),
                    _measure("stderr_written", result.stderr.bytes_written),
                    _flag(result.stderr.truncated),
                    result.stderr.path,
                    result.stderr.tail,
                ),
            )

    def _write_invocation(
        self, run_id: RunId, seq: int, invocation: ModelInvocation
    ) -> None:
        self._conn.execute(
            "INSERT INTO model_invocations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id.value,
                seq,
                invocation.provider_id,
                invocation.model_id,
                _measure("input_tokens", invocation.usage.input_tokens),
                _measure("output_tokens", invocation.usage.output_tokens),
                _measure("cache_write", invocation.usage.cache_write_tokens),
                _measure("cache_read", invocation.usage.cache_read_tokens),
                _measure("cost", int(invocation.cost)),
                _measure("latency_ms", invocation.latency_ms),
            ),
        )

    # --- reads --------------------------------------------------------------

    def get(self, run_id: RunId) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id.value,)
        ).fetchone()
        return None if row is None else self._hydrate(row)

    def list_runs(self, limit: int | None = 50) -> tuple[RunRecord, ...]:
        """The most recent runs, newest first. None means every one of them.

        A report needs all of them, and a default limit silently truncating it
        would produce a table that looks complete and is not -- the denominator
        shrinking with no indication that it had.
        """
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY created_at_unix_ms DESC, run_id DESC LIMIT ?",
            # SQLite reads a negative LIMIT as no limit, which keeps this one
            # statement rather than branching into two that could drift.
            (-1 if limit is None else limit,),
        ).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def list_unfinished(self) -> tuple[RunRecord, ...]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE state NOT IN ('scored','failed','aborted') "
            "ORDER BY created_at_unix_ms"
        ).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def _hydrate(self, row: sqlite3.Row) -> RunRecord:
        """Rebuild a record through the domain's own constructors.

        Every __post_init__ runs here, so the read path is the validation path
        and a row that violates an invariant fails loudly rather than becoming
        a plausible wrong number.
        """
        state = RunState(row["state"])
        failure = (
            HarnessFailure(
                kind=FailureCategory(row["failure_kind"]),
                failed_from=RunState(row["failure_from"]),
                detail=row["failure_detail"],
            )
            if row["failure_kind"]
            else None
        )
        run = EvaluationRun(
            run_id=RunId(row["run_id"]),
            task_id=TaskId(row["task_id"]),
            state=state,
            created_at_unix_ms=row["created_at_unix_ms"],
            updated_at_unix_ms=row["updated_at_unix_ms"],
            failure=failure,
        )
        score = (
            Score(
                resolved=bool(row["resolved"]),
                regressed=bool(row["regressed"]),
                fail_to_pass_passed=row["f2p_passed"],
                fail_to_pass_total=row["f2p_total"],
                pass_to_pass_passed=row["p2p_passed"],
                pass_to_pass_total=row["p2p_total"],
                timed_out_commands=row["timed_out_commands"],
            )
            if row["resolved"] is not None
            else None
        )
        return RunRecord(
            run=run,
            suite=Suite(row["suite"]),
            fixture_sha256=row["fixture_sha256"],
            runner_id=row["runner_id"],
            runner_version=row["runner_version"],
            arch=row["arch"],
            pid1=bool(row["pid1"]),
            report=self._read_report(run.run_id),
            score=score,
            outcome=AgentOutcome(row["outcome"]) if row["outcome"] else None,
            task_defect=bool(row["task_defect"]),
            patch_sha256=row["patch_sha256"],
            patch_bytes=row["patch_bytes"],
            patch_is_empty=(
                None if row["patch_is_empty"] is None else bool(row["patch_is_empty"])
            ),
            invocations=self._read_invocations(run.run_id),
            total_cost_micros=MicroUsd(row["total_cost_micros"]),
            agent_ms=row["agent_ms"],
            validation_ms=row["validation_ms"],
            workspace_path=row["workspace_path"],
            image_digest=row["image_digest"],
            agent_note=row["agent_note"],
            agent_id=row["agent_id"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            prompt_id=row["prompt_id"],
        )

    def _read_report(self, run_id: RunId) -> ValidationReport | None:
        header = self._conn.execute(
            "SELECT * FROM validation_reports WHERE run_id = ?", (run_id.value,)
        ).fetchone()
        if header is None:
            return None
        rows = self._conn.execute(
            "SELECT * FROM command_results WHERE run_id = ? ORDER BY idx",
            (run_id.value,),
        ).fetchall()
        return ValidationReport(
            schema_version=header["schema_version"],
            runner_version=header["runner_version"],
            arch=header["arch"],
            pid1=bool(header["pid1"]),
            shell=header["shell"],
            workdir=header["workdir"],
            started_at_unix_ms=header["started_at_unix_ms"],
            wall_ms=header["wall_ms"],
            exit_reason=ExitReason(header["exit_reason"]),
            aborted=bool(header["aborted"]),
            orphans_reaped_total=header["orphans_reaped_total"],
            commands=tuple(self._hydrate_command(row) for row in rows),
        )

    def _hydrate_command(self, row: sqlite3.Row) -> CommandResult:
        def stream(prefix: str) -> StreamCapture:
            return StreamCapture(
                bytes_total=row[f"{prefix}_bytes_total"],
                bytes_written=row[f"{prefix}_bytes_written"],
                truncated=bool(row[f"{prefix}_truncated"]),
                path=row[f"{prefix}_path"],
                tail=row[f"{prefix}_tail"],
            )

        return CommandResult(
            index=row["idx"],
            command=row["command"],
            started_at_unix_ms=row["started_at_unix_ms"],
            duration_ms=row["duration_ms"],
            outcome=CommandOutcome(row["outcome"]),
            stdout=stream("stdout"),
            stderr=stream("stderr"),
            exit_code=row["exit_code"],
            signal=row["signal"],
            timed_out=bool(row["timed_out"]),
            deadline_signal_sent=bool(row["deadline_signal_sent"]),
            killed_after_grace=bool(row["killed_after_grace"]),
            group_escaped=bool(row["group_escaped"]),
            drain_deadline_hit=bool(row["drain_deadline_hit"]),
            orphans_reaped=row["orphans_reaped"],
            spawn_error=row["spawn_error"],
        )

    def _read_invocations(self, run_id: RunId) -> tuple[ModelInvocation, ...]:
        rows = self._conn.execute(
            "SELECT * FROM model_invocations WHERE run_id = ? ORDER BY seq",
            (run_id.value,),
        ).fetchall()
        return tuple(
            ModelInvocation(
                provider_id=row["provider_id"],
                model_id=row["model_id"],
                usage=ModelUsage(
                    input_tokens=row["input_tokens"],
                    output_tokens=row["output_tokens"],
                    cache_write_tokens=row["cache_write_tokens"],
                    cache_read_tokens=row["cache_read_tokens"],
                ),
                cost=MicroUsd(row["cost_micros"]),
                latency_ms=row["latency_ms"],
            )
            for row in rows
        )

    def column_types(self) -> Iterator[tuple[str, str, str]]:
        """(table, column, declared type) for every column. Used by a test."""
        for table in _TABLES:
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
            for table in _TABLES
        }
