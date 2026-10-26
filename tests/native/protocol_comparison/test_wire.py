"""Harness tests only. None of these qualify a Home Assistant build."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from .wire import MAX_BYTES, VERSION, Client, WireError, loopback_url, parse_reply


def reply_bytes(**extra: Any) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 7, "result": {"text": "café\u2028✓"}, **extra},
        ensure_ascii=False,
    ).encode()


@pytest.mark.parametrize("line_end", [b"\n", b"\r\n", b"\r"])
def test_multiline_sse_preserves_unicode_and_ignores_priming(line_end: bytes) -> None:
    body = line_end.join(
        [
            b": keepalive",
            b"",
            b"id: 0",
            b"data:",
            b"",
            b"event: message",
            b'data: {"jsonrpc":"2.0",',
            b'data: "method":"notifications/progress","params":{}}',
            b"",
            b"data: " + reply_bytes(),
            b"",
            b"",
        ]
    )
    assert (
        parse_reply("text/event-stream; charset=utf-8", body, 7)["result"]["text"]
        == "café\u2028✓"
    )


def test_json_error_retains_numeric_code_without_success_invention() -> None:
    body = b'{"jsonrpc":"2.0","id":7,"error":{"code":-32022,"message":"private"}}'
    parsed = parse_reply("application/json", body, 7)
    assert parsed["error"]["code"] == -32022
    assert "result" not in parsed


@pytest.mark.parametrize(
    ("body", "category"),
    [
        (b"", "missing_or_duplicate_terminal_reply"),
        (b": heartbeat\n\n", "missing_or_duplicate_terminal_reply"),
        (b'data: {"jsonrpc":"2.0",\n\n', "malformed_payload"),
        (b"data: " + reply_bytes() + b"\n", "truncated_sse_event"),
        (b"data: " + reply_bytes(id=8) + b"\n\n", "reply_id_mismatch"),
        (
            (b"data: " + reply_bytes() + b"\n\n") * 2,
            "missing_or_duplicate_terminal_reply",
        ),
        (
            b'data: {"jsonrpc":"2.0","id":7,"method":"elicitation/create"}\n\n',
            "server_request_not_supported",
        ),
        (b'data: {"jsonrpc":"2.0","id":7}\n\n', "invalid_terminal_reply"),
    ],
)
def test_stream_loss_and_unexercised_roundtrips_never_count_as_success(
    body: bytes, category: str
) -> None:
    with pytest.raises(WireError, match=category):
        parse_reply("text/event-stream", body, 7)


def test_overlong_body_is_rejected_before_json_decoding() -> None:
    with pytest.raises(WireError, match="body_limit"):
        parse_reply("application/json", b" " * (MAX_BYTES + 1), 7)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/mcp",
        "http://localhost/mcp",
        "http://192.168.1.1/mcp",
        "http://user:secret@127.0.0.1/mcp",
    ],
)
def test_probe_rejects_nonliteral_loopback_and_embedded_credentials(url: str) -> None:
    with pytest.raises(WireError, match="literal_loopback_http_required"):
        loopback_url(url)


async def test_same_wire_client_sends_modern_headers_and_reads_real_http() -> None:
    """Actual loopback socket, but a fixture server: proves client wiring only."""
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append((dict(self.headers), body))
            data = (
                b"data: "
                + json.dumps(
                    {"jsonrpc": "2.0", "id": body["id"], "result": {"ok": True}}
                ).encode()
                + b"\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            client = Client(
                http, f"http://127.0.0.1:{server.server_port}/fixture", "test-secret"
            )
            observation = await client.rpc(
                "tools/call", {"name": "ha_get_state", "arguments": {}}
            )
            assert observation.result() == {"ok": True}
            public = json.dumps(observation.public())
            assert "test-secret" not in public and "/fixture" not in public
        headers, body = captured[0]
        assert headers["Mcp-Protocol-Version"] == VERSION
        assert (
            headers["Mcp-Method"] == "tools/call"
            and headers["Mcp-Name"] == "ha_get_state"
        )
        assert (
            body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"]
            == VERSION
        )
        assert "initialize" not in json.dumps(body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
