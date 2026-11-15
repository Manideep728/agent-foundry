"""A local server that answers like the OpenAI chat-completions API."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Reply:
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeServer:
    url: str
    replies: list[Reply]
    received: list[dict[str, Any]]


def completion(
    content: str | None = "ok",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 12, "completion_tokens": 3},
    }


@contextmanager
def fake_openai_server() -> Iterator[FakeServer]:
    """Serves `replies` in order and records every request it received."""
    replies: list[Reply] = []
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            received.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "user_agent": self.headers.get("User-Agent"),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            reply = replies.pop(0)
            if isinstance(reply.body, bytes):
                raw = reply.body
            else:
                raw = json.dumps(reply.body).encode()
            self.send_response(reply.status)
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield FakeServer(f"http://127.0.0.1:{httpd.server_port}/v1", replies, received)
    finally:
        httpd.shutdown()
        httpd.server_close()
