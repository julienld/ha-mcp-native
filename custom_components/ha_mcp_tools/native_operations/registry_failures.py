"""A registry callback failure cannot establish whether its effects landed."""

from typing import NoReturn

from ha_mcp.tool_api import ToolFailure


def registry_outcome_unknown(domain: str, action: str, error: Exception) -> NoReturn:
    raise ToolFailure(
        "OUTCOME_UNKNOWN",
        f"Core raised {type(error).__name__} after the {domain} registry {action} was dispatched. Changes may already be present; inspect the registry before retrying.",
        status="outcome_unknown",
    ) from error


def registry_target_missing(domain: str, identifier: str) -> NoReturn:
    raise ToolFailure(
        "RESOURCE_NOT_FOUND",
        f"{domain.capitalize()} not found: {identifier}",
        status="not_started",
    )
