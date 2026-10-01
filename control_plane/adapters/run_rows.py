"""How a run becomes rows, and rows become a run, whichever database holds them.

Shared by the SQLite and Postgres repositories, so the two cannot disagree
about what is stored: one mapping from record to columns, one set of
statements, one rebuild through the domain constructors. Each repository keeps
only what is genuinely its own -- the DDL, connection handling, transactions,
locking and schema versioning.

The statements bind values by name (`:run_id`). SQLite reads that spelling
natively; the Postgres repository rewrites it to `%(run_id)s` once, at import.

Every measurement is an integer, and every flag is stored as 0 or 1 in an
integer column with a CHECK, in both databases. The same row then means the
same thing in either, and `_measure` refuses a non-integer before any driver
sees it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol

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


class Row(Protocol):
    """A row read back by column name: `sqlite3.Row` or a psycopg `dict_row`."""

    def __getitem__(self, key: str, /) -> Any: ...


TABLES = ("runs", "validation_reports", "command_results", "model_invocations")


CREATE_SQL = (
    "INSERT INTO runs "
    "(run_id, task_id, suite, state, created_at_unix_ms, "
    " updated_at_unix_ms, fixture_sha256, runner_id, "
    " runner_version, arch, pid1, workspace_path, "
    " agent_id, provider_id, model_id, prompt_id, harness_version) "
    "VALUES (:run_id, :task_id, :suite, :state, :created_at_unix_ms, "
    " :updated_at_unix_ms, :fixture_sha256, :runner_id, "
    " :runner_version, :arch, :pid1, :workspace_path, "
    " :agent_id, :provider_id, :model_id, :prompt_id, :harness_version)"
)

ADVANCE_SQL = (
    "UPDATE runs SET state = :state, updated_at_unix_ms = :updated_at_unix_ms, "
    "failure_kind = :failure_kind, failure_from = :failure_from, "
    "failure_detail = :failure_detail "
    "WHERE run_id = :run_id AND state = :expected_state"
)

FINISH_SQL = (
    "UPDATE runs SET state = :state, updated_at_unix_ms = :updated_at_unix_ms, "
    "failure_kind = :failure_kind, failure_from = :failure_from, "
    "failure_detail = :failure_detail, "
    "runner_version = :runner_version, arch = :arch, pid1 = :pid1, "
    "outcome = :outcome, resolved = :resolved, regressed = :regressed, "
    "f2p_passed = :f2p_passed, f2p_total = :f2p_total, "
    "p2p_passed = :p2p_passed, p2p_total = :p2p_total, "
    "timed_out_commands = :timed_out_commands, task_defect = :task_defect, "
    "patch_sha256 = :patch_sha256, patch_bytes = :patch_bytes, "
    "patch_is_empty = :patch_is_empty, total_cost_micros = :total_cost_micros, "
    "agent_ms = :agent_ms, validation_ms = :validation_ms, "
    "workspace_path = :workspace_path, image_digest = :image_digest, "
    "agent_note = :agent_note "
    "WHERE run_id = :run_id AND state = :expected_state"
)
"""The three statements that write `runs`, lifted out of their methods.

Named so a test can read the column names back out of the SQL. Three bugs have
now been a column missing from one of these lists while the value that belongs
in it was computed correctly -- `arch` and `pid1`, `workspace_path` before
them. A comment asking the next author to keep the lists in step is what was
there when each of those shipped.

The values are bound by name from `run_columns`, never by position, so a list
that names the right columns cannot feed them in the wrong order.
"""

REPORT_SQL = (
    "INSERT INTO validation_reports "
    "(run_id, schema_version, runner_version, arch, pid1, shell, workdir, "
    " started_at_unix_ms, wall_ms, exit_reason, aborted, orphans_reaped_total) "
    "VALUES (:run_id, :schema_version, :runner_version, :arch, :pid1, :shell, "
    " :workdir, :started_at_unix_ms, :wall_ms, :exit_reason, :aborted, "
    " :orphans_reaped_total)"
)

COMMAND_SQL = (
    "INSERT INTO command_results "
    "(run_id, idx, command, started_at_unix_ms, duration_ms, outcome, "
    " exit_code, signal, timed_out, deadline_signal_sent, killed_after_grace, "
    " group_escaped, drain_deadline_hit, orphans_reaped, spawn_error, "
    " stdout_bytes_total, stdout_bytes_written, stdout_truncated, stdout_path, "
    " stdout_tail, stderr_bytes_total, stderr_bytes_written, stderr_truncated, "
    " stderr_path, stderr_tail) "
    "VALUES (:run_id, :idx, :command, :started_at_unix_ms, :duration_ms, "
    " :outcome, :exit_code, :signal, :timed_out, :deadline_signal_sent, "
    " :killed_after_grace, :group_escaped, :drain_deadline_hit, "
    " :orphans_reaped, :spawn_error, :stdout_bytes_total, "
    " :stdout_bytes_written, :stdout_truncated, :stdout_path, :stdout_tail, "
    " :stderr_bytes_total, :stderr_bytes_written, :stderr_truncated, "
    " :stderr_path, :stderr_tail)"
)

INVOCATION_SQL = (
    "INSERT INTO model_invocations "
    "(run_id, seq, provider_id, model_id, input_tokens, output_tokens, "
    " cache_write_tokens, cache_read_tokens, cost_micros, latency_ms) "
    "VALUES (:run_id, :seq, :provider_id, :model_id, :input_tokens, "
    " :output_tokens, :cache_write_tokens, :cache_read_tokens, :cost_micros, "
    " :latency_ms)"
)

GET_SQL = "SELECT * FROM runs WHERE run_id = :run_id"

LIST_ALL_SQL = "SELECT * FROM runs ORDER BY created_at_unix_ms DESC, run_id DESC"
LIST_SQL = LIST_ALL_SQL + " LIMIT :limit"
"""Two statements, one built from the other, because the two databases spell
"no limit" differently: SQLite takes a negative number, Postgres takes NULL."""

UNFINISHED_SQL = (
    "SELECT * FROM runs WHERE state NOT IN ('scored', 'failed', 'aborted') "
    "ORDER BY created_at_unix_ms"
)

REPORT_HEADER_SQL = "SELECT * FROM validation_reports WHERE run_id = :run_id"
COMMANDS_SQL = "SELECT * FROM command_results WHERE run_id = :run_id ORDER BY idx"
INVOCATIONS_SQL = "SELECT * FROM model_invocations WHERE run_id = :run_id ORDER BY seq"


FIELD_COLUMNS: Mapping[str, frozenset[str]] = MappingProxyType(
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
        "harness_version": frozenset({"harness_version"}),
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

CHILD_TABLE_FIELDS = frozenset({"report", "invocations"})
"""Fields stored in a table of their own rather than in a `runs` column."""

CREATE_ONLY = frozenset(
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
        "harness_version",
    }
)
"""Columns written once, at creation, and never revised.

Identity and configuration: what the run is and what was being measured. Every
other column describes what happened, so `finish()` must write it.
"""


def flag(value: bool) -> int:
    return 1 if value else 0


def measure(name: str, value: int | None) -> int | None:
    """Refuse a non-integer measurement.

    The third float guard. SQLite's STRICT rejects 1.5 but silently coerces
    2.0, and `isinstance(True, int)` is True in Python -- so a boolean written
    into a count would pass every other check.
    """
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    return value


# --- record -> rows ----------------------------------------------------------


def run_columns(record: RunRecord) -> dict[str, object]:
    """Every column of `runs`, by name, as one record says it.

    The single place a record becomes a row. Each statement binds the names it
    writes, so `create` and `finish` read the same value for a column by
    construction, and a test can compare these keys against the table.

    `create` writes some columns before they are known -- arch 'unknown', pid1
    False -- because the row must exist, and name its workspace, before the
    run can die leaving one behind. `finish` writes them again.
    """
    run = record.run
    failure = run.failure
    score = record.score
    return {
        "run_id": run.run_id.value,
        "task_id": run.task_id.value,
        "suite": record.suite.value,
        "state": run.state.value,
        "created_at_unix_ms": measure("created_at", run.created_at_unix_ms),
        "updated_at_unix_ms": measure("updated_at", run.updated_at_unix_ms),
        "failure_kind": failure.kind.value if failure else None,
        "failure_from": failure.failed_from.value if failure else None,
        "failure_detail": failure.detail if failure else "",
        "fixture_sha256": record.fixture_sha256,
        "runner_id": record.runner_id,
        "runner_version": record.runner_version,
        "arch": record.arch,
        "pid1": flag(record.pid1),
        "outcome": record.outcome.value if record.outcome else None,
        "resolved": flag(score.resolved) if score else None,
        "regressed": flag(score.regressed) if score else None,
        "f2p_passed": score.fail_to_pass_passed if score else None,
        "f2p_total": score.fail_to_pass_total if score else None,
        "p2p_passed": score.pass_to_pass_passed if score else None,
        "p2p_total": score.pass_to_pass_total if score else None,
        "timed_out_commands": score.timed_out_commands if score else None,
        "task_defect": flag(record.task_defect),
        "patch_sha256": record.patch_sha256,
        "patch_bytes": measure("patch_bytes", record.patch_bytes),
        "patch_is_empty": (
            None if record.patch_is_empty is None else flag(record.patch_is_empty)
        ),
        "total_cost_micros": measure(
            "total_cost_micros", int(record.total_cost_micros)
        ),
        "agent_ms": measure("agent_ms", record.agent_ms),
        "validation_ms": measure("validation_ms", record.validation_ms),
        "workspace_path": record.workspace_path,
        "image_digest": record.image_digest,
        "agent_note": record.agent_note,
        "agent_id": record.agent_id,
        "provider_id": record.provider_id,
        "model_id": record.model_id,
        "prompt_id": record.prompt_id,
        "harness_version": record.harness_version,
    }


def advance_columns(run: EvaluationRun, expected: RunState) -> dict[str, object]:
    failure = run.failure
    return {
        "run_id": run.run_id.value,
        "state": run.state.value,
        "updated_at_unix_ms": measure("updated_at", run.updated_at_unix_ms),
        "failure_kind": failure.kind.value if failure else None,
        "failure_from": failure.failed_from.value if failure else None,
        "failure_detail": failure.detail if failure else "",
        "expected_state": expected.value,
    }


def report_columns(run_id: RunId, report: ValidationReport) -> dict[str, object]:
    return {
        "run_id": run_id.value,
        "schema_version": report.schema_version,
        "runner_version": report.runner_version,
        "arch": report.arch,
        "pid1": flag(report.pid1),
        "shell": report.shell,
        "workdir": report.workdir,
        "started_at_unix_ms": measure("started_at", report.started_at_unix_ms),
        "wall_ms": measure("wall_ms", report.wall_ms),
        "exit_reason": report.exit_reason.value,
        "aborted": flag(report.aborted),
        "orphans_reaped_total": measure("orphans", report.orphans_reaped_total),
    }


def command_columns(run_id: RunId, result: CommandResult) -> dict[str, object]:
    return {
        "run_id": run_id.value,
        "idx": result.index,
        "command": result.command,
        "started_at_unix_ms": measure("started_at", result.started_at_unix_ms),
        "duration_ms": measure("duration_ms", result.duration_ms),
        "outcome": result.outcome.value,
        "exit_code": result.exit_code,
        "signal": result.signal,
        "timed_out": flag(result.timed_out),
        "deadline_signal_sent": flag(result.deadline_signal_sent),
        "killed_after_grace": flag(result.killed_after_grace),
        "group_escaped": flag(result.group_escaped),
        "drain_deadline_hit": flag(result.drain_deadline_hit),
        "orphans_reaped": measure("orphans", result.orphans_reaped),
        "spawn_error": result.spawn_error,
        "stdout_bytes_total": measure("stdout_total", result.stdout.bytes_total),
        "stdout_bytes_written": measure("stdout_written", result.stdout.bytes_written),
        "stdout_truncated": flag(result.stdout.truncated),
        "stdout_path": result.stdout.path,
        "stdout_tail": result.stdout.tail,
        "stderr_bytes_total": measure("stderr_total", result.stderr.bytes_total),
        "stderr_bytes_written": measure("stderr_written", result.stderr.bytes_written),
        "stderr_truncated": flag(result.stderr.truncated),
        "stderr_path": result.stderr.path,
        "stderr_tail": result.stderr.tail,
    }


def invocation_columns(
    run_id: RunId, seq: int, invocation: ModelInvocation
) -> dict[str, object]:
    usage = invocation.usage
    return {
        "run_id": run_id.value,
        "seq": seq,
        "provider_id": invocation.provider_id,
        "model_id": invocation.model_id,
        "input_tokens": measure("input_tokens", usage.input_tokens),
        "output_tokens": measure("output_tokens", usage.output_tokens),
        "cache_write_tokens": measure("cache_write", usage.cache_write_tokens),
        "cache_read_tokens": measure("cache_read", usage.cache_read_tokens),
        "cost_micros": measure("cost", int(invocation.cost)),
        "latency_ms": measure("latency_ms", invocation.latency_ms),
    }


# --- rows -> record ----------------------------------------------------------


def hydrate_run(
    row: Row,
    report: ValidationReport | None,
    invocations: tuple[ModelInvocation, ...],
) -> RunRecord:
    """Rebuild a record through the domain's own constructors.

    Every __post_init__ runs here, so the read path is the validation path
    and a row that violates an invariant fails loudly rather than becoming
    a plausible wrong number.
    """
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
        state=RunState(row["state"]),
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
        report=report,
        score=score,
        outcome=AgentOutcome(row["outcome"]) if row["outcome"] else None,
        task_defect=bool(row["task_defect"]),
        patch_sha256=row["patch_sha256"],
        patch_bytes=row["patch_bytes"],
        patch_is_empty=(
            None if row["patch_is_empty"] is None else bool(row["patch_is_empty"])
        ),
        invocations=invocations,
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
        harness_version=row["harness_version"],
    )


def hydrate_report(header: Row, commands: Sequence[Row]) -> ValidationReport:
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
        commands=tuple(_hydrate_command(row) for row in commands),
    )


def _hydrate_command(row: Row) -> CommandResult:
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


def hydrate_invocation(row: Row) -> ModelInvocation:
    return ModelInvocation(
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
