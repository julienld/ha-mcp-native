"""Owned HTTP/WS exchange with one resolved app on Core's existing session."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from ha_mcp._vendor.websockets.asyncio.client import connect
from ha_mcp._vendor.websockets.exceptions import ConnectionClosedOK
from ha_mcp.runtime.app_proxy_results import _MAX_RESPONSE_SIZE
from ha_mcp.tools.coercion import ANSI_ESCAPE_RE

if TYPE_CHECKING:
    from .app_routes import AppRoute

# HTTP needs room for an array_patch's full before-image, while the public
# projection remains 50 KiB. Reject a larger image rather than edit a prefix.
MAX_HTTP_BYTES = 8 * 1024 * 1024
MAX_WS_MESSAGE_BYTES = 5 * 1024 * 1024
MAX_WS_MESSAGES = 1000


class SingleAttemptWebSocket(connect):
    """An opaque app's handshake must not be repeated after a redirect."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc


async def http_exchange(
    route: AppRoute, method: str, body: Any
) -> tuple[int, dict[str, str], Any]:
    await route.open()
    content = None
    if body is not None:
        content = (body if isinstance(body, str) else json.dumps(body)).encode()
        route.headers["Content-Type"] = "application/json"
    attempted = False

    async def once(request: Any, handler: Any) -> Any:
        nonlocal attempted
        if attempted:
            raise RuntimeError("An opaque app request cannot be retried")
        attempted = True
        return await handler(request)

    async with route.session.request(
        method,
        route.url,
        headers=route.headers,
        data=content,
        allow_redirects=False,
        timeout=aiohttp.ClientTimeout(total=30),
        middlewares=(once,),
    ) as response:
        chunks = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            chunks.extend(chunk)
            if len(chunks) > MAX_HTTP_BYTES:
                raise ValueError("App response exceeds the bounded input limit")
        text = bytes(chunks).decode(response.charset or "utf-8", errors="replace")
        value: Any = text
        if "application/json" in response.headers.get("Content-Type", "").lower():
            try:
                value = json.loads(text)
            except ValueError:
                pass
        if response.status >= 400:
            # The opaque app may have changed state before replying with an
            # error. Never retry or expose echoed ingress credentials.
            raise ValueError(f"App returned HTTP {response.status}")
        headers = {
            key.lower(): val
            for key, val in response.headers.items()
            if key.lower() not in {"set-cookie", "authorization", "proxy-authorization"}
        }
        return response.status, headers, value


async def ws_exchange(
    route: AppRoute, body: Any, limit: int | None, offset: int, wait_for_close: bool
) -> tuple[list[str], int, str, float, int]:
    await route.open()
    cap = (
        MAX_WS_MESSAGES
        if limit is None
        else min(MAX_WS_MESSAGES, max(0, offset) + max(0, limit))
    )
    budget = 120 if wait_for_close else 10
    started = time.monotonic()
    assert route.url is not None
    ws_url = route.url.with_scheme("wss" if route.url.scheme == "https" else "ws")
    # aiohttp.ws_connect cannot disable its automatic GET retry per call.
    # Core still owns the URL and the actor's Supervisor ingress session.
    async with SingleAttemptWebSocket(
        str(ws_url),
        additional_headers=route.headers,
        proxy=None,
        max_size=MAX_WS_MESSAGE_BYTES,
        open_timeout=10,
        close_timeout=5,
    ) as socket:
        # Socket establishment can await: revoke a queued initial command
        # before it is sent, while keeping the connection owned until close.
        await route.service.admit()
        route.pin()
        if body is not None:
            await socket.send(json.dumps(body) if isinstance(body, dict) else str(body))
        messages, size, reason = await _collect(
            socket, cap, budget, started, wait_for_close, limit is not None
        )
    return messages, size, reason, round(time.monotonic() - started, 2), cap


async def _collect(
    socket: Any,
    cap: int,
    budget: float,
    started: float,
    wait_for_close: bool,
    caller_capped: bool,
) -> tuple[list[str], int, str]:
    messages: list[str] = []
    size = 0
    while True:
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            return messages, size, "timeout"
        if len(messages) >= cap:
            return (
                messages,
                size,
                "message_limit" if caller_capped else "safety_ceiling",
            )
        if size >= _MAX_RESPONSE_SIZE:
            return messages, size, "size_limit"
        try:
            message = await asyncio.wait_for(
                socket.recv(), remaining if wait_for_close else min(remaining, 2)
            )
        except TimeoutError:
            return messages, size, "timeout" if wait_for_close else "silence"
        except ConnectionClosedOK:
            return messages, size, "server_closed"
        if isinstance(message, str):
            clean = ANSI_ESCAPE_RE.sub("", message)
            messages.append(clean)
            size += len(clean)
