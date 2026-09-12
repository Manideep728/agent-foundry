"""The domain layer must stay pure.

Domain code must not touch the filesystem, a database, Docker, a model API,
environment variables, the system clock, or a random source -- and must not
import the outer layers. Injected interfaces are how it reaches those things.

This is enforced by parsing the source rather than by importing it: an import
check that works by importing would execute module-level side effects, which
is the very thing being guarded against.
"""

from __future__ import annotations

import ast
from pathlib import Path

DOMAIN_DIR = Path(__file__).resolve().parent.parent / "control_plane" / "domain"

# Top-level module names the domain layer may not reach for. The reason is
# recorded next to each group because a bare list invites someone to delete an
# entry that looks arbitrary.
FORBIDDEN_MODULES = frozenset(
    {
        # filesystem
        "os",
        "pathlib",
        "shutil",
        "tempfile",
        "io",
        "glob",
        "fileinput",
        # storage
        "sqlite3",
        "dbm",
        "pickle",
        "shelve",
        # process and container control
        "subprocess",
        "signal",
        "multiprocessing",
        "threading",
        # clock -- injected, so that runs are reproducible and testable
        "time",
        "datetime",
        "calendar",
        "zoneinfo",
        # identity -- injected, for the same reason
        "random",
        "uuid",
        "secrets",
        # network and providers
        "socket",
        "ssl",
        "http",
        "urllib",
        "asyncio",
        "requests",
        "httpx",
        "anthropic",
        "openai",
        "docker",
        # logging is I/O; the domain returns values, it does not emit them
        "logging",
    }
)

# The domain is the innermost layer, so every sibling layer is off limits.
FORBIDDEN_PACKAGE_PREFIXES = (
    "control_plane.adapters",
    "control_plane.services",
    "control_plane.interface",
)


def forbidden_imports(source: str) -> list[str]:
    """Return every forbidden module name imported by `source`.

    Matches on the first dotted component, so `os.path` is caught by the `os`
    entry and `import xml.etree` is not caught by an `xml` entry that is absent.
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                offenders.extend(_check(alias.name))
        # `from . import x` has module None, and relative imports stay inside
        # the domain package, so both are fine and are skipped here.
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
        ):
            offenders.extend(_check(node.module))
    return offenders


def _check(dotted: str) -> list[str]:
    root = dotted.split(".")[0]
    if root in FORBIDDEN_MODULES:
        return [dotted]
    if any(dotted.startswith(prefix) for prefix in FORBIDDEN_PACKAGE_PREFIXES):
        return [dotted]
    return []


def test_domain_layer_imports_nothing_forbidden() -> None:
    sources = sorted(DOMAIN_DIR.rglob("*.py"))
    assert sources, f"no domain modules found under {DOMAIN_DIR}"

    violations: dict[str, list[str]] = {}
    for path in sources:
        offenders = forbidden_imports(path.read_text(encoding="utf-8"))
        if offenders:
            violations[str(path.relative_to(DOMAIN_DIR.parent.parent))] = offenders

    assert not violations, f"domain layer reached outside itself: {violations}"


# The test above passes trivially while the domain is nearly empty, so the
# checker itself is tested against source that must fail. Without these, the
# guard could be broken for weeks without anyone noticing.


def test_checker_catches_stdlib_io() -> None:
    assert forbidden_imports("import os") == ["os"]
    assert forbidden_imports("import os.path") == ["os.path"]
    assert forbidden_imports("from datetime import datetime") == ["datetime"]
    assert forbidden_imports("import sqlite3, json") == ["sqlite3"]


def test_checker_catches_outer_layers() -> None:
    assert forbidden_imports("from control_plane.adapters import x") == [
        "control_plane.adapters"
    ]
    assert forbidden_imports("import control_plane.services.run") == [
        "control_plane.services.run"
    ]


def test_checker_allows_pure_imports() -> None:
    assert forbidden_imports("import json") == []
    assert forbidden_imports("from dataclasses import dataclass") == []
    assert forbidden_imports("from enum import Enum") == []
    assert forbidden_imports("from . import states") == []
    assert forbidden_imports("from control_plane.domain.states import RunState") == []
