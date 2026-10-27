"""App operations on Core's Supervisor owners and bounded app transports."""

import copy
from functools import partial
from typing import Any

from ha_mcp.runtime.app_arrays import _apply_array_ops
from ha_mcp.runtime.app_dispatch import AppDispatch
from ha_mcp.runtime.app_http_hints import (
    _addon_config_for_http_hint,
    add_http_error_hints,
)
from ha_mcp.runtime.app_proxy_results import (
    _apply_response_transform,
    _build_ws_result,
    _truncate_http_response,
    http_result,
)
from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .app_actions import WEBSOCKET_TIMEOUT, AppActions
from .app_routes import AppRoute, routing_identity
from .app_transport import AppHTTPError, http_exchange, ws_exchange


class NativeApps(AppActions, AppDispatch):
    async def _project(self, function: Any, *args: Any) -> Any:
        result = await await_owned_io(
            self.manager.hass.async_add_executor_job(partial(function, *args))
        )
        await self.observe()
        return result

    async def _http(
        self,
        slug: str,
        path: str,
        method: str,
        body: Any,
        port: int | None,
        headers: dict[str, str] | None,
        *,
        expected_route: AppRoute | None = None,
    ) -> tuple[AppRoute, int, dict[str, str], Any]:
        route = await AppRoute.prepare(self, slug, path, port, headers)
        if expected_route is not None and not json_values_equal(
            routing_identity(expected_route.addon), routing_identity(route.addon)
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The app changed during array editing",
                status="not_started",
            )
        try:
            status, response_headers, value = await self.write(
                lambda: http_exchange(route, method, body),
                array_patch_post=expected_route is not None,
            )
        except AppHTTPError as error:
            hints: dict[str, Any] = {}
            add_http_error_hints(
                hints, error.status_code, route.addon, slug, port is not None
            )
            message = hints.pop("error")
            if "addon_config" in hints:
                # Select the remedy from the real schema/options, but return
                # only the same redacted options as ha_get_app.
                hints["addon_config"] = _addon_config_for_http_hint(
                    self._info(route.addon, slug)["addon"]
                )
            failure = ToolFailure(
                "OUTCOME_UNKNOWN",
                f"{message}. The request was sent; inspect the app before another change. No retry was performed.",
                status="outcome_unknown",
            )
            failure.data.update(status_code=error.status_code, slug=slug, **hints)
            if self.array_patch_post_completed is not None:
                failure.data.update(
                    prerequisite_get_completed=self.completed,
                    array_patch_applied=None if expected_route is not None else False,
                )
            raise failure from None
        return route, status, response_headers, value

    async def _execute_http_proxy(
        self,
        *,
        slug: str,
        path: str,
        method: str,
        body: Any,
        debug: bool,
        port: int | None,
        offset: int,
        limit: int | None,
        python_transform: str | None,
        request_headers: dict[str, str] | None,
        message_limit: int | None,
        message_offset: int,
        summarize: bool,
    ) -> dict[str, Any]:
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
            raise ToolFailure(
                "VALIDATION_ERROR", "Unsupported app HTTP method", status="not_started"
            )
        if message_limit is not None or message_offset != 0 or not summarize:
            raise ToolFailure(
                "VALIDATION_ERROR",
                "message_limit / message_offset / summarize apply only to WebSocket mode",
                status="not_started",
            )
        route, status, response_headers, value = await self._http(
            slug, path, method, body, port, request_headers
        )
        result: dict[str, Any] = await self._project(
            _http_projection,
            route.slug,
            route.addon.get("name", route.slug),
            route.debug_url,
            status,
            response_headers,
            value,
            offset,
            limit,
            python_transform,
            debug,
        )
        return result

    async def _execute_ws_proxy(
        self,
        slug: str,
        path: str,
        body: Any,
        debug: bool,
        port: int | None,
        wait_for_close: bool,
        message_limit: int | None,
        message_offset: int,
        summarize: bool,
        python_transform: str | None,
    ) -> dict[str, Any]:
        route = await AppRoute.prepare(self, slug, path, port, None)
        collected, size, reason, elapsed, cap = await self.write(
            lambda: ws_exchange(
                route, body, message_limit, message_offset, wait_for_close
            ),
            budget=WEBSOCKET_TIMEOUT,
        )
        result: dict[str, Any] = await self._project(
            _build_ws_result,
            slug,
            route.addon.get("name", slug),
            collected,
            reason,
            elapsed,
            message_limit,
            message_offset,
            summarize,
            python_transform,
            debug,
            route.debug_url,
            {},
            body,
            size,
            cap,
        )
        return result

    async def _execute_array_patch(
        self,
        slug: str,
        path: str,
        array_patch: dict[str, Any],
        websocket: bool,
        body: Any,
        offset: int,
        limit: int | None,
        debug: bool,
        port: int | None,
        request_headers: dict[str, str] | None,
    ) -> dict[str, Any]:
        id_field, operations = self._validate_array_patch_input(
            array_patch, websocket, body, offset, limit
        )
        # This serializes our own read/modify/write calls. The app's API has no
        # conditional write contract; other clients remain outside this lock.
        async with registry_update_lock("supervisor_app", slug):
            self.array_patch_post_completed = False
            route, _, _, before = await self._http(
                slug, path, "GET", None, port, request_headers
            )
            if not isinstance(before, list):
                raise ToolFailure(
                    "VALIDATION_ERROR",
                    "array_patch requires a JSON array",
                    status="not_started",
                )
            candidate, summary = await self._project(
                _apply_array_ops, copy.deepcopy(before), operations, id_field
            )
            await self._http(
                slug,
                path,
                "POST",
                candidate,
                port,
                request_headers,
                expected_route=route,
            )
            result = {
                "success": True,
                "slug": slug,
                "addon_name": route.addon.get("name", slug),
                "path": path,
                "id_field": id_field,
                "items_before": len(before),
                "items_after": len(candidate),
                "summary": summary,
                "warnings": [
                    "The app API has no conditional array write. Other app clients can change this array between its GET and POST."
                ],
            }
            if debug:
                result["_debug"] = {
                    "fetch": {"url": route.debug_url},
                    "post": {"url": route.debug_url},
                }
            return result


def _http_projection(
    slug: str,
    name: str,
    debug_url: str,
    status: int,
    response_headers: dict[str, str],
    value: Any,
    offset: int,
    limit: int | None,
    transform: str | None,
    debug: bool,
) -> dict[str, Any]:
    pagination = None
    if isinstance(value, list) and (offset > 0 or limit is not None):
        total = len(value)
        value = value[offset : offset + limit if limit is not None else total]
        pagination = {
            "total_items": total,
            "offset": offset,
            "limit": limit,
            "returned": len(value),
        }
    if transform is not None:
        value = _apply_response_transform(value, transform)
    value, truncated = _truncate_http_response(value, False)
    return http_result(
        status,
        response_headers,
        value,
        name,
        slug,
        debug_url,
        {},
        debug,
        pagination,
        transform is not None,
        truncated,
    )
