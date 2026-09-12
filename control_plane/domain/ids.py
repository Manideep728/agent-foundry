"""Identifiers for tasks and evaluation runs.

These are deliberately not bare `str`. Two reasons, and the second is the one
that matters:

1.  A `TaskId` and a `RunId` are not interchangeable, and the type checker can
    say so.
2.  **Adapters build filesystem paths and database keys out of these values.**
    An identifier containing `/`, `..`, a null byte or a newline would become a
    path traversal or a corrupted record at the point of use. Constraining the
    character set here means every adapter inherits the guarantee instead of
    re-deriving it, and the check exists in exactly one place.

The domain never *generates* an identifier -- that needs a random source, which
it may not import. Generation is the `IdGenerator` port in `ports.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from control_plane.domain.errors import InvalidIdentifier

MAX_LENGTH = 64

# Must start alphanumeric, then alphanumerics plus a small set of separators.
# Anchored with \A and \Z rather than ^ and $: in Python `$` also matches just
# before a trailing newline, so "run-1\n" would pass a `$`-anchored pattern --
# and a newline in an identifier is exactly the kind of value that corrupts a
# log line or a shell-quoted path later.
_ALLOWED = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def validate_identifier(value: str, kind: str) -> None:
    """Raise `InvalidIdentifier` unless `value` is safe to use as a path segment.

    Public because other domain types hold identifier-shaped strings that reach
    the filesystem -- a fixture directory name, for one -- and they must not
    re-derive the rule.
    """
    if not value:
        raise InvalidIdentifier(f"{kind} must not be empty")
    if len(value) > MAX_LENGTH:
        raise InvalidIdentifier(
            f"{kind} must be at most {MAX_LENGTH} characters, got {len(value)}"
        )
    if not _ALLOWED.fullmatch(value):
        raise InvalidIdentifier(
            f"{kind} must start with a letter or digit and contain only "
            f"letters, digits, '_', '.' or '-'; got {value!r}"
        )
    # Rejected separately from the pattern so the message names the real
    # problem. '..' cannot appear as a whole segment, but it can appear inside
    # one -- and adapters join these into paths.
    if ".." in value:
        raise InvalidIdentifier(f"{kind} must not contain '..'; got {value!r}")


@dataclass(frozen=True, slots=True)
class TaskId:
    """Identifies a benchmark task. Stable across runs and across machines."""

    value: str

    def __post_init__(self) -> None:
        validate_identifier(self.value, "TaskId")

    def __str__(self) -> str:
        return self.value

    def __format__(self, spec: str) -> str:
        # Without this, `f"{task_id:<24}"` raises TypeError: a dataclass
        # inherits object.__format__, which rejects any non-empty spec. These
        # wrap a string and are printed in aligned columns, so they must format
        # like the string they wrap.
        return format(self.value, spec)


@dataclass(frozen=True, slots=True)
class RunId:
    """Identifies one evaluation run. Supplied by an `IdGenerator`."""

    value: str

    def __post_init__(self) -> None:
        validate_identifier(self.value, "RunId")

    def __str__(self) -> str:
        return self.value

    def __format__(self, spec: str) -> str:
        return format(self.value, spec)
