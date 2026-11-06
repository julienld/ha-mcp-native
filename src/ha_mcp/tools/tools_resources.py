"""
Dashboard resource hosting tools for Home Assistant MCP server.

Provides tools for managing dashboard resources (custom cards, CSS, JS):
- Inline resources: Code embedded directly in the resource URL as a
  ``data:`` URI — stored in HA's own resource registry, served by nothing
- External resources: URLs to /local/ (incl. /local/community/), /hacsfiles/, or CDNs

Inline resources were previously served by a Cloudflare Worker that decoded
base64 content embedded in its URL, based on issue #71's claim that browsers
reject ``data:`` URIs for ES6 modules. That claim was wrong — HA's WS API
applies no URL-scheme validation, HA ships no CSP, and every layer loads
``data:`` modules and stylesheets fine (verified live; see #2060). Inline
content now goes straight into a ``data:`` URI: same storage location
(the resource registry), no third-party hop on dashboard loads.
"""

from __future__ import annotations

import builtins
import logging
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.backends.legacy import legacy_errors, legacy_errors_sync
from ha_mcp.runtime.resource_tools import ResourceTools as ResourceContractTools
from ha_mcp.runtime.resource_values import (
    _ALLOWED_DATA_URI_PREFIXES as _ALLOWED_DATA_URI_PREFIXES,
)
from ha_mcp.runtime.resource_values import (
    _HA_CONFIG_YAML_MARKERS as _HA_CONFIG_YAML_MARKERS,
)
from ha_mcp.runtime.resource_values import (
    _RESOURCE_TYPE,
    _YAML_MODE_MESSAGE,
    LEGACY_WORKER_BASE_URL,
    MAX_CONTENT_SIZE,
    _check_ws_error,
    _decode_data_uri,
    _decode_inline_content,
    _decode_legacy_worker_url,
    _detect_ha_config_yaml,
    _extract_resource_id,
    _is_data_url,
    _is_preview_of,
    _looks_like_preview,
    _process_resource_list,
    _summarize_resources,
    resource_list_result,
)
from ha_mcp.runtime.resource_values import (
    _data_uri_for as _pure_data_uri_for,
)
from ha_mcp.runtime.resource_values import (
    _require_created_resource_id as _pure_require_created_resource_id,
)
from ha_mcp.runtime.resource_write_values import (
    resource_delete_result,
    resource_set_result,
    validate_choice,
    validate_inline,
)

from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
    ws_failure_code,
)
from .resource_codec import content_sha256
from .resource_patch import (
    ResourceEdit,
    ResourceLookupError,
    apply_resource_edits,
    check_config_hash,
    find_resource,
    load_inline_resource,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)


class LegacyResources:
    """Dashboard resource hosting tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def list(
        self,
        include_content: bool = False,
        limit: int = 100,
        offset: int = 0,
        include_hash: bool = False,
    ) -> dict[str, Any]:
        try:
            result = await self._client.send_websocket_message(
                {"type": "lovelace/resources"}
            )

            # Handle WebSocket response format
            if isinstance(result, dict) and "result" in result:
                resources = result["result"]
            elif isinstance(result, list):
                resources = result
            else:
                resources = []

            return resource_list_result(
                resources,
                include_content,
                limit,
                offset,
                await self._resource_mode(),
                include_hash,
            )
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error listing dashboard resources: {e}")
            exception_to_structured_error(
                e, context={"tool": "ha_config_list_dashboard_resources"}
            )
            # Unreachable (exception_to_structured_error is NoReturn).
            raise

    async def save(
        self,
        content: str | None = None,
        url: str | None = None,
        resource_type: str = "module",
        resource_id: str | None = None,
        edits: builtins.list[ResourceEdit] | None = None,
        config_hash: str | None = None,
    ) -> dict[str, Any]:
        if edits is not None:
            return await self._patch_inline_resource(
                resource_id, edits, config_hash, other_modes=(content, url)
            )
        if config_hash is not None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "config_hash only applies together with edits",
                )
            )
        if resource_id is not None:
            validate_identifier_not_empty(
                resource_id, "resource_id", context={"action": "set"}
            )
        validate_choice(content, url)
        if content is not None:
            return await self._set_inline_resource(content, resource_type, resource_id)
        return await self._set_url_resource(url, resource_type, resource_id)

    async def _set_inline_resource(
        self,
        content: str,
        resource_type: _RESOURCE_TYPE,
        resource_id: str | None,
        *,
        check_preview: bool = True,
    ) -> dict[str, Any]:
        """Create or update an inline dashboard resource.

        ``check_preview=False`` skips the truncated-preview guard. Only edit
        results pass it: they are derived from the stored content, not
        copied from a list response, so a preview-shaped result (e.g. a
        suffix deletion leaving ``_PREVIEW_LENGTH`` characters) is intended.
        """
        content_size = validate_inline(content, resource_type)

        if resource_id is not None and check_preview:
            await self._reject_truncated_preview_resave(content, resource_id)

        resource_url = _data_uri_for(content, resource_type)

        try:
            result, action = await self._upsert_resource(
                resource_id, resource_url, resource_type
            )

            if _check_ws_error(result):
                await self._raise_write_error(
                    result, {"action": action, "resource_id": resource_id}
                )

            new_resource_id = _extract_resource_id(result, resource_id)
            _require_created_resource_id(new_resource_id, action)

            logger.info(
                f"Inline dashboard resource {action}: id={new_resource_id}, "
                f"type={resource_type}, size={content_size}"
            )

            return {
                "success": True,
                "action": action,
                "resource_id": new_resource_id,
                "resource_type": resource_type,
                "size": content_size,
                "config_hash": _stored_content_hash(result),
                "note": "Clear browser cache or hard refresh to load changes",
            }
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error setting inline dashboard resource: {e}")
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_config_set_dashboard_resource",
                    "action": "update" if resource_id else "create",
                },
            )
            # Unreachable (exception_to_structured_error is NoReturn).
            raise

    async def _patch_inline_resource(
        self,
        resource_id: str | None,
        edits: builtins.list[ResourceEdit],
        config_hash: str | None,
        other_modes: tuple[str | None, str | None],
    ) -> dict[str, Any]:
        """Apply search/replace edits to a stored inline resource, all or nothing."""
        if any(mode is not None for mode in other_modes):
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message="Provide only one of 'edits', 'content' or 'url'",
                    suggestions=[
                        "Use edits= alone to change part of an existing inline resource",
                    ],
                )
            )
        if resource_id is None:
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_MISSING_PARAMETER,
                    message="edits requires resource_id",
                    suggestions=[
                        "Get the ID from ha_config_list_dashboard_resources()",
                    ],
                )
            )
        validate_identifier_not_empty(
            resource_id,
            "resource_id",
            suggestions=["Pass the ID of the inline resource to patch"],
            context={"action": "patch"},
        )
        # with_auto_backup holds this resource's write lock around the whole
        # call (resource_write_lock), so no other write through this server
        # lands between this read and the write below.
        try:
            stored, resource_type = await load_inline_resource(
                self._client, resource_id
            )
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error reading dashboard resource to patch: {e}")
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_config_set_dashboard_resource",
                    "action": "patch",
                    "resource_id": resource_id,
                },
            )
            # Unreachable (exception_to_structured_error is NoReturn).
            raise
        check_config_hash(stored, config_hash)
        patched = apply_resource_edits(stored, edits)
        response = await self._set_inline_resource(
            patched, resource_type, resource_id, check_preview=False
        )
        return {**response, "action": "patched", "applied": len(edits)}

    async def _reject_truncated_preview_resave(
        self, content: str, resource_id: str
    ) -> None:
        """Refuse to overwrite an inline resource with its own truncated preview.

        The list tool's default output carries ``_preview`` — the first
        ``_PREVIEW_LENGTH`` characters plus ``"..."``. An agent that re-saves
        a resource using that string as ``content`` would silently replace
        the user's card with a syntactically broken fragment.

        Only preview-shaped content triggers the (one WS round-trip) check,
        so normal updates pay nothing. When the check itself cannot run, it
        FAILS CLOSED: the write is refused rather than allowed unverified.
        A preview-shaped payload is already the suspicious case, and the
        costs are lopsided — a wrong refusal costs one retry with the real
        content, a wrong allow destroys the user's card and still reports
        ``success: True``.
        """
        if not _looks_like_preview(content):
            return

        def _unverifiable(detail: str) -> NoReturn:
            logger.warning(
                "Preview-writeback guard could not verify resource %s (%s); "
                "refusing the update rather than risking content loss",
                resource_id,
                detail,
            )
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.SERVICE_CALL_FAILED,
                    message=(
                        "Content looks like the truncated preview from "
                        "ha_config_list_dashboard_resources, and the stored "
                        f"resource could not be read back to confirm ({detail}). "
                        "Refusing the write: saving a preview would destroy the "
                        "resource."
                    ),
                    context={"resource_id": resource_id},
                    suggestions=[
                        "Fetch the full code with ha_config_list_dashboard_resources(include_content=True)",
                        "Re-save with the complete content (this check only triggers on preview-shaped content)",
                    ],
                )
            )

        try:
            found = await find_resource(self._client, resource_id)
        except ResourceLookupError as e:
            _unverifiable(e.detail)
        except Exception as e:  # noqa: BLE001
            _unverifiable(f"listing the resources failed: {e}")
        if found is None:
            return
        stored = found[1]
        if stored and _is_preview_of(content, stored):
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message="Content is the truncated preview from "
                    "ha_config_list_dashboard_resources, not the full "
                    "resource code — saving it would destroy the resource",
                    context={"resource_id": resource_id},
                    suggestions=[
                        "Fetch the full code with ha_config_list_dashboard_resources(include_content=True)",
                        "Then re-save with the complete content",
                    ],
                )
            )

    async def _set_url_resource(
        self,
        url: str | None,
        resource_type: _RESOURCE_TYPE,
        resource_id: str | None,
    ) -> dict[str, Any]:
        """Create or update an external URL dashboard resource."""
        try:
            result, action = await self._upsert_resource(
                resource_id, url, resource_type
            )

            if _check_ws_error(result):
                await self._raise_write_error(
                    result, {"action": action, "resource_id": resource_id, "url": url}
                )

            new_resource_id = _extract_resource_id(result, resource_id)
            _require_created_resource_id(new_resource_id, action)

            logger.info(
                f"Dashboard resource {action}: id={new_resource_id}, "
                f"type={resource_type}, url={url}"
            )

            return resource_set_result(new_resource_id, resource_type, action, url=url)
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error setting dashboard resource: {e}")
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_config_set_dashboard_resource",
                    "action": "update" if resource_id else "create",
                    "url": url,
                },
            )
            # Unreachable (exception_to_structured_error is NoReturn).
            raise

    async def _resource_mode(self) -> str | None:
        """Core's lovelace ``resource_mode`` ('storage'/'yaml'), None if unreadable."""
        try:
            result = await self._client.send_websocket_message(
                {"type": "lovelace/info"}
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read lovelace/info: {e}")
            return None
        info = result.get("result") if isinstance(result, dict) else None
        mode = info.get("resource_mode") if isinstance(info, dict) else None
        return mode if isinstance(mode, str) else None

    async def _raise_write_error(
        self, result: Any, context: dict[str, Any]
    ) -> NoReturn:
        """Raise Core's rejection of a resource write, routed on Core's error code."""
        error_msg = str(_check_ws_error(result))
        code = result.get("error_code") if isinstance(result, dict) else None
        resource_id = context.get("resource_id")
        if code == "not_found" and resource_id:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Dashboard resource '{resource_id}' not found",
                    details=error_msg,
                    context=context,
                    suggestions=[
                        "Use ha_config_list_dashboard_resources() to see available resources"
                    ],
                )
            )
        if code == "unknown_command" and await self._resource_mode() == "yaml":
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    _YAML_MODE_MESSAGE,
                    details=error_msg,
                    context={**context, "resource_mode": "yaml"},
                    suggestions=[
                        "Edit lovelace: resources: in configuration.yaml, then run "
                        "the lovelace.reload_resources action"
                    ],
                )
            )
        raise_tool_error(
            create_error_response(
                ws_failure_code(result)
                if isinstance(result, dict)
                else ErrorCode.SERVICE_CALL_FAILED,
                error_msg,
                context={**context, "ha_error_code": code} if code else context,
            )
        )

    async def _upsert_resource(
        self,
        resource_id: str | None,
        url: str | None,
        resource_type: _RESOURCE_TYPE,
    ) -> tuple[dict[str, Any], str]:
        """Create or update a lovelace resource. Returns (result, action).

        Callers validate ``resource_id`` first: None creates, anything else
        updates.
        """
        if resource_id is not None:
            result = await self._client.send_websocket_message(
                {
                    "type": "lovelace/resources/update",
                    "resource_id": resource_id,
                    "url": url,
                    "res_type": resource_type,
                }
            )
            return result, "updated"
        else:
            result = await self._client.send_websocket_message(
                {
                    "type": "lovelace/resources/create",
                    "url": url,
                    "res_type": resource_type,
                }
            )
            return result, "created"

    async def delete(self, resource_id: str) -> dict[str, Any]:
        try:
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                resource_id,
                "resource_id",
                suggestions=[
                    "Pass a valid resource_id (use ha_config_list_dashboard_resources() to list)",
                ],
                context={"action": "delete"},
            )
            result = await self._client.send_websocket_message(
                {
                    "type": "lovelace/resources/delete",
                    "resource_id": resource_id,
                }
            )

            if _check_ws_error(result):
                await self._raise_write_error(
                    result, {"action": "delete", "resource_id": resource_id}
                )

            logger.info(f"Dashboard resource deleted: id={resource_id}")

            return resource_delete_result(resource_id)
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error deleting dashboard resource: {e}")
            exception_to_structured_error(
                e, context={"action": "delete", "resource_id": resource_id}
            )
            # Unreachable (exception_to_structured_error is NoReturn).
            raise


class ResourceTools(ResourceContractTools):
    def __init__(self, client: Any) -> None:
        self._client = client
        super().__init__(LegacyResources(client))

    ha_config_list_dashboard_resources = tool(
        name="ha_config_list_dashboard_resources",
        tags={"Dashboards"},
        annotations=read_only_hints("List Dashboard Resources", open_world=True),
    )(
        log_tool_usage(
            legacy_errors(ResourceContractTools.ha_config_list_dashboard_resources)
        )
    )
    ha_config_set_dashboard_resource = tool(
        name="ha_config_set_dashboard_resource",
        tags={"Dashboards"},
        annotations=write_hints(
            "Set Dashboard Resource",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(
        with_auto_backup(domain="dashboard_resource", id_param="resource_id")(
            log_tool_usage(
                legacy_errors(ResourceContractTools.ha_config_set_dashboard_resource)
            )
        )
    )
    ha_config_delete_dashboard_resource = tool(
        name="ha_config_delete_dashboard_resource",
        tags={"Dashboards"},
        annotations=write_hints(
            "Delete Dashboard Resource",
            destructive=True,
            idempotent=True,
            open_world=False,
        ),
    )(
        with_auto_backup(domain="dashboard_resource", id_param="resource_id")(
            log_tool_usage(
                legacy_errors(ResourceContractTools.ha_config_delete_dashboard_resource)
            )
        )
    )


def register_resources_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register dashboard resource tools."""
    register_tool_methods(mcp, ResourceTools(client))


_data_uri_for = legacy_errors_sync(_pure_data_uri_for)
_require_created_resource_id = legacy_errors_sync(_pure_require_created_resource_id)


__all__ = [
    "LEGACY_WORKER_BASE_URL",
    "MAX_CONTENT_SIZE",
    "ResourceTools",
    "_ALLOWED_DATA_URI_PREFIXES",
    "_HA_CONFIG_YAML_MARKERS",
    "_decode_data_uri",
    "_decode_legacy_worker_url",
    "_detect_ha_config_yaml",
    "_is_data_url",
    "_process_resource_list",
    "_summarize_resources",
    "register_resources_tools",
]


def _stored_content_hash(result: Any) -> str | None:
    """Hash the content Home Assistant stored, decoded from the write's result.

    ``lovelace/resources/create`` and ``/update`` return the stored item;
    hashing its URL rather than the string sent keeps the hash tied to what
    HA holds. None when the result carries no decodable inline URL.
    """
    item = result.get("result") if isinstance(result, dict) else None
    url = item.get("url") if isinstance(item, dict) else None
    decoded = _decode_inline_content(url) if isinstance(url, str) else None
    return content_sha256(decoded[0]) if decoded else None
