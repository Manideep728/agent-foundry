"""PostgreSQL persistence for evaluation runs (D33).

The same rows as the SQLite repository, written by the same statements and
read back by the same code in `run_rows`. What differs is what a server
database needs that a file does not:

- Integers are BIGINT. Postgres INTEGER is 32 bits, and a millisecond
  timestamp is not. Flags are SMALLINT 0 or 1, which keeps them the values
  `run_rows` writes for either database.
- The schema version lives in a table, because there is no file header to
  hold it. Setup runs under a transaction-scoped advisory lock, so two
  processes opening a fresh database cannot both create it.
- The run lock (D17) is a session advisory lock rather than `flock`, because
  the writers may be on different machines.

Every write is one explicit transaction. The connection is otherwise in
autocommit, so a read never holds a transaction open between commands.
"""

from __future__ import annotations

import re

import psycopg
from psycopg import errors
from psycopg.rows import DictRow, dict_row

from control_plane.adapters import run_rows as rows
from control_plane.domain.ids import RunId
from control_plane.domain.model import ModelInvocation
from control_plane.domain.record import RunRecord
from control_plane.domain.states import EvaluationRun, RunState
from control_plane.domain.validation import ValidationReport
from control_plane.services.errors import ConcurrentModification, RunAlreadyExists

SCHEMA_VERSION = 1
"""This store's own counter, independent of SQLite's. Version 1 is SQLite's
version 7: the Postgres store began with everything SQLite had learned."""

_SCHEMA = (
    """
CREATE TABLE runs (
  run_id             TEXT PRIMARY KEY,
  task_id            TEXT NOT NULL,
  suite              TEXT NOT NULL,
  state              TEXT NOT NULL,
  created_at_unix_ms BIGINT NOT NULL,
  updated_at_unix_ms BIGINT NOT NULL,
  failure_kind       TEXT,
  failure_from       TEXT,
  failure_detail     TEXT NOT NULL DEFAULT '',
  fixture_sha256     TEXT NOT NULL,
  runner_id          TEXT NOT NULL,
  runner_version     TEXT NOT NULL,
  arch               TEXT NOT NULL,
  pid1               SMALLINT NOT NULL,
  outcome            TEXT,
  resolved           SMALLINT,
  regressed          SMALLINT,
  f2p_passed         BIGINT,
  f2p_total          BIGINT,
  p2p_passed         BIGINT,
  p2p_total          BIGINT,
  timed_out_commands BIGINT,
  task_defect        SMALLINT NOT NULL DEFAULT 0,
  patch_sha256       TEXT,
  patch_bytes        BIGINT NOT NULL DEFAULT 0,
  patch_is_empty     SMALLINT,
  total_cost_micros  BIGINT NOT NULL DEFAULT 0,
  agent_ms           BIGINT,
  validation_ms      BIGINT,
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
)
""",
    """
CREATE TABLE validation_reports (
  run_id               TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
  schema_version       BIGINT NOT NULL,
  runner_version       TEXT NOT NULL,
  arch                 TEXT NOT NULL,
  pid1                 SMALLINT NOT NULL,
  shell                TEXT NOT NULL,
  workdir              TEXT NOT NULL,
  started_at_unix_ms   BIGINT NOT NULL,
  wall_ms              BIGINT NOT NULL,
  exit_reason          TEXT NOT NULL,
  aborted              SMALLINT NOT NULL,
  orphans_reaped_total BIGINT NOT NULL
)
""",
    """
CREATE TABLE command_results (
  run_id               TEXT NOT NULL
      REFERENCES validation_reports(run_id) ON DELETE CASCADE,
  idx                  BIGINT NOT NULL,
  command              TEXT NOT NULL,
  started_at_unix_ms   BIGINT NOT NULL,
  duration_ms          BIGINT NOT NULL,
  outcome              TEXT NOT NULL,
  exit_code            BIGINT,
  signal               BIGINT,
  timed_out            SMALLINT NOT NULL,
  deadline_signal_sent SMALLINT NOT NULL,
  killed_after_grace   SMALLINT NOT NULL,
  group_escaped        SMALLINT NOT NULL,
  drain_deadline_hit   SMALLINT NOT NULL,
  orphans_reaped       BIGINT NOT NULL,
  spawn_error          TEXT,
  stdout_bytes_total   BIGINT NOT NULL,
  stdout_bytes_written BIGINT NOT NULL,
  stdout_truncated     SMALLINT NOT NULL,
  stdout_path          TEXT NOT NULL,
  stdout_tail          TEXT NOT NULL,
  stderr_bytes_total   BIGINT NOT NULL,
  stderr_bytes_written BIGINT NOT NULL,
  stderr_truncated     SMALLINT NOT NULL,
  stderr_path          TEXT NOT NULL,
  stderr_tail          TEXT NOT NULL,
  PRIMARY KEY (run_id, idx)
)
""",
    """
CREATE TABLE model_invocations (
  run_id             TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq                BIGINT NOT NULL,
  provider_id        TEXT NOT NULL,
  model_id           TEXT NOT NULL,
  input_tokens       BIGINT NOT NULL,
  output_tokens      BIGINT NOT NULL,
  cache_write_tokens BIGINT NOT NULL,
  cache_read_tokens  BIGINT NOT NULL,
  cost_micros        BIGINT NOT NULL,
  latency_ms         BIGINT NOT NULL,
  PRIMARY KEY (run_id, seq)
)
""",
)

_SCHEMA_LOCK = 0x6166_7363_6865_6D61
"""Advisory lock key for schema setup ("afschema" in ASCII)."""

RUN_LOCK = 0x6166_7275_6E6C_6F63
"""Advisory lock key for the run lock ("afrunloc" in ASCII)."""


def _pyformat(sql: str) -> str:
    """The shared `:name` placeholders, spelled the way psycopg reads them.

    Checked rather than assumed: a `%` would be read by psycopg as the start
    of a placeholder, and a `::` cast would be half rewritten.
    """
    if "%" in sql or "::" in sql:
        raise ValueError(f"cannot rewrite placeholders safely in: {sql}")
    return re.sub(r":(\w+)", r"%(\1)s", sql)


_CREATE = _pyformat(rows.CREATE_SQL)
_ADVANCE = _pyformat(rows.ADVANCE_SQL)
_FINISH = _pyformat(rows.FINISH_SQL)
_REPORT = _pyformat(rows.REPORT_SQL)
_COMMAND = _pyformat(rows.COMMAND_SQL)
_INVOCATION = _pyformat(rows.INVOCATION_SQL)
_GET = _pyformat(rows.GET_SQL)
_LIST = _pyformat(rows.LIST_SQL)
_LIST_ALL = _pyformat(rows.LIST_ALL_SQL)
_UNFINISHED = _pyformat(rows.UNFINISHED_SQL)
_REPORT_HEADER = _pyformat(rows.REPORT_HEADER_SQL)
_COMMANDS = _pyformat(rows.COMMANDS_SQL)
_INVOCATIONS = _pyformat(rows.INVOCATIONS_SQL)


class PostgresRunRepository:
    """Stores runs in one PostgreSQL database.

    The connection string is libpq's, so a password can come from
    `PGPASSWORD` or a `.pgpass` file and never appear in a command line.
    """

    def __init__(self, url: str) -> None:
        try:
            self._conn: psycopg.Connection[DictRow] = psycopg.connect(
                url, autocommit=True, row_factory=dict_row
            )
        except psycopg.OperationalError as error:
            raise RuntimeError(f"could not connect to PostgreSQL: {error}") from error
        try:
            self._prepare()
        except BaseException:
            self._conn.close()
            raise

    def _prepare(self) -> None:
        """Create the schema, or refuse a newer one, atomically.

        Postgres runs DDL inside a transaction, so a failure here leaves no
        half-created schema behind, and the advisory lock makes a second
        process wait until the first has finished rather than race it.
        """
        with self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK,))
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version BIGINT NOT NULL)"
            )
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                for statement in _SCHEMA:
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (%s)",
                    (SCHEMA_VERSION,),
                )
            elif row["version"] > SCHEMA_VERSION:
                # Refused for the reason the SQLite store refuses one: a column
                # may mean something different now, and guessing yields
                # plausible wrong data.
                raise RuntimeError(
                    f"this database was written by a newer build (schema "
                    f"{row['version']}); this one understands {SCHEMA_VERSION}"
                )

    def try_hold_run_lock(self) -> bool:
        """Take the run lock for as long as this connection lives, if it is free.

        Session-scoped, so the server releases it when the connection ends for
        any reason, including a killed process -- the property D17 chose
        `flock` for.
        """
        row = self._conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS held", (RUN_LOCK,)
        ).fetchone()
        return row is not None and bool(row["held"])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> PostgresRunRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- writes -------------------------------------------------------------

    def create(self, record: RunRecord) -> None:
        with self._conn.transaction():
            self._insert(record, record.run.state)

    def store(self, record: RunRecord) -> None:
        """Write a finished run whole, in one transaction, as SQLite does."""
        if not record.run.is_terminal:
            raise ValueError(
                f"{record.run.run_id} is {record.run.state.value}; only a "
                f"finished run can be stored whole"
            )
        with self._conn.transaction():
            self._insert(record, RunState.CREATED)
            self._finalise(record, expected=RunState.CREATED)

    def _insert(self, record: RunRecord, state: RunState) -> None:
        try:
            self._conn.execute(
                _CREATE, {**rows.run_columns(record), "state": state.value}
            )
        except errors.UniqueViolation as error:
            # The primary key only. Another unique constraint failing is a row
            # this code built wrongly, not a run that already exists.
            if error.diag.constraint_name != "runs_pkey":
                raise
            raise RunAlreadyExists(
                f"{record.run.run_id} is already recorded"
            ) from error

    def advance(self, run: EvaluationRun, expected: RunState) -> None:
        with self._conn.transaction():
            cursor = self._conn.execute(_ADVANCE, rows.advance_columns(run, expected))
        if cursor.rowcount != 1:
            raise ConcurrentModification(
                f"{run.run_id} was not in state {expected.value} when the write "
                f"to {run.state.value} was attempted"
            )

    def finish(self, record: RunRecord, expected: RunState) -> None:
        with self._conn.transaction():
            self._finalise(record, expected)

    def _finalise(self, record: RunRecord, expected: RunState) -> None:
        cursor = self._conn.execute(
            _FINISH, {**rows.run_columns(record), "expected_state": expected.value}
        )
        if cursor.rowcount != 1:
            raise ConcurrentModification(
                f"{record.run.run_id} was not in state {expected.value}"
            )
        run_id = record.run.run_id
        if record.report is not None:
            self._conn.execute(_REPORT, rows.report_columns(run_id, record.report))
            for result in record.report.commands:
                self._conn.execute(_COMMAND, rows.command_columns(run_id, result))
        for seq, invocation in enumerate(record.invocations):
            self._conn.execute(
                _INVOCATION, rows.invocation_columns(run_id, seq, invocation)
            )

    # --- reads --------------------------------------------------------------

    def get(self, run_id: RunId) -> RunRecord | None:
        row = self._conn.execute(_GET, {"run_id": run_id.value}).fetchone()
        return None if row is None else self._hydrate(row)

    def list_runs(self, limit: int | None = 50) -> tuple[RunRecord, ...]:
        if limit is None:
            found = self._conn.execute(_LIST_ALL).fetchall()
        else:
            found = self._conn.execute(_LIST, {"limit": limit}).fetchall()
        return tuple(self._hydrate(row) for row in found)

    def list_unfinished(self) -> tuple[RunRecord, ...]:
        return tuple(
            self._hydrate(row) for row in self._conn.execute(_UNFINISHED).fetchall()
        )

    def _hydrate(self, row: DictRow) -> RunRecord:
        run_id = RunId(row["run_id"])
        return rows.hydrate_run(
            row, self._read_report(run_id), self._read_invocations(run_id)
        )

    def _read_report(self, run_id: RunId) -> ValidationReport | None:
        key = {"run_id": run_id.value}
        header = self._conn.execute(_REPORT_HEADER, key).fetchone()
        if header is None:
            return None
        return rows.hydrate_report(
            header, self._conn.execute(_COMMANDS, key).fetchall()
        )

    def _read_invocations(self, run_id: RunId) -> tuple[ModelInvocation, ...]:
        found = self._conn.execute(_INVOCATIONS, {"run_id": run_id.value}).fetchall()
        return tuple(rows.hydrate_invocation(row) for row in found)

    def column_types(self) -> list[tuple[str, str, str]]:
        """(table, column, data type) for every column. Used by a test."""
        found = self._conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() ORDER BY table_name, column_name"
        ).fetchall()
        return [(r["table_name"], r["column_name"], r["data_type"]) for r in found]
