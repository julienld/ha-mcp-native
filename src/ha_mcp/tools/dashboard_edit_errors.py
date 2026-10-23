"""Consistent failure codes and recovery guidance for dashboard edit backends."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime.dashboard_errors import (
    raise_dashboard_edit_error as _pure_edit_error,
)

from ..errors import get_error_code, get_error_message
from .helpers import extract_tool_error_message

raise_dashboard_edit_error: Callable[..., NoReturn] = legacy_errors_sync(
    _pure_edit_error
)


def raise_dashboard_edit_fetch_error(
    exc: ToolError, url_path: str, action: str
) -> NoReturn:
    """Only Core's explicit config_not_found means an absent dashboard/config."""
    try:
        data = json.loads(str(exc))
    except (ValueError, TypeError):
        data = None
    reason = (
        "not_found"
        if isinstance(data, dict) and data.get("ha_error_code") == "config_not_found"
        else "load_failed"
    )
    raise_dashboard_edit_error(
        url_path, reason, extract_tool_error_message(exc), False, action
    )


def raise_known_dashboard_save_rejection(
    response: dict[str, Any], url_path: str, action: str
) -> None:
    """Translate definite Core rejections; leave all other save errors unchanged.

    Lovelace's WebSocket wrapper reports missing configs as config_not_found.
    LovelaceConfig.async_save rejects non-storage implementations (including
    YAML) with HomeAssistantError("Not supported"), encoded as code "error".
    Require both that code and exact message: recovery-mode failures differ.
    """
    code = response.get("error_code") or get_error_code(response)
    message = get_error_message(response) or "Dashboard save rejected"
    if code in {"unauthorized", "invalid_format"}:
        raise_dashboard_edit_error(url_path, code, message, False, action)
    if code == "config_not_found":
        raise_dashboard_edit_error(url_path, "not_found", message, False, action)
    if code == "error" and message.removeprefix("Command failed: ") == "Not supported":
        raise_dashboard_edit_error(
            url_path,
            "unsupported_mode",
            "Dashboard does not support storage-mode editing",
            False,
            action,
        )
    if (
        code == "error"
        and message.removeprefix("Command failed: ")
        == "Saving not supported in recovery mode"
    ):
        # LovelaceStorage checks recovery mode before changing its live config.
        raise_dashboard_edit_error(url_path, "recovery_mode", message, False, action)
