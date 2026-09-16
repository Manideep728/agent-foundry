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
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

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

SCHEMA_VERSION = 1

_SCHEMA = """
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
        fresh = not path.exists()
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        # Foreign keys are per-connection, not stored in the file.
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if fresh:
            # WAL persists in the file, so it is set once at creation. A reader
            # then does not block the writer, and a crash mid-write leaves a
            # recoverable database rather than a corrupt one.
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
        self._check_version()

    def _check_version(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            # Refused rather than best-effort read, for the same reason a
            # newer ValidationReport schema is refused: a field may mean
            # something different now, and guessing yields plausible wrong data.
            raise RuntimeError(
                f"{self._path} was written by a newer build (schema {version}); "
                f"this one understands {SCHEMA_VERSION}"
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteRunRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- writes -------------------------------------------------------------

    def create(self, record: RunRecord) -> None:
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO runs "
                    "(run_id, task_id, suite, state, created_at_unix_ms, "
                    " updated_at_unix_ms, fixture_sha256, runner_id, "
                    " runner_version, arch, pid1, workspace_path) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.run.run_id.value,
                        record.run.task_id.value,
                        record.suite.value,
                        record.run.state.value,
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
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise RunAlreadyExists(
                f"{record.run.run_id} is already recorded"
            ) from error

    def advance(self, run: EvaluationRun, expected: RunState) -> None:
        """Compare-and-swap the state, or raise."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE runs SET state = ?, updated_at_unix_ms = ?, "
                "failure_kind = ?, failure_from = ?, failure_detail = ? "
                "WHERE run_id = ? AND state = ?",
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
        """
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE runs SET state = ?, updated_at_unix_ms = ?, "
                "failure_kind = ?, failure_from = ?, failure_detail = ?, "
                "outcome = ?, resolved = ?, regressed = ?, f2p_passed = ?, "
                "f2p_total = ?, p2p_passed = ?, p2p_total = ?, "
                "timed_out_commands = ?, task_defect = ?, patch_sha256 = ?, "
                "patch_bytes = ?, patch_is_empty = ?, total_cost_micros = ?, "
                "agent_ms = ?, validation_ms = ?, workspace_path = ? "
                "WHERE run_id = ? AND state = ?",
                (
                    record.run.state.value,
                    _measure("updated_at", record.run.updated_at_unix_ms),
                    record.run.failure.kind.value if record.run.failure else None,
                    record.run.failure.failed_from.value
                    if record.run.failure
                    else None,
                    record.run.failure.detail if record.run.failure else "",
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
                    None
                    if record.patch_is_empty is None
                    else _flag(record.patch_is_empty),
                    _measure("total_cost_micros", int(record.total_cost_micros)),
                    _measure("agent_ms", record.agent_ms),
                    _measure("validation_ms", record.validation_ms),
                    record.workspace_path,
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

    def list_runs(self, limit: int = 50) -> tuple[RunRecord, ...]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY created_at_unix_ms DESC, run_id DESC LIMIT ?",
            (limit,),
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
        for table in (
            "runs",
            "validation_reports",
            "command_results",
            "model_invocations",
        ):
            with closing(self._conn.execute(f"PRAGMA table_info({table})")) as cursor:
                for row in cursor.fetchall():
                    yield table, row["name"], row["type"]
