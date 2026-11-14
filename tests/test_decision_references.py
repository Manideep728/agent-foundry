"""Every decision the public tree cites must be a record in docs/DECISIONS.md.

Code comments and docs cite decisions as `D<n>`. A citation that names no
published record sends a reader to a decision they cannot read, and the
number may belong to a different record later.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECISIONS = ROOT / "docs" / "DECISIONS.md"

SCANNED_DIRS = ("control_plane", "runner", "tests", "docs", "infra", "tasks")
SCANNED_ROOT_FILES = ("README.md", "Makefile", "pyproject.toml")
SCANNED_SUFFIXES = frozenset(
    {".py", ".md", ".cpp", ".hpp", ".sh", ".tf", ".toml", ".yml", ".txt"}
)
SCANNED_NAMES = frozenset({"Dockerfile", "Makefile"})

# Fixture repositories are the agent's input, not this project's prose.
SKIPPED_PARTS = frozenset({"fixtures", "__pycache__"})

CITATION = re.compile(r"\bD(\d+)\b")
HEADING = re.compile(r"^## D(\d+) — ", re.MULTILINE)


def _scanned_files() -> list[Path]:
    files = [ROOT / name for name in SCANNED_ROOT_FILES]
    for directory in SCANNED_DIRS:
        for path in (ROOT / directory).rglob("*"):
            if not path.is_file() or SKIPPED_PARTS.intersection(path.parts):
                continue
            if path.suffix in SCANNED_SUFFIXES or path.name in SCANNED_NAMES:
                files.append(path)
    return files


def _published_numbers() -> list[int]:
    return [int(number) for number in HEADING.findall(DECISIONS.read_text())]


def test_every_cited_decision_is_published() -> None:
    published = set(_published_numbers())
    dangling = []
    for path in _scanned_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            for match in CITATION.finditer(line):
                if int(match.group(1)) not in published:
                    where = f"{path.relative_to(ROOT)}:{line_number}"
                    dangling.append(f"{where} cites D{match.group(1)}")
    assert not dangling, "unpublished decisions cited:\n" + "\n".join(dangling)


def test_no_decision_number_is_used_twice() -> None:
    numbers = _published_numbers()
    duplicates = sorted({number for number in numbers if numbers.count(number) > 1})
    assert not duplicates, f"decision numbers used twice: {duplicates}"


def test_the_scan_reaches_the_code_that_cites_decisions() -> None:
    # Guards the guard: a wrong root or suffix list would scan nothing and pass.
    scanned = {path.relative_to(ROOT).as_posix() for path in _scanned_files()}
    assert "control_plane/domain/task.py" in scanned
    assert "runner/src/main.cpp" in scanned
    assert "docs/DECISIONS.md" in scanned
