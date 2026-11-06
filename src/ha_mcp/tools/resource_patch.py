"""Search/replace edits and content hashing for inline dashboard resources.

Lets ``ha_config_set_dashboard_resource(edits=...)`` change a few lines of a
large inline card without the model regenerating the whole payload. Edits are
exact-match and all-or-nothing: every ``old`` must occur exactly once in the
content as it stands when that edit is applied, or nothing is saved.
"""

from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any

from ..backends.legacy import legacy_errors_sync
from ..errors import ErrorCode, create_error_response
from ..runtime.resource_edits import MAX_EDITS as MAX_EDITS
from ..runtime.resource_edits import ResourceEdit as ResourceEdit
from ..runtime.resource_edits import _count_occurrences as _count_occurrences
from ..runtime.resource_edits import apply_resource_edits as _apply_resource_edits
from ..runtime.resource_edits import check_config_hash as _check_config_hash
from ..utils.registry_update_lock import registry_update_lock
from .helpers import raise_tool_error, ws_failure_code
from .resource_codec import (
    _DATA_URI_MIME,
    _MAX_ECHOED_URL,
    _check_ws_error,
    _decode_inline_content,
)

# Lock key shared with _restore_dashboard_resource in backup_manager (the
# auto-backup domain name), so a restore and a tool write serialize too.
RESOURCE_LOCK_REGISTRY = "dashboard_resource"


def resource_write_lock(resource_id: str | None) -> AbstractAsyncContextManager[Any]:
    """Serialize this process's writes to one resource; a create needs none.

    ``with_auto_backup`` holds it around every dashboard_resource tool call,
    from the pre-write snapshot through the write. Writes from the HA UI or
    other clients are not serialized. Never take it inside those tools:
    ``asyncio.Lock`` is not reentrant.
    """
    if resource_id is None:
        return nullcontext()
    return registry_update_lock(RESOURCE_LOCK_REGISTRY, resource_id)


# Pydantic reads this at schema time; mypy allows only fields in the body.


class ResourceLookupError(Exception):
    """Home Assistant rejected the resource listing or returned no list.

    ``result`` is Core's reply, so a caller can map Core's error code.
    """

    def __init__(self, detail: str, result: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.result = result


async def find_resource(
    client: Any, resource_id: str
) -> tuple[dict[str, Any], str | None] | None:
    """Look up ``resource_id`` in ``lovelace/resources``.

    Returns ``(row, decoded_content)``, with ``decoded_content`` None for a
    URL that is not an inline resource, or None when no row has that ID.
    Raises ``ResourceLookupError`` when Home Assistant rejects the listing or
    returns something other than a list; transport errors propagate as-is.
    Callers word their own errors.
    """
    result = await client.send_websocket_message({"type": "lovelace/resources"})
    error_msg = _check_ws_error(result)
    if error_msg:
        raise ResourceLookupError(
            f"Home Assistant rejected the listing: {error_msg}", result
        )
    resources = result.get("result") if isinstance(result, dict) else result
    if not isinstance(resources, list):
        raise ResourceLookupError(
            "Home Assistant returned an unreadable resource list", result
        )
    for row in resources:
        # A malformed entry must not raise a bare AttributeError.
        if isinstance(row, dict) and str(row.get("id")) == str(resource_id):
            url = row.get("url")
            decoded = _decode_inline_content(url) if isinstance(url, str) else None
            return row, decoded[0] if decoded else None
    return None


async def load_inline_resource(client: Any, resource_id: str) -> tuple[str, str]:
    """Fetch an inline resource and return ``(decoded_content, resource_type)``.

    Raises ``RESOURCE_NOT_FOUND`` for an unknown ID, a ``not_inline``
    validation error for /local/, /hacsfiles/, https:// or undecodable URLs,
    an ``unsupported_type`` validation error for an inline row whose type a
    ``data:`` URI cannot carry (legacy ``js``/``html`` worker rows), and
    Core's mapped error when the listing itself fails.
    """
    context = {"action": "patch", "resource_id": resource_id}
    try:
        found = await find_resource(client, resource_id)
    except ResourceLookupError as e:
        code = (
            ws_failure_code(e.result)
            if isinstance(e.result, dict)
            else ErrorCode.SERVICE_CALL_FAILED
        )
        raise_tool_error(create_error_response(code, e.detail, context=context))
    if found is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Dashboard resource '{resource_id}' not found",
                context=context,
                suggestions=[
                    "Use ha_config_list_dashboard_resources() to see available resources"
                ],
            )
        )
    stored, content = found
    if content is None:
        url = stored.get("url")
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Resource '{resource_id}' is not an inline resource, so it "
                "cannot be patched with edits",
                context={
                    **context,
                    "reason": "not_inline",
                    "url": url[:_MAX_ECHOED_URL] if isinstance(url, str) else url,
                },
                suggestions=[
                    "Edit the file behind a /local/ URL with ha_write_file instead",
                    "HACS and CDN resources are managed by their source",
                ],
            )
        )
    resource_type = str(stored.get("type", "module"))
    if resource_type not in _DATA_URI_MIME:
        # Legacy worker URLs could carry 'js'/'html' rows; a data: URI cannot.
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Resource '{resource_id}' has type {resource_type!r}, which "
                "edits cannot rewrite as an inline resource",
                context={
                    **context,
                    "reason": "unsupported_type",
                    "resource_type": resource_type,
                },
                suggestions=[
                    "Pass the full content with resource_type='module' and "
                    "this resource_id to convert it",
                ],
            )
        )
    return content, resource_type


apply_resource_edits = legacy_errors_sync(_apply_resource_edits)
check_config_hash = legacy_errors_sync(_check_config_hash)


__all__ = [
    "MAX_EDITS",
    "RESOURCE_LOCK_REGISTRY",
    "ResourceEdit",
    "ResourceLookupError",
    "_count_occurrences",
    "apply_resource_edits",
    "check_config_hash",
    "find_resource",
    "load_inline_resource",
    "resource_write_lock",
]
