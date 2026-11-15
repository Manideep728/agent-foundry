"""An agent that looks around before it edits.

The single-shot agent sends every visible file and asks for the whole repository
back in one reply. That works for a fixture of a few hundred bytes and stops
working as soon as a task is larger than one prompt, because the prompt is the
entire context the model ever gets.

This one is given three tools -- list the files, read one, write one -- and
several turns to use them. It costs more calls for the same edit, which is the
point of keeping both: they answer the same tasks through different amounts of
context, and the benchmark can say which is worth the tokens.

Two properties of the single-shot agent are deliberately *not* reproduced.

Writes are no longer all-or-nothing. `_apply` there plans every edit before
making any, so a reply whose second block is unusable leaves nothing behind.
A loop writes when the model says to, so a run that gives up halfway leaves
half an edit -- which is a real patch, scored as one, and not called malformed.

`MALFORMED_OUTPUT` is claimable only while nothing has been written. The record
refuses that outcome alongside a non-empty patch, and rightly: a reply that
could not be read cannot have changed a file. A loop that wrote something and
then lost its way reports no outcome at all and lets the score speak.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from control_plane.adapters.prompts import (
    DEFAULT_TOOL_PROMPT,
    LIST_DIRECTORY,
    LIST_FILES,
    READ_FILE,
    REPOSITORY_TOOLS,
    SEARCH,
    TOOLS,
    WRITE_FILE,
    PromptVariant,
    variant,
)
from control_plane.domain.budget import is_exhausted, validate_ceiling
from control_plane.domain.errors import DomainError
from control_plane.domain.fixture import is_excluded, validate_fixture_path
from control_plane.domain.model import (
    Message,
    MessageRole,
    MicroUsd,
    ModelClient,
    ModelInvocation,
    ModelResponse,
    TokenPrice,
    ToolCall,
    cost_micros,
    model_latency_ms,
    rate_limit_note,
    total_micros,
)
from control_plane.domain.outcomes import AgentOutcome
from control_plane.domain.ports import Clock
from control_plane.domain.task import TaskDefinition
from control_plane.services.errors import AgentError
from control_plane.services.ports import AgentResult

AGENT_ID = "tools"

DEFAULT_MAX_TURNS = 12
"""Turns before the loop stops and reports what the model managed.

A budget, not a target. A model that keeps reading files without ever writing
one would otherwise run until the task's total timeout, and a benchmark row
that says "timed out" tells you nothing about the model.
"""

MAX_READ_BYTES = 64_000
"""Per-file cap on what a read returns.

Enough for any fixture file, and small enough that a model asking for something
enormous cannot fill its own context with one call.
"""

MAX_LISTED = 300
"""Entries one directory listing returns. A real repository's test directory
can hold more files than are worth a turn's worth of context."""

MAX_MATCHES = 100
MAX_SEARCHED_BYTES = 1_000_000
"""Files larger than this are skipped by search: generated data, not source."""

DEFAULT_PROMPT = variant(DEFAULT_TOOL_PROMPT)
"""What the loop uses when the caller names no prompt."""


def _line_number(value: object, *, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ToolError(f"a line number must be a whole number from 1, got {value!r}")
    return value


class ToolError(Exception):
    """A tool the model could not use as asked.

    Returned to the model as the tool's answer rather than raised, so it can
    correct itself on the next turn. A model calling a tool wrongly is part of
    what this measures, not a harness fault -- unlike `AgentError`, which means
    the adapter itself broke.
    """


@dataclass(frozen=True, slots=True)
class _Workspace:
    """The repository copy, and the rules for touching it."""

    root: Path
    hidden: frozenset[str]

    def visible_files(self) -> list[str]:
        found = []
        for entry in sorted(self.root.rglob("*")):
            if not entry.is_file():
                continue
            relative = entry.relative_to(self.root)
            if any(is_excluded(part) for part in relative.parts):
                continue
            path = relative.as_posix()
            if path in self.hidden:
                continue
            found.append(path)
        return found

    def resolve(self, path: object) -> Path:
        """Turn a path the model sent into one inside the workspace.

        Two layers, both of which earn their place. `validate_fixture_path`
        rejects absolute paths, traversal segments and backslashes; the
        resolved-parent check catches what a symlink inside the workspace could
        still reach. This is the same pair the single-shot agent uses, called
        rather than restated -- a second spelling of a boundary is how the two
        drift apart.
        """
        if not isinstance(path, str):
            raise ToolError(f"path must be a string, got {type(path).__name__}")
        try:
            validate_fixture_path(path)
        except DomainError as error:
            raise ToolError(f"unusable path {path!r}: {error}") from error

        target = (self.root.resolve() / path).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ToolError(f"path {path!r} leaves the repository")
        return target

    def read(self, path: object) -> str:
        if isinstance(path, str) and path in self.hidden:
            # Named rather than silently absent, so the model stops asking. It
            # is still never shown: D18 -- a score taken with the fail-to-pass
            # test in the prompt cannot be compared with one taken without it.
            raise ToolError(f"{path} is not available for this task")
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"{path} is not a file in this repository")
        data = target.read_bytes()[:MAX_READ_BYTES]
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolError(f"{path} is not UTF-8 text") from error

    def read_lines(self, path: object, start: object, end: object) -> str:
        """Lines `start` to `end`, counted from 1, each with its number.

        Read whole and then sliced, rather than capped like `read`: a real
        source file is often larger than the cap, and the range is how the
        model reaches its end. The slice is what is capped.
        """
        if isinstance(path, str) and path in self.hidden:
            raise ToolError(f"{path} is not available for this task")
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"{path} is not a file in this repository")
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise ToolError(f"{path} is not UTF-8 text") from error
        first = _line_number(start, default=1)
        last = _line_number(end, default=len(lines))
        if first > last:
            raise ToolError(f"start_line {first} is after end_line {last}")
        numbered = "\n".join(
            f"{number}: {lines[number - 1]}"
            for number in range(first, min(last, len(lines)) + 1)
        )
        if len(numbered) > MAX_READ_BYTES:
            numbered = numbered[:MAX_READ_BYTES] + "\n[cut: ask for fewer lines]"
        return numbered or f"{path} has {len(lines)} line(s); none in that range"

    def list_directory(self, path: object) -> str:
        directory = self.root if path in (None, "", ".") else self.resolve(path)
        if not directory.is_dir():
            raise ToolError(f"{path} is not a directory in this repository")
        entries = []
        for entry in sorted(directory.iterdir()):
            relative = entry.relative_to(self.root).as_posix()
            if is_excluded(entry.name) or relative in self.hidden:
                continue
            entries.append(f"{relative}/" if entry.is_dir() else relative)
        if len(entries) > MAX_LISTED:
            kept = entries[:MAX_LISTED]
            return "\n".join([*kept, f"[{len(entries) - MAX_LISTED} more not shown]"])
        return "\n".join(entries) or "(empty)"

    def search(self, text: object, path: object) -> str:
        if not isinstance(text, str) or not text:
            raise ToolError("text must be a non-empty string")
        directory = self.root if path in (None, "", ".") else self.resolve(path)
        if not directory.is_dir():
            raise ToolError(f"{path} is not a directory in this repository")
        matches: list[str] = []
        for file in sorted(directory.rglob("*")):
            relative = file.relative_to(self.root)
            if (
                not file.is_file()
                or any(is_excluded(part) for part in relative.parts)
                or relative.as_posix() in self.hidden
                or file.stat().st_size > MAX_SEARCHED_BYTES
            ):
                continue
            try:
                content = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                if text in line:
                    matches.append(f"{relative.as_posix()}:{number}: {line.strip()}")
                    if len(matches) == MAX_MATCHES:
                        matches.append(f"[stopped at {MAX_MATCHES} matches]")
                        return "\n".join(matches)
        return "\n".join(matches) or f"no match for {text!r}"

    def write(self, path: object, content: object) -> str:
        if not isinstance(content, str):
            raise ToolError(f"content must be a string, got {type(content).__name__}")
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Normalised to end in exactly one newline, matching the single-shot
        # agent, so the two produce identical patches for identical edits and
        # their diffs stay comparable.
        target.write_text(content.rstrip("\n") + "\n", encoding="utf-8")
        return f"wrote {len(content)} bytes to {path}"


class ToolUseAgent:
    """Edits a workspace over several turns, using tools."""

    def __init__(
        self,
        client: ModelClient,
        clock: Clock,
        *,
        price: TokenPrice | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        prompt: PromptVariant | None = None,
        run_ceiling: MicroUsd | None = None,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        validate_ceiling(run_ceiling, "run_ceiling")
        self._client = client
        self._clock = clock
        self._price = price or TokenPrice.free(client.model_id)
        self._prompt = prompt or DEFAULT_PROMPT
        self._max_turns = max_turns
        self._run_ceiling = run_ceiling
        # A variant offering list_directory is one written for a whole
        # repository. Its opening shows the top level and its reads are
        # numbered; every other variant behaves exactly as it was frozen.
        self._repository_mode = any(
            tool.name == LIST_DIRECTORY for tool in self._prompt.tools
        )

    @property
    def agent_id(self) -> str:
        return AGENT_ID

    @property
    def provider_id(self) -> str:
        return self._client.provider_id

    @property
    def model_id(self) -> str:
        return self._client.model_id

    @property
    def prompt_id(self) -> str:
        return self._prompt.prompt_id

    def edit(self, workspace: Path, task: TaskDefinition) -> AgentResult:
        invocations: list[ModelInvocation] = []
        try:
            return self._converse(workspace, task, invocations)
        except AgentError as error:
            # Re-raised with the turns that completed. The failed call itself
            # reported no usage, so it adds nothing; the earlier ones were
            # spent, and the record has to say so.
            raise AgentError(str(error), invocations=tuple(invocations)) from error

    def _converse(
        self,
        workspace: Path,
        task: TaskDefinition,
        invocations: list[ModelInvocation],
    ) -> AgentResult:
        """The loop. Appends each completed call to `invocations` as it goes."""
        space = _Workspace(root=workspace, hidden=frozenset(task.hidden_paths))
        messages: list[Message] = [
            Message(MessageRole.SYSTEM, self._prompt.system),
            Message(MessageRole.USER, self._opening(space, task)),
        ]
        wrote_anything = False
        tools_called = 0
        text_calls = 0
        waited_ms = 0

        def note(ending: str | None = None) -> str | None:
            return _note(
                text_calls,
                tools_called,
                wrote=wrote_anything,
                ending=ending,
                waited_ms=waited_ms,
            )

        for _ in range(self._max_turns):
            spent = total_micros(call.cost for call in invocations)
            if is_exhausted(spent, self._run_ceiling):
                # A stop rule, reported the way the turn budget is: the run
                # stays a measurement, and the money it spent stays visible.
                # A failure category here would assert the harness broke, take
                # the run out of the benchmark, and hide the spending that the
                # ceiling exists to bound.
                return AgentResult(
                    tuple(invocations),
                    outcome_detail=note(
                        f"stopped at the {self._run_ceiling} micro-USD "
                        f"run ceiling, having spent {spent}"
                    ),
                )

            response, invocation = self._call(messages)
            invocations.append(invocation)
            waited_ms += response.waited_ms

            tool_calls = response.tool_calls
            if not tool_calls:
                tool_calls = _tool_calls_in_text(response.text)
                if tool_calls:
                    text_calls += 1

            if not tool_calls:
                # The model answered in prose, which ends the loop either way.
                # What it means depends on whether it ever used a tool.
                #
                # malformed_output is a statement about the harness being
                # unable to read the model, and it is only true when nothing
                # was ever read. A model that listed files, read them and then
                # said it was finished has been understood perfectly; whether
                # that produced a patch is the score's business, and an empty
                # one classifies as no_patch_produced. Reporting that as
                # malformed instead blames the harness for a decision the
                # model made, and inflates the one rate that must not read
                # wrong.
                if tools_called or wrote_anything:
                    return AgentResult(tuple(invocations), outcome_detail=note())
                return AgentResult(
                    tuple(invocations),
                    outcome=AgentOutcome.MALFORMED_OUTPUT,
                    raw_reply=response.text,
                    outcome_detail=note("the model never called a tool"),
                )

            messages.append(
                Message(MessageRole.ASSISTANT, response.text, tool_calls=tool_calls)
            )
            for call in tool_calls:
                answer, wrote = self._run_tool(space, call)
                tools_called += 1
                wrote_anything = wrote_anything or wrote
                messages.append(
                    Message(
                        MessageRole.TOOL,
                        answer,
                        tool_name=call.name,
                        tool_call_id=call.call_id,
                    )
                )

        # Out of turns, which is only reachable by calling a tool on every one
        # of them: any turn that produced no call returned above. So the model
        # was read on every turn and this is never malformed -- it ran out of
        # budget, which the note records because the outcome cannot.
        return AgentResult(
            tuple(invocations),
            outcome_detail=note(f"stopped after the {self._max_turns} turn budget"),
        )

    def _opening(self, space: _Workspace, task: TaskDefinition) -> str:
        if self._repository_mode:
            top = space.list_directory(None)
            return f"{task.description.strip()}\n\nThe repository's top level:\n{top}"
        listing = "\n".join(space.visible_files())
        return f"{task.description.strip()}\n\nFiles you may read:\n{listing}"

    def _call(self, messages: list[Message]) -> tuple[ModelResponse, ModelInvocation]:
        started = self._clock.monotonic_ms()
        response = self._client.complete(tuple(messages), self._prompt.tools)
        return response, ModelInvocation(
            provider_id=self._client.provider_id,
            model_id=self._client.model_id,
            usage=response.usage,
            cost=cost_micros(response.usage, self._price),
            latency_ms=model_latency_ms(self._clock.monotonic_ms() - started, response),
        )

    def _run_tool(self, space: _Workspace, call: ToolCall) -> tuple[str, bool]:
        """Run one tool. Returns what to tell the model, and whether it wrote.

        Every failure the model can cause comes back as text it can read and
        retry. Raising here would end the run as a harness failure and remove
        it from the benchmark, which would quietly delete exactly the runs
        where a model handles tools badly.
        """
        arguments = call.arguments
        known = ", ".join(tool.name for tool in self._prompt.tools)
        if call.name not in {tool.name for tool in self._prompt.tools}:
            # Only what the variant offered runs. Under tools-v3 a remembered
            # list_files would dump a whole repository into the context, and
            # the measurement would be of a tool the prompt never described.
            return f"error: no tool named {call.name}. Available: {known}", False
        try:
            if call.name == LIST_FILES:
                return "\n".join(space.visible_files()), False
            if call.name == LIST_DIRECTORY:
                return space.list_directory(arguments.get("path")), False
            if call.name == SEARCH:
                found = space.search(arguments.get("text"), arguments.get("path"))
                return found, False
            if call.name == READ_FILE and self._repository_mode:
                lines = space.read_lines(
                    arguments.get("path"),
                    arguments.get("start_line"),
                    arguments.get("end_line"),
                )
                return lines, False
            if call.name == READ_FILE:
                return space.read(arguments.get("path")), False
            if call.name == WRITE_FILE:
                answer = space.write(
                    call.arguments.get("path"), call.arguments.get("content")
                )
                return answer, True
        except ToolError as error:
            return f"error: {error}", False
        except OSError as error:
            raise AgentError(f"{call.name} failed on the workspace: {error}") from error

        raise AssertionError(f"{call.name} is offered but has no implementation")


_TOOL_NAMES = frozenset(tool.name for tool in (*TOOLS, *REPOSITORY_TOOLS))


def _note(
    text_calls: int,
    tools_called: int,
    *,
    wrote: bool,
    ending: str | None = None,
    waited_ms: int = 0,
) -> str | None:
    """What to record about how the model worked, when there is anything to say.

    Silent on an ordinary successful run, deliberately. A note attached to
    every run is a field readers learn to skip, which is the same reason the
    image digest line is printed only when it has something to report.

    It speaks up for three things. How the model asked for its tools, because a
    model that never uses the structured channel is not comparable with one
    that does. How the loop ended, when the outcome cannot carry it -- running
    out of budget and stopping deliberately score identically. And how many
    tools were called when nothing was written, which is the whole difference
    between a model that investigated and declined and one that did nothing.
    """
    parts: list[str] = []
    if ending is not None:
        parts.append(ending)
    if text_calls:
        parts.append(
            f"{text_calls} turn(s) asked for tools as text rather than tool calls"
        )
    if tools_called and not wrote:
        parts.append(f"{tools_called} tool call(s) but no file written")
    waited = rate_limit_note(waited_ms)
    if waited is not None:
        parts.append(waited)
    return "; ".join(parts) if parts else None


def _tool_calls_in_text(text: str) -> tuple[ToolCall, ...]:
    """A tool call the model wrote as content instead of sending as one.

    Measured on 2026-09-16: `qwen2.5-coder:3b` advertises the `tools`
    capability, and Ollama returns `tool_calls: null` with
    `{"name": "list_files", "arguments": {}}` sitting in `content` -- for a
    minimal, correct request. Refusing to read that would measure the
    provider's template rather than the model, and would leave the loop
    untestable against the only model this project develops against.

    Deliberately narrow, so ordinary prose can never be mistaken for a call:
    the whole message must be one JSON object or a list of them, every entry
    must name a tool that exists, and arguments must be an object. Anything
    else is prose. Each use is counted and reported, so a model that only ever
    asks this way is visible in the data rather than quietly accommodated.

    One shape is refused on purpose rather than by omission. `qwen2.5-coder:3b`
    sends several JSON objects separated by newlines, which is not one JSON
    value, and D22 keeps it prose. The consequence is real and is stated there:
    that model's stored rows are a floor on what it asked for.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # A fenced block, which some templates wrap the call in.
        body = stripped.split("\n", 1)[-1]
        stripped = body.rsplit("```", 1)[0].strip()
    if not stripped.startswith(("{", "[")):
        return ()

    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return ()

    entries = decoded if isinstance(decoded, list) else [decoded]
    calls: list[ToolCall] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return ()
        name = entry.get("name")
        if not isinstance(name, str) or name not in _TOOL_NAMES:
            return ()
        arguments = entry.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return ()
        calls.append(ToolCall(name=name, arguments=dict(arguments)))
    return tuple(calls)


def describe_tools() -> str:
    """The tool schemas as JSON. Used by a test and by `af doctor`."""
    return json.dumps(
        [{"name": tool.name, "parameters": tool.parameters} for tool in TOOLS],
        indent=2,
    )
