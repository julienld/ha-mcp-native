"""Compatibility exports for the shared service-attempt error types."""

from ..errors import (
    HomeAssistantCommandOutcomeUnknown,
    HomeAssistantServiceAPIOutcomeUnknown,
    HomeAssistantServiceAuthOutcomeUnknown,
    HomeAssistantServiceConnectionOutcomeUnknown,
    HomeAssistantWebSocketServiceAuthError,
    HomeAssistantWebSocketServiceError,
    is_service_outcome_unknown,
)

__all__ = [
    "HomeAssistantCommandOutcomeUnknown",
    "HomeAssistantServiceConnectionOutcomeUnknown",
    "HomeAssistantServiceAPIOutcomeUnknown",
    "HomeAssistantServiceAuthOutcomeUnknown",
    "HomeAssistantWebSocketServiceAuthError",
    "HomeAssistantWebSocketServiceError",
    "is_service_outcome_unknown",
]
