"""Keep WebSocket service failures distinct from HTTP responses."""

from .rest_client import HomeAssistantAPIError


class HomeAssistantWebSocketServiceError(HomeAssistantAPIError):
    """A service-adapter failure with an optional authoritative Core error code.

    The inherited 400 preserves existing service-error handling. It is synthetic,
    so write-outcome consumers must use the native code instead of that status.
    """

    def __init__(self, message: str, *, code: object) -> None:
        super().__init__(
            f"API error: 400 - {message}",
            status_code=400,
            response_data={"message": message},
        )
        self.code = code if isinstance(code, str) else None
