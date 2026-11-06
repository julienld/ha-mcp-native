"""Frontend theme management tools for Home Assistant.

Themes are YAML/file-based: Home Assistant itself exposes no API to create or
edit theme files. This module covers what CAN be managed at runtime - listing
the installed themes and selecting the backend default theme. Creating or
editing custom theme files goes through ha_config_set_yaml (beta, ha-mcp
custom component); installing community themes goes through HACS.
"""

import logging
from typing import Any, Literal

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from ..runtime.theme_tools import ThemeAction, ThemeTools
from ..runtime.theme_values import backend_theme_data, engine_theme_value
from ..tool_api import ToolFailure
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import write_hints
from .util_helpers import summarize_theme_listing, websocket_error_message

logger = logging.getLogger(__name__)


class ThemesTools:
    """Frontend theme listing and selection tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _engine_credential(self) -> Any:
        """Resolve the screenshot engine user's credential, or fail clearly.

        The engine writes its own token user's profile, which in a dedicated
        engine-account setup is NOT the account ha-mcp itself authenticates
        as -- so these actions cannot simply reuse ha-mcp's own client.
        """
        from ..config import get_global_settings
        from ..dashboard_screenshot.provision import resolve_engine
        from ..dashboard_screenshot.theme_guard import ThemeGuard

        explicit_url = (
            get_global_settings().dashboard_screenshot_engine_url or ""
        ).strip()
        engine_target = await resolve_engine()
        # An explicit URL is never provably paired with a credential. Even on
        # HA OS, _addon_credential_best_effort() hands back the DISCOVERED
        # Puppet app's credential without checking it identifies the engine
        # that URL points at -- so a sidecar URL plus a running app yields a
        # credential for the wrong account. That was a safe no-op while the
        # guard only wrote back its own snapshot; it is not safe now that
        # these actions read and write a profile on request.
        if explicit_url or engine_target.addon_credential is None:
            # An explicitly configured engine URL yields no addon_credential,
            # so the only fallback is ha-mcp's OWN credential -- which under
            # the dedicated-engine-account setup this feature recommends is a
            # different Home Assistant user. Acting on it would read and
            # overwrite the wrong profile while leaving the engine account
            # untouched. expected_current cannot save us: it compares values,
            # not identity, and two users sharing a theme compare equal.
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "The screenshot engine's Home Assistant account cannot be "
                    "identified, so its theme cannot be read or written.",
                    suggestions=[
                        (
                            "This applies whenever an engine URL is set "
                            + "explicitly: the engine's own token is not "
                            + "discoverable, and a credential discovered via "
                            + "the Supervisor is not provably that engine's."
                        ),
                        (
                            "Restore the theme from that account's own "
                            + "session: Profile > General in the Home "
                            + "Assistant UI."
                        ),
                    ],
                )
            )
        guard = ThemeGuard.for_capture(engine_target.addon_credential, self._client)
        if guard.credential is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "No screenshot-engine credential is discoverable in this "
                    "deployment, so the engine account's theme cannot be read "
                    "or written.",
                    suggestions=[
                        "This action only applies where a screenshot engine "
                        "is configured.",
                    ],
                )
            )
        return guard.credential

    async def _get_engine_theme(self) -> dict[str, Any]:
        """Read the screenshot engine account's saved per-user theme."""
        from ..dashboard_screenshot.theme_guard import read_engine_theme

        credential = await self._engine_credential()
        return {"success": True, "data": {"theme": await read_engine_theme(credential)}}

    async def _set_engine_theme(
        self,
        action: str,
        value: dict[str, Any] | None,
        expected_current: dict[str, Any] | None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Restore the screenshot engine account's saved per-user theme."""
        from ..dashboard_screenshot.theme_guard import (
            ThemeChangedError,
            write_engine_theme,
        )

        value = engine_theme_value(value)
        credential = await self._engine_credential()
        try:
            # expected_current=None is an explicit "expect no stored theme",
            # not an omission -- only force skips the guard.
            await write_engine_theme(
                credential, value, expected_current=expected_current, force=force
            )
        except ThemeChangedError as guard_error:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "The engine account's saved theme is no longer the value "
                    "this restore expected, so it was not overwritten.",
                    context={
                        "action": action,
                        "expected_current": expected_current,
                        "actual_current": guard_error.actual,
                    },
                    suggestions=[
                        (
                            "Something else changed the theme since the "
                            + "warning was emitted -- confirm the intended "
                            + "value before retrying."
                        ),
                        (
                            "Re-read it with ha_manage_theme("
                            + "action='get_engine_theme')."
                        ),
                    ],
                )
            )
        return {"success": True, "data": {"theme": value}}

    async def _list_themes(self) -> dict[str, Any]:
        """Fetch installed theme names and defaults via websocket."""
        result = await self._client.send_websocket_message(
            {"type": "frontend/get_themes"}
        )
        if not result.get("success"):
            error_msg = websocket_error_message(result.get("error", "Operation failed"))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list themes: {error_msg}",
                    context={"action": "list"},
                )
            )
        return summarize_theme_listing(result.get("result") or {})

    @property
    def themes(self) -> Any:
        return self

    async def manage(
        self,
        action: ThemeAction,
        theme_name: str | None = None,
        expected_current: dict[str, Any] | None = None,
        force: bool = False,
        value: dict[str, Any] | None = None,
        mode: Literal["light", "dark"] | None = None,
    ) -> dict[str, Any]:
        try:
            if action == "list":
                return {"success": True, "data": await self._list_themes()}

            if action == "get_engine_theme":
                return await self._get_engine_theme()

            if action == "set_engine_theme":
                return await self._set_engine_theme(
                    action, value, expected_current, force
                )

            service_data = backend_theme_data(theme_name, mode)
            try:
                await self._client.call_service("frontend", "set_theme", service_data)
            except HomeAssistantAPIError as e:
                # HA's REST layer collapses the service's validation detail
                # ("Theme X not found") into a bare 400 - name the theme here.
                error_response = exception_to_structured_error(
                    e,
                    raise_error=False,
                    context={"action": action, "theme_name": theme_name, "mode": mode},
                    suggestions=[
                        "Call ha_manage_theme(action='list') to see installed themes"
                    ],
                )
                error_response["error"]["message"] = (
                    f"Theme '{theme_name}': {error_response['error']['message']}"
                )
                raise_tool_error(error_response)

            # Re-read the defaults so the agent sees the effective state.
            listing = await self._list_themes()
            return {
                "success": True,
                "data": {
                    "theme": theme_name,
                    # HA applies the theme to light mode when mode is omitted.
                    "mode": mode or "light",
                    "default_theme": listing.get("default_theme"),
                    "default_dark_theme": listing.get("default_dark_theme"),
                },
            }
        except (ToolError, ToolFailure):
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"action": action, "theme_name": theme_name, "mode": mode},
                suggestions=[
                    "Call ha_manage_theme(action='list') to see installed themes",
                    "Verify the Home Assistant connection",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    ha_manage_theme = tool(
        name="ha_manage_theme",
        tags={"System"},
        annotations=write_hints(
            "Manage Frontend Themes",
            destructive=True,
            idempotent=True,
            open_world=False,
        ),
    )(log_tool_usage(legacy_errors(ThemeTools.ha_manage_theme)))


def register_themes_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register frontend theme management tools."""
    register_tool_methods(mcp, ThemesTools(client))
