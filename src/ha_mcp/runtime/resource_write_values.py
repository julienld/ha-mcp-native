"""Pure dashboard-resource validation shared by Core and legacy services."""

from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure as raise_tool_error
from .resource_values import (
    _DATA_URI_MIME,
    _HA_CONFIG_YAML_MARKERS,
    MAX_CONTENT_SIZE,
    _detect_ha_config_yaml,
    _is_data_url,
)


def validate_choice(content: str | None, url: str | None) -> None:
    if content is not None and url is not None:
        raise_tool_error(
            create_error_response(
                code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                message="Provide either 'content' (inline code) or 'url' (external), not both",
                suggestions=[
                    "Use content= for inline JavaScript/CSS code",
                    "Use url= for /local/, /hacsfiles/, or https:// resources",
                ],
            )
        )
    if content is None and url is None:
        raise_tool_error(
            create_error_response(
                code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                message="Either 'content' (inline code) or 'url' (external) is required",
                suggestions=[
                    "Use content= for inline JavaScript/CSS code",
                    "Use url= for /local/, /hacsfiles/, or https:// resources",
                ],
            )
        )
    if url is not None and _is_data_url(url):
        raise_tool_error(
            create_error_response(
                code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                message="data: URLs cannot be registered via url= — "
                "pass the code itself via content= instead",
                suggestions=[
                    "Use content=<your JS/CSS source> (the tool builds the data: URI)",
                    "url= is for /local/, /hacsfiles/, and https:// resources",
                ],
            )
        )


def raise_yaml_misroute(detected_yaml: str, resource_type: str) -> NoReturn:
    """Raise the structured error for HA-config YAML misrouted as inline resource content."""
    right_tool = _HA_CONFIG_YAML_MARKERS[detected_yaml]
    suggestions = ["This tool stores Lovelace JavaScript/CSS resources only"]
    if right_tool:
        suggestions.insert(
            0,
            f"For `{detected_yaml}:` configuration, use {right_tool} instead",
        )
    elif detected_yaml == "scene":
        suggestions.insert(
            0,
            "Scene configuration tools are tracked in #995; "
            "until they ship, scenes can only be created via the HA UI",
        )
    else:
        suggestions.insert(
            0,
            f"No direct tool exists for `{detected_yaml}:` config; "
            "configure it via the HA UI or YAML packages",
        )
    raise_tool_error(
        create_error_response(
            code=ErrorCode.VALIDATION_INVALID_PARAMETER,
            message=(
                f"Content starts with HA-configuration YAML "
                f"(`{detected_yaml}:`) — this tool only accepts Lovelace "
                f"JavaScript or CSS resources, not Home Assistant config "
                f"(see issue #1072)."
            ),
            context={
                "detected_marker": f"{detected_yaml}:",
                "resource_type": resource_type,
            },
            suggestions=suggestions,
        )
    )


def validate_inline(content: str, resource_type: str) -> int:
    if not content.strip():
        raise_tool_error(
            create_error_response(
                code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                message="Content cannot be empty",
            )
        )
    if resource_type not in _DATA_URI_MIME:
        raise_tool_error(
            create_error_response(
                code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                message=f"Inline content does not support resource_type={resource_type!r}",
                suggestions=[
                    "Use resource_type='module' for ES6 JavaScript (recommended)",
                    "Use url= mode for a file of another resource type",
                ],
            )
        )
    detected_yaml = _detect_ha_config_yaml(content)
    if detected_yaml is not None:
        raise_yaml_misroute(detected_yaml, resource_type)
    content_bytes = content.encode("utf-8")
    content_size = len(content_bytes)
    if content_size > MAX_CONTENT_SIZE:
        raise_tool_error(
            create_error_response(
                code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                message=f"Content too large: {content_size:,} bytes (max {MAX_CONTENT_SIZE:,})",
                context={"size": content_size},
                suggestions=[
                    "Minify the code to reduce size",
                    "Split into multiple smaller modules",
                    "Use url= with a /local/ path for larger files",
                ],
            )
        )

    return content_size


def resource_set_result(
    resource_id: str | None,
    resource_type: str,
    action: str,
    *,
    size: int | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    return {
        "success": True,
        "action": action,
        "resource_id": resource_id,
        "resource_type": resource_type,
        **({"size": size} if size is not None else {"url": url}),
        "note": "Clear browser cache or hard refresh to load changes",
    }


def resource_delete_result(resource_id: str) -> dict[str, Any]:
    return {
        "success": True,
        "action": "delete",
        "resource_id": resource_id,
        "message": "Resource deleted successfully",
    }
