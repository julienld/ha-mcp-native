"""Small bounded Streamable HTTP client for the exercised MCP 2026 subset.

Envelope/header spelling follows the repository's private MCP SDK 2.3.0
client/session.py::_make_modern_stamp and send_discover. No SDK 1.x handshake
or automatic downgrade is involved. MRTR and reconnect/resume are unsupported.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

VERSION = "2026-07-28"
MAX_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 2048
META = "io.modelcontextprotocol/"


class WireError(ValueError):
    """A fixed error category safe to include in a public receipt."""


def loopback_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise WireError("literal_loopback_http_required")
    return url


def parse_reply(content_type: str, body: bytes, request_id: int) -> dict[str, Any]:
    """Require a complete terminal reply with the exact request id.

    SSE comments, priming events and notifications are allowed. EOF is never
    invented as a successful result. A final event needs its blank delimiter.
    """
    if len(body) > MAX_BYTES:
        raise WireError("body_limit")
    try:
        text = body.decode("utf-8-sig")
        media = content_type.split(";", 1)[0].strip().lower()
        if media == "application/json":
            messages = [json.loads(text)]
        elif media == "text/event-stream":
            messages = _sse_messages(text)
        else:
            raise WireError("unexpected_content_type")
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WireError("malformed_payload") from error
    return _terminal_reply(messages, request_id)


def _terminal_reply(messages: list[Any], request_id: int) -> dict[str, Any]:
    replies = []
    for message in messages:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise WireError("invalid_rpc_envelope")
        if "method" in message:
            if "id" in message:
                raise WireError("server_request_not_supported")
            continue
        if type(message.get("id")) is not int or message["id"] != request_id:
            raise WireError("reply_id_mismatch")
        if ("result" in message) == ("error" in message):
            raise WireError("invalid_terminal_reply")
        replies.append(message)
    if len(replies) != 1:
        raise WireError("missing_or_duplicate_terminal_reply")
    return replies[0]


def _sse_messages(text: str) -> list[Any]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    events = normalized.split("\n\n")
    if len(events) > MAX_EVENTS:
        raise WireError("event_limit")
    if events[-1].strip():
        raise WireError("truncated_sse_event")
    messages = []
    for event in events[:-1]:
        data = []
        for line in event.split("\n"):
            field, _, value = line.partition(":")
            if field == "data":
                data.append(value.removeprefix(" "))
        payload = "\n".join(data)
        if payload.strip():
            messages.append(json.loads(payload))
    return messages


def modern_request(
    request_id: int, method: str, params: dict[str, Any], version: str
) -> tuple[dict[str, str], dict[str, Any]]:
    metadata = {
        META + "protocolVersion": version,
        META + "clientCapabilities": {},
        META + "clientInfo": {"name": "paired-protocol-probe", "version": "1"},
    }
    headers = {"Mcp-Protocol-Version": version, "Mcp-Method": method}
    if method == "tools/call":
        name = params["name"]
        if not name.isascii() or not name.replace("_", "").isalnum():
            raise WireError("probe_requires_ascii_tool_name")
        headers["Mcp-Name"] = name
    return headers, {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**params, "_meta": metadata},
    }


@dataclass
class Observation:
    status: int
    media: str
    duration_ms: float
    body_bytes: int
    reply: dict[str, Any] | None
    failure: str | None = None

    def public(self) -> dict[str, Any]:
        error = (self.reply or {}).get("error", {})
        return {
            "status": self.status,
            "media": self.media,
            "duration_ms": round(self.duration_ms, 4),
            "body_bytes": self.body_bytes,
            "failure": self.failure,
            "rpc_error_code": error.get("code") if isinstance(error, dict) else None,
        }

    def result(self) -> dict[str, Any]:
        if (
            self.failure
            or self.status != 200
            or not self.reply
            or "error" in self.reply
        ):
            raise WireError("request_failed")
        result = self.reply.get("result")
        if not isinstance(result, dict):
            raise WireError("result_not_object")
        return result


class Client:
    def __init__(self, http: httpx.AsyncClient, url: str, token: str) -> None:
        self.http, self.url, self.token = http, loopback_url(url), token
        self.ids = itertools.count(1)
        self.observations: deque[dict[str, Any]] = deque(maxlen=128)

    async def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        version: str = VERSION,
        legacy: bool = False,
    ) -> Observation:
        request_id = next(self.ids)
        headers, body = modern_request(request_id, method, params or {}, version)
        if legacy:
            body["params"] = params or {}
            headers = {"Mcp-Protocol-Version": version}
        headers.update(
            {
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer " + self.token,
            }
        )
        start = time.perf_counter()
        status, media, payload, reply, failure = 0, "", bytearray(), None, None
        try:
            async with (
                asyncio.timeout(30),
                self.http.stream(
                    "POST", self.url, headers=headers, json=body
                ) as response,
            ):
                status = response.status_code
                media = response.headers.get("content-type", "").split(";", 1)[0]
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_BYTES:
                        raise WireError("body_limit")
                reply = parse_reply(media, bytes(payload), request_id)
        except WireError as error:
            failure = str(error)
        except (httpx.HTTPError, TimeoutError) as error:
            # Exception text often embeds the secret path; retain only its type.
            failure = type(error).__name__
        observation = Observation(
            status,
            media,
            (time.perf_counter() - start) * 1000,
            len(payload),
            reply,
            failure,
        )
        self.observations.append(
            {"method": method, "version": version, **observation.public()}
        )
        return observation


def tool_payload(observation: Observation) -> dict[str, Any]:
    result = observation.result()
    if result.get("isError"):
        raise WireError("tool_error")
    payload = result.get("structuredContent")
    if payload is None:
        text = [
            b.get("text") for b in result.get("content", []) if b.get("type") == "text"
        ]
        if len(text) != 1:
            raise WireError("expected_one_text_payload")
        try:
            payload = json.loads(text[0])
        except (TypeError, json.JSONDecodeError) as error:
            raise WireError("tool_payload_not_json") from error
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise WireError("tool_payload_failed")
    return payload
