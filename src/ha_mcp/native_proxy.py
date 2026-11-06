"""Optional external MCP bridge to the canonical Home Assistant endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp.server.providers.proxy import FastMCPProxy


def create_native_component_proxy(url: str, auth: Any = None) -> FastMCPProxy:
    """Relay an explicit native MCP URL using the SDK's normal proxy.

    ``auth`` is a bearer token or an authentication object accepted by the SDK's
    StreamableHttpTransport. No HA client, URL discovery, legacy middleware or
    fallback is installed. Use the native ``/readonly`` URL for a read-only
    bridge. Background MCP tasks require connecting to the native endpoint
    directly; the SDK proxy deliberately disables that capability.
    """
    from ha_mcp._vendor.fastmcp.client.transports import StreamableHttpTransport
    from ha_mcp._vendor.fastmcp.server import create_proxy
    from ha_mcp._vendor.fastmcp.server.providers.proxy import ProxyProvider

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("An explicit absolute native MCP HTTP URL is required")
    proxy = create_proxy(
        StreamableHttpTransport(url, auth=auth),
        name="Home Assistant native bridge",
        provider_error_strategy="raise",
    )
    # create_proxy does not expose its provider cache setting. Its unrelated
    # server cache_ttl describes protocol hints and cannot be zero. This pinned
    # provider instance must fetch live tool/policy metadata for every lookup.
    for provider in proxy.providers:
        if isinstance(provider, ProxyProvider):
            provider._cache_ttl = 0
    return proxy
