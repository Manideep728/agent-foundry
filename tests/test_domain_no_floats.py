"""No floating point anywhere in the domain.

Every measurement this project records is an integer: milliseconds, bytes,
tokens, micro-dollars. That is not stylistic. binary64 cannot represent 0.1
exactly, so a total assembled from per-item floats depends on summation order
and the same result scored twice can print two different figures. Stored as
SQLite REAL it stops summing exactly and stops ordering reliably.

The rule is easy to state and easy to violate by accident -- `tokens / 1_000`
looks harmless and is a true-division float. So it is enforced the same way the
layer boundary is: by parsing the source, with the checker itself tested
against code that must fail.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

DOMAIN_DIR = Path(__file__).resolve().parent.parent / "control_plane" / "domain"


def float_usages(source: str) -> list[str]:
    """Return every use of floating point in `source`.

    Catches three shapes: a float literal, true division (which produces a
    float even from two ints), and any mention of the name `float` -- a call,
    an annotation, or a cast.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            found.append(f"float literal {node.value!r} on line {node.lineno}")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            # `//` is ast.FloorDiv and stays integral; only `/` is caught here.
            found.append(f"true division on line {node.lineno}")
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Div):
            found.append(f"true division assignment on line {node.lineno}")
        elif isinstance(node, ast.Name) and node.id == "float":
            found.append(f"reference to float on line {node.lineno}")
    return found


def test_the_domain_uses_no_floating_point() -> None:
    sources = sorted(DOMAIN_DIR.rglob("*.py"))
    assert sources, f"no domain modules found under {DOMAIN_DIR}"

    violations: dict[str, list[str]] = {}
    for path in sources:
        usages = float_usages(path.read_text(encoding="utf-8"))
        if usages:
            violations[path.name] = usages

    assert not violations, f"floating point in the domain: {violations}"


# The scan above passes while nobody has made the mistake, which proves nothing
# about the checker. These do.


def test_checker_catches_true_division() -> None:
    # The realistic slip: this looks integral, and is not.
    assert float_usages("cost = tokens * price / 1_000_000") == [
        "true division on line 1"
    ]


def test_checker_allows_floor_division() -> None:
    # How the cost model actually divides.
    assert float_usages("cost = tokens * price // 1_000_000") == []


def test_checker_catches_float_literals() -> None:
    assert float_usages("RATE = 1.25") == ["float literal 1.25 on line 1"]


def test_checker_catches_float_annotations_and_casts() -> None:
    assert float_usages("def f(x: float) -> float: ...")
    assert float_usages("seconds = float(ms)")


@pytest.mark.parametrize(
    "source",
    [
        "total = a + b",
        "half = n // 2",
        "ms = 1_000",
        "ratio = divmod(ms, 1000)",
        "from decimal import Decimal",
    ],
)
def test_checker_allows_integer_arithmetic(source: str) -> None:
    assert float_usages(source) == []
