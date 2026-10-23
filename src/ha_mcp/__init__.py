"""
Home Assistant MCP Server

A Model Context Protocol server that provides complete control over Home Assistant
through REST API and WebSocket integration.
"""

# MCP logging uses its own private Rich copy; importing this library must not
# patch Home Assistant's shared RichHandler.
from ._version import get_version

__version__ = get_version()
__author__ = "Julien"
__license__ = "MIT"

from typing import TYPE_CHECKING, Any

from ._lazy_exports import resolve_export
from .errors import (
    ErrorCode,
    create_auth_error,
    create_config_error,
    create_connection_error,
    create_entity_not_found_error,
    create_entity_unavailable_after_dispatch_error,
    create_error_response,
    create_service_error,
    create_timeout_error,
    create_validation_error,
    get_error_code,
    get_error_message,
    is_error_response,
)

if TYPE_CHECKING:
    from .auth import HomeAssistantOAuthProvider
    from .client.rest_client import HomeAssistantClient
    from .config import Settings
    from .server import HomeAssistantSmartMCPServer

# The integration imports the tool API without constructing the standalone
# transport stack. Keep the historical package exports available on demand.
_LAZY_EXPORTS = {
    "Settings": (".config", "Settings"),
    "HomeAssistantClient": (".client.rest_client", "HomeAssistantClient"),
    "HomeAssistantSmartMCPServer": (".server", "HomeAssistantSmartMCPServer"),
    "HomeAssistantOAuthProvider": (".auth", "HomeAssistantOAuthProvider"),
}


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _LAZY_EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_EXPORTS.keys())


__all__ = [
    "Settings",
    "HomeAssistantClient",
    "HomeAssistantSmartMCPServer",
    "HomeAssistantOAuthProvider",
    # Error handling exports
    "ErrorCode",
    "create_error_response",
    "create_connection_error",
    "create_auth_error",
    "create_entity_not_found_error",
    "create_entity_unavailable_after_dispatch_error",
    "create_service_error",
    "create_validation_error",
    "create_config_error",
    "create_timeout_error",
    "is_error_response",
    "get_error_code",
    "get_error_message",
]
