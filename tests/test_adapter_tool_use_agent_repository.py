"""The tool loop under `tools-v3`, the variant for a whole repository.

The security boundary is re-tested for the new tools because they reach the
filesystem through new code: a boundary that holds for `read_file` is not
thereby holding for `search`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from control_plane.adapters.prompts import (
    LIST_DIRECTORY,
    LIST_FILES,
    READ_FILE,
    SEARCH,
    WRITE_FILE,
    variant,
)
from control_plane.adapters.tool_use_agent import MAX_LISTED, ToolUseAgent
from control_plane.domain.model import MessageRole, ToolCall
from tests.fakes import FakeClock
from tests.test_adapter_tool_use_agent import ScriptedClient, calls, prepared, says

V3 = variant("tools-v3")


def run_v3(tmp_path: Path, *tool_calls: ToolCall, hidden: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    task, repo = prepared(tmp_path)
    task = replace(task, hidden_paths=hidden)
    client = ScriptedClient([calls(*tool_calls), says("done")])
    result = ToolUseAgent(client, FakeClock(), prompt=V3).edit(repo, task)
    answers = [m.content for m in client.conversations[1] if m.role is MessageRole.TOOL]
    return result, repo, client, answers


def test_the_opening_shows_the_top_level_not_every_file(tmp_path: Path) -> None:
    _, _, client, _ = run_v3(tmp_path, ToolCall(LIST_DIRECTORY, {}))

    opening = client.conversations[0][1].content
    assert "csvkit/" in opening
    assert "csvkit/fields.py" not in opening


def test_a_directory_lists_its_children_with_subdirectories_marked(
    tmp_path: Path,
) -> None:
    _, _, _, (answer,) = run_v3(tmp_path, ToolCall(LIST_DIRECTORY, {"path": "csvkit"}))

    assert "csvkit/fields.py" in answer.splitlines()


def test_a_long_directory_is_cut_and_says_how_much_was_left_out(
    tmp_path: Path,
) -> None:
    task, repo = prepared(tmp_path)
    many = repo / "many"
    many.mkdir()
    for number in range(MAX_LISTED + 5):
        (many / f"f{number:04}.txt").write_text("x\n")
    client = ScriptedClient(
        [calls(ToolCall(LIST_DIRECTORY, {"path": "many"})), says("done")]
    )

    ToolUseAgent(client, FakeClock(), prompt=V3).edit(repo, task)

    answer = client.conversations[1][-1].content
    assert answer.endswith("[5 more not shown]")


def test_search_finds_text_and_names_the_line(tmp_path: Path) -> None:
    _, _, _, (answer,) = run_v3(
        tmp_path, ToolCall(SEARCH, {"text": "def split_fields"})
    )

    assert answer.startswith("csvkit/fields.py:")
    assert "def split_fields" in answer


def test_search_never_shows_a_hidden_file(tmp_path: Path) -> None:
    """D18 again: the graded test must not reach the prompt by another tool."""
    _, _, _, (answer,) = run_v3(
        tmp_path,
        ToolCall(SEARCH, {"text": "import"}),
        hidden=("tests/test_quoting.py",),
    )

    assert "tests/test_quoting.py" not in answer


def test_search_cannot_leave_the_repository(tmp_path: Path) -> None:
    _, _, _, (answer,) = run_v3(tmp_path, ToolCall(SEARCH, {"text": "x", "path": ".."}))

    assert answer.startswith("error:")


def test_a_read_with_a_range_returns_numbered_lines(tmp_path: Path) -> None:
    _, repo, _, (answer,) = run_v3(
        tmp_path,
        ToolCall(
            READ_FILE, {"path": "csvkit/fields.py", "start_line": 2, "end_line": 3}
        ),
    )

    source = (repo / "csvkit" / "fields.py").read_text().splitlines()
    assert answer.splitlines() == [f"2: {source[1]}", f"3: {source[2]}"]


def test_a_backwards_range_is_a_tool_error_the_model_can_correct(
    tmp_path: Path,
) -> None:
    _, _, _, (answer,) = run_v3(
        tmp_path,
        ToolCall(
            READ_FILE, {"path": "csvkit/fields.py", "start_line": 5, "end_line": 2}
        ),
    )

    assert answer.startswith("error: start_line 5 is after end_line 2")


def test_write_file_is_the_same_tool_under_every_variant(tmp_path: Path) -> None:
    result, repo, _, _ = run_v3(
        tmp_path, ToolCall(WRITE_FILE, {"path": "new.py", "content": "X = 1"})
    )

    assert (repo / "new.py").read_text() == "X = 1\n"
    assert result.outcome is None


def test_a_v1_tool_is_unknown_under_v3(tmp_path: Path) -> None:
    _, _, _, (answer,) = run_v3(tmp_path, ToolCall(LIST_FILES, {}))

    assert answer.startswith("error: no tool named list_files")
