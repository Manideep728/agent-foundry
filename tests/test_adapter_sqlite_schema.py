"""The guard for the bug this project keeps shipping.

Three defects have now had one shape: a value computed correctly and then not
written down. `arch` and `pid1` were missing from `finish()`'s SET list;
`workspace_path` before them; the agent result was dropped on two of six
failure paths. Every one of them passed review, passed the type checker, and
passed a test suite that asserted on the object in hand rather than on what the
database kept.

The common cause is not carelessness. It is that one value is described by five
hand-maintained lists -- an argument list, a constructor, an INSERT, an UPDATE
and a rehydrator -- with nothing comparing them. These tests compare them.

They are deliberately set comparisons rather than a generator that emits the
SQL. A generator would be this same mapping spelled as code, and it would cost
the property that makes `sqlite_repo.py` defensible: a reader can see exactly
what is written.
"""

from __future__ import annotations

import dataclasses
import re

from control_plane.adapters.sqlite_repo import (
    _ADVANCE_SQL,
    _CHILD_TABLE_FIELDS,
    _CREATE_ONLY,
    _CREATE_SQL,
    _FIELD_COLUMNS,
    _FINISH_SQL,
    _SCHEMA,
)
from control_plane.domain.record import RunRecord


def record_fields() -> frozenset[str]:
    return frozenset(field.name for field in dataclasses.fields(RunRecord))


def runs_columns() -> frozenset[str]:
    """The columns of `runs`, read out of the schema text.

    Read from `_SCHEMA` rather than from a live `PRAGMA table_info`, because
    this test is about the statements agreeing with the declared table. A
    migrated database is a different question, and
    test_a_migrated_database_matches_a_fresh_one already asks it.
    """
    body = _SCHEMA[0].split("CREATE TABLE runs (", 1)[1].split(") STRICT", 1)[0]
    columns = set()
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("CHECK"):
            continue
        columns.add(stripped.split()[0])
    return frozenset(columns)


def set_columns(sql: str) -> frozenset[str]:
    """The columns an UPDATE assigns."""
    assignments = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    return frozenset(re.findall(r"(\w+) = \?", assignments))


def insert_columns(sql: str) -> frozenset[str]:
    """The columns an INSERT names."""
    named = sql.split("(", 1)[1].split(")", 1)[0]
    return frozenset(name.strip() for name in named.split(","))


def mapped_columns() -> frozenset[str]:
    return frozenset().union(*_FIELD_COLUMNS.values())


def test_every_record_field_reaches_a_column_or_a_child_table() -> None:
    """A field the record carries and the database never sees.

    This is the bug in its purest form: add a field, compute it correctly,
    store it nowhere. Fails the moment `RunRecord` grows a field that
    `_FIELD_COLUMNS` does not account for.
    """
    accounted = frozenset(_FIELD_COLUMNS) | _CHILD_TABLE_FIELDS
    assert record_fields() - accounted == frozenset()


def test_no_field_is_mapped_that_the_record_does_not_have() -> None:
    """The other direction: a mapping left behind by a renamed field.

    Without this, `_FIELD_COLUMNS` could name `agent_note_text` forever and
    every other test here would still pass while guarding nothing.
    """
    accounted = frozenset(_FIELD_COLUMNS) | _CHILD_TABLE_FIELDS
    assert accounted - record_fields() == frozenset()


def test_every_runs_column_is_fed_by_exactly_one_field() -> None:
    """A column nothing writes, or two fields quietly sharing one.

    Both directions at once, because a column that no field feeds is a column
    that will hold its default forever, and two fields writing one column is
    two facts overwriting each other.
    """
    assert mapped_columns() == runs_columns()

    seen: dict[str, str] = {}
    for field, columns in _FIELD_COLUMNS.items():
        for column in columns:
            assert column not in seen, (
                f"{column} is fed by both {seen[column]} and {field}"
            )
            seen[column] = field


def test_every_column_that_is_not_create_only_is_in_the_finish_set_list() -> None:
    """The `arch` and `pid1` bug, as a set comparison.

    `create()` runs before a container exists, so it guesses the environment:
    arch 'unknown', pid1 False. For as long as `finish()` left those columns
    alone, that guess was what every stored run kept -- while the run's own
    report held the truth. Anything that is not identity or configuration
    describes what happened, so `finish()` must write it.
    """
    must_finish = runs_columns() - _CREATE_ONLY
    assert must_finish - set_columns(_FINISH_SQL) == frozenset()


def test_create_and_finish_together_cover_every_column() -> None:
    """The `workspace_path` bug: a column no statement ever wrote.

    It was in neither list, so a recovery scan had no directory to delete.
    """
    assert insert_columns(_CREATE_SQL) | set_columns(_FINISH_SQL) == runs_columns()


def test_create_writes_every_column_that_is_written_only_once() -> None:
    """A create-only column that `create()` forgets is never written at all.

    `_CREATE_ONLY` is the claim that these need no later write. This is the
    check that the claim is paid for.
    """
    assert _CREATE_ONLY - insert_columns(_CREATE_SQL) == frozenset()


def test_advancing_a_state_touches_only_the_state_and_its_failure() -> None:
    """`advance()` is a state transition, not a measurement write.

    If it ever learns to write a measurement column, two paths would write one
    fact and the compare-and-swap would no longer describe what it swapped.
    """
    assert set_columns(_ADVANCE_SQL) == frozenset(
        {
            "state",
            "updated_at_unix_ms",
            "failure_kind",
            "failure_from",
            "failure_detail",
        }
    )
