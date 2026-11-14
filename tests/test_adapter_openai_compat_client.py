"""The OpenAI-compatible client, against a local HTTP server.

A real server in a thread rather than a patched `urlopen`, so the request the
client builds is the one that crosses a socket: the path, the headers and the
body are all read back as the server received them.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from control_plane.adapters.openai_compat_client import OpenAICompatClient
from control_plane.domain.model import Message, MessageRole, ToolCall, ToolSpec
from control_plane.services.errors import AgentError
from tests.openai_support import FakeServer, Reply, completion, fake_openai_server

KEY = "sk-test-not-a-real-key"


@pytest.fixture
def server() -> Iterator[FakeServer]:
    with fake_openai_server() as running:
        yield running


class FakeTime:
    """A sleep that only moves a clock, so a 30 s wait takes no time."""

    def __init__(self) -> None:
        self.now_ms = 0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now_ms += int(seconds * 1000)

    def monotonic_ms(self) -> int:
        return self.now_ms


def client(server: FakeServer, time: FakeTime | None = None, **kwargs: Any) -> Any:
    clock = time or FakeTime()
    return OpenAICompatClient(
        "big-model-70b",
        base_url=server.url,
        api_key=KEY,
        provider_id="example-tier",
        sleep=clock.sleep,
        monotonic_ms=clock.monotonic_ms,
        **kwargs,
    )


LIST = ToolSpec("list_files", "List the files.", {"type": "object", "properties": {}})


# --- the request --------------------------------------------------------------


def test_the_request_is_a_chat_completion_with_the_key_as_a_bearer_token(
    server: FakeServer,
) -> None:
    server.replies.append(Reply(body=completion()))

    client(server).complete(
        (Message(MessageRole.SYSTEM, "be brief"), Message(MessageRole.USER, "hi")),
        (LIST,),
    )

    (request,) = server.received
    assert request["path"] == "/v1/chat/completions"
    assert request["authorization"] == f"Bearer {KEY}"
    body = request["body"]
    assert body["model"] == "big-model-70b"
    assert (body["temperature"], body["seed"]) == (0, 0)
    assert body["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List the files.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def test_no_tools_key_is_sent_when_none_are_offered(server: FakeServer) -> None:
    server.replies.append(Reply(body=completion()))

    client(server).complete((Message(MessageRole.USER, "hi"),))

    assert "tools" not in server.received[0]["body"]


# --- tool calls ---------------------------------------------------------------


def test_a_tool_call_is_decoded_with_its_identifier_and_arguments(
    server: FakeServer,
) -> None:
    server.replies.append(
        Reply(
            body=completion(
                None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "lru/cache.py"}',
                        },
                    }
                ],
            )
        )
    )

    response = client(server).complete((Message(MessageRole.USER, "go"),), (LIST,))

    assert response.text == ""
    assert response.tool_calls == (
        ToolCall("read_file", {"path": "lru/cache.py"}, call_id="call_1"),
    )


def test_arguments_that_are_not_a_json_object_reach_the_agent_as_empty(
    server: FakeServer,
) -> None:
    """The model wrote bad arguments; the tool's error tells it so."""
    server.replies.append(
        Reply(
            body=completion(
                None,
                tool_calls=[
                    {"id": "c", "function": {"name": "read_file", "arguments": "{oops"}}
                ],
            )
        )
    )

    response = client(server).complete((Message(MessageRole.USER, "go"),), (LIST,))

    assert response.tool_calls[0].arguments == {}


def test_a_call_and_its_answer_are_replayed_under_one_identifier(
    server: FakeServer,
) -> None:
    server.replies.append(Reply(body=completion()))
    call = ToolCall("read_file", {"path": "a.py"}, call_id="call_9")

    client(server).complete(
        (
            Message(MessageRole.USER, "go"),
            Message(MessageRole.ASSISTANT, "", tool_calls=(call,)),
            Message(
                MessageRole.TOOL,
                "print(1)",
                tool_name="read_file",
                tool_call_id="call_9",
            ),
        )
    )

    assistant, answer = server.received[0]["body"]["messages"][1:]
    assert assistant["tool_calls"] == [
        {
            "id": "call_9",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
        }
    ]
    assert answer == {"role": "tool", "tool_call_id": "call_9", "content": "print(1)"}


def test_the_answer_to_a_call_written_as_text_goes_back_as_a_user_message(
    server: FakeServer,
) -> None:
    """No identifier was issued, and the API refuses an answer to an unknown one."""
    server.replies.append(Reply(body=completion()))
    text_call = ToolCall("list_files", {})

    client(server).complete(
        (
            Message(MessageRole.USER, "go"),
            Message(MessageRole.ASSISTANT, '{"name": "list_files"}', (text_call,)),
            Message(MessageRole.TOOL, "a.py", tool_name="list_files"),
        )
    )

    assistant, answer = server.received[0]["body"]["messages"][1:]
    assert "tool_calls" not in assistant
    assert answer == {"role": "user", "content": "Result of list_files:\na.py"}


# --- usage and truncation -----------------------------------------------------


def test_cached_prompt_tokens_are_a_subset_of_the_prompt(server: FakeServer) -> None:
    server.replies.append(
        Reply(
            body=completion(
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 7,
                    "prompt_tokens_details": {"cached_tokens": 60},
                }
            )
        )
    )

    usage = client(server).complete((Message(MessageRole.USER, "hi"),)).usage

    assert (usage.input_tokens, usage.cache_read_tokens) == (40, 60)
    assert usage.output_tokens == 7


def test_a_reply_cut_off_by_the_output_cap_is_truncated(server: FakeServer) -> None:
    server.replies.append(Reply(body=completion(finish_reason="length")))

    assert client(server).complete((Message(MessageRole.USER, "hi"),)).truncated


def test_a_reply_without_usage_is_refused_rather_than_counted_as_zero(
    server: FakeServer,
) -> None:
    body = completion()
    del body["usage"]
    server.replies.append(Reply(body=body))

    with pytest.raises(AgentError, match="no usage"):
        client(server).complete((Message(MessageRole.USER, "hi"),))


def test_a_body_that_is_not_json_is_an_agent_error(server: FakeServer) -> None:
    server.replies.append(Reply(body=b"<html>gateway</html>"))

    with pytest.raises(AgentError, match="not JSON"):
        client(server).complete((Message(MessageRole.USER, "hi"),))


# --- rate limits --------------------------------------------------------------


def test_a_429_is_waited_out_and_the_wait_is_reported_apart(
    server: FakeServer,
) -> None:
    time = FakeTime()
    server.replies.extend(
        [
            Reply(
                status=429, body={"error": "slow down"}, headers={"Retry-After": "2"}
            ),
            Reply(body=completion()),
        ]
    )

    response = client(server, time).complete((Message(MessageRole.USER, "hi"),))

    assert time.slept == [2.0]
    assert response.waited_ms == 2_000
    assert len(server.received) == 2


def test_a_429_without_retry_after_backs_off_and_doubles(server: FakeServer) -> None:
    time = FakeTime()
    server.replies.extend(
        [
            Reply(status=429, body={}),
            Reply(status=429, body={}),
            Reply(body=completion()),
        ]
    )

    response = client(server, time).complete((Message(MessageRole.USER, "hi"),))

    assert time.slept == [5.0, 10.0]
    assert response.waited_ms == 15_000


def test_a_tier_that_keeps_refusing_fails_the_call_after_its_attempts(
    server: FakeServer,
) -> None:
    server.replies.extend(
        Reply(status=429, body={}, headers={"Retry-After": "1"}) for _ in range(3)
    )

    with pytest.raises(AgentError, match="refused 3 request") as raised:
        client(server, rate_limit_attempts=3).complete(
            (Message(MessageRole.USER, "hi"),)
        )
    assert KEY not in str(raised.value)


def test_a_wait_longer_than_allowed_is_refused_instead_of_slept(
    server: FakeServer,
) -> None:
    time = FakeTime()
    server.replies.append(Reply(status=429, body={}, headers={"Retry-After": "3600"}))

    with pytest.raises(AgentError, match="3600 s wait"):
        client(server, time).complete((Message(MessageRole.USER, "hi"),))
    assert time.slept == []


# --- the key ------------------------------------------------------------------


def test_an_error_names_the_status_and_never_the_key(server: FakeServer) -> None:
    server.replies.append(Reply(status=401, body={"error": "bad credentials"}))

    with pytest.raises(AgentError) as raised:
        client(server).complete((Message(MessageRole.USER, "hi"),))

    assert "401" in str(raised.value)
    assert KEY not in str(raised.value)


def test_no_authorization_header_is_sent_without_a_key(server: FakeServer) -> None:
    """A local llama.cpp or vLLM server takes no key."""
    server.replies.append(Reply(body=completion()))
    keyless = OpenAICompatClient(
        "local", base_url=server.url, api_key=None, provider_id="llama-cpp"
    )

    keyless.complete((Message(MessageRole.USER, "hi"),))

    assert server.received[0]["authorization"] is None
