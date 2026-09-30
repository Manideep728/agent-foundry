"""The Ollama client, against a real HTTP server serving canned replies.

Not against a live model. CI has no GPU and no model, so every rule this
adapter enforces -- the usage arithmetic above all -- has to be provable
without one. A real socket rather than a patched urlopen, because the failures
worth catching here are HTTP failures.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any, ClassVar

import pytest

from control_plane.adapters.ollama_client import OllamaClient, _decode
from control_plane.domain.model import Message, MessageRole
from control_plane.services.errors import AgentError
from tests.ollama_support import MODEL as LIVE_MODEL
from tests.ollama_support import requires_ollama


class Canned:
    """What the next request gets, and what the last request was."""

    status = 200
    body: bytes = b"{}"
    received: ClassVar[dict[str, Any]] = {}
    paths: ClassVar[list[str]] = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        Canned.received = json.loads(self.rfile.read(length))
        Canned.paths.append(self.path)
        self.send_response(Canned.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(Canned.body)))
        self.end_headers()
        self.wfile.write(Canned.body)

    def log_message(self, *args: Any) -> None:
        """Silence the per-request line; pytest output is the report here."""


@pytest.fixture(name="base_url")
def base_url_fixture() -> Iterator[str]:
    # Port 0: the kernel picks a free one, so parallel runs cannot collide.
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Canned.status = 200
    Canned.body = b"{}"
    Canned.paths = []
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def reply(**overrides: Any) -> None:
    body = {
        "message": {"role": "assistant", "content": "hello"},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 100,
        "prompt_eval_cached_count": 0,
        "eval_count": 20,
    }
    body.update(overrides)
    Canned.body = json.dumps(body).encode("utf-8")


ASK = (Message(MessageRole.USER, "fix it"),)


def test_a_reply_carries_its_text_and_usage(base_url: str) -> None:
    reply()

    response = OllamaClient("m", base_url=base_url).complete(ASK)

    assert response.text == "hello"
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 20
    assert response.usage.cache_read_tokens == 0
    assert response.truncated is False


def test_cached_prompt_tokens_are_a_subset_not_an_extra() -> None:
    """The arithmetic that stops every cached run reporting twice its input.

    Measured against the real server: an identical second call reported
    prompt_eval_count 271 with prompt_eval_cached_count 270. The count is the
    whole prompt and the cached figure is part of it. Treat them as separate
    additions and a 271-token prompt is recorded as 541.
    """
    usage = _decode(
        {
            "message": {"content": "hi"},
            "prompt_eval_count": 271,
            "prompt_eval_cached_count": 270,
            "eval_count": 8,
        }
    ).usage

    assert usage.input_tokens == 1
    assert usage.cache_read_tokens == 270
    # The whole prompt, counted once.
    assert usage.input_tokens + usage.cache_read_tokens == 271
    assert usage.total_tokens == 279


def test_a_reply_that_reused_the_prompt_cache_is_refused(base_url: str) -> None:
    """A reply that depends on an earlier request cannot be reproduced.

    Measured: the same request after two different predecessors produced two
    different replies at temperature 0. The client asks for a cold model every
    time, so cached tokens mean that request was not honoured.
    """
    reply(prompt_eval_count=271, prompt_eval_cached_count=270)

    with pytest.raises(AgentError, match="reused 270 cached prompt tokens"):
        OllamaClient("m", base_url=base_url).complete(ASK)


def test_every_request_runs_on_a_model_loaded_for_it_alone(base_url: str) -> None:
    """Unloaded before, so nothing another client left cached is reused.

    Unloaded after too, with zero rather than a short duration, so this
    client leaves nothing cached for anyone else. Measured 2026-09-29: a cold
    request straight after another client's warm one still found the cache,
    because `keep_alive` on a request only governs what happens after it.
    """
    reply()

    OllamaClient("m", base_url=base_url).complete(ASK)

    assert Canned.paths == ["/api/generate", "/api/chat"]
    assert Canned.received["keep_alive"] == 0


def test_more_cached_tokens_than_prompt_tokens_is_refused(base_url: str) -> None:
    """A subset cannot exceed its set. Without this the difference goes
    negative and ModelUsage raises somewhere far from the cause."""
    reply(prompt_eval_count=10, prompt_eval_cached_count=11)

    with pytest.raises(AgentError, match="cannot be a subset"):
        OllamaClient("m", base_url=base_url).complete(ASK)


def test_hitting_the_output_cap_is_reported_as_truncated(base_url: str) -> None:
    reply(done_reason="length")

    assert OllamaClient("m", base_url=base_url).complete(ASK).truncated is True


def test_missing_counters_are_zero_not_an_error(base_url: str) -> None:
    """Ollama omits counters it has nothing to report for."""
    Canned.body = json.dumps({"message": {"content": "hi"}}).encode("utf-8")

    usage = OllamaClient("m", base_url=base_url).complete(ASK).usage

    assert usage.total_tokens == 0


def test_a_non_integer_counter_is_refused(base_url: str) -> None:
    """A string where a count belongs means the response is not the shape this
    adapter was written against. Coercing it would invent a measurement."""
    reply(eval_count="twenty")

    with pytest.raises(AgentError, match="non-integer eval_count"):
        OllamaClient("m", base_url=base_url).complete(ASK)


def test_an_http_error_keeps_the_body(base_url: str) -> None:
    """The body names the real problem, usually an unpulled model."""
    Canned.status = 404
    Canned.body = b'{"error":"model \'nope\' not found"}'

    with pytest.raises(AgentError, match="not found"):
        OllamaClient("nope", base_url=base_url).complete(ASK)


def test_a_body_that_is_not_json_is_an_agent_error(base_url: str) -> None:
    Canned.body = b"<html>proxy error</html>"

    with pytest.raises(AgentError, match="not JSON"):
        OllamaClient("m", base_url=base_url).complete(ASK)


def test_a_reply_with_no_content_is_an_agent_error(base_url: str) -> None:
    Canned.body = json.dumps({"done": True}).encode("utf-8")

    with pytest.raises(AgentError, match="no message content"):
        OllamaClient("m", base_url=base_url).complete(ASK)


def test_an_unreachable_server_is_an_agent_error() -> None:
    """A dead server is a harness fault, not a model result. It must not reach
    scoring as a measurement."""
    client = OllamaClient("m", base_url="http://127.0.0.1:1", timeout_s=2.0)

    with pytest.raises(AgentError, match="could not reach ollama"):
        client.complete(ASK)


def test_the_request_pins_the_sampler_and_sends_every_message(
    base_url: str,
) -> None:
    """Temperature zero and a fixed seed, or the benchmark measures the
    sampler. Streaming off, or the body is newline-delimited chunks."""
    reply()

    OllamaClient("m", base_url=base_url, seed=7).complete(
        (
            Message(MessageRole.SYSTEM, "you fix repositories"),
            Message(MessageRole.USER, "fix it"),
        )
    )

    assert Canned.received["model"] == "m"
    assert Canned.received["stream"] is False
    assert Canned.received["options"]["temperature"] == 0.0
    assert Canned.received["options"]["seed"] == 7
    assert [m["role"] for m in Canned.received["messages"]] == ["system", "user"]
    assert Canned.received["messages"][1]["content"] == "fix it"


# --- against a real server ---------------------------------------------------


@pytest.mark.ollama
@requires_ollama
def test_a_real_server_answers_in_the_shape_this_adapter_expects() -> None:
    """The one claim the canned server cannot make.

    Everything above proves what the adapter does with a response. This proves
    the response is real: that a live Ollama still returns `message.content`
    and counters where this adapter looks for them. A field renamed upstream
    would pass every test above and report zero tokens forever.
    """
    client = OllamaClient(LIVE_MODEL, num_predict=8)

    response = client.complete((Message(MessageRole.USER, "Reply with: ok"),))

    assert response.text.strip() != ""
    assert response.usage.output_tokens > 0
    # The prompt was counted somewhere, cached or not.
    assert response.usage.input_tokens + response.usage.cache_read_tokens > 0


@pytest.mark.ollama
@requires_ollama
def test_a_real_server_starts_every_request_cold() -> None:
    """The claim `keep_alive: 0` rests on, checked where it can fail.

    An identical second request is the case most likely to hit the cache: the
    whole prompt would be reusable. A server that ignored `keep_alive` would
    report it cached, and the client would refuse the reply.
    """
    client = OllamaClient(LIVE_MODEL, num_predict=8)
    ask = (Message(MessageRole.USER, "Reply with: ok"),)

    first = client.complete(ask)
    second = client.complete(ask)

    assert first.usage.cache_read_tokens == 0
    assert second.usage.cache_read_tokens == 0
