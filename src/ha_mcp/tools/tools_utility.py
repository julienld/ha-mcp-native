"""
Utility tools for Home Assistant MCP server.

Template and condition evaluation. The log tools that used to share this module
are in ``tools_logs`` and its helpers, split out under `.gemini/styleguide.md` §
Tool Consolidation and Module Size.
"""

import logging
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime.template_results import (
    HA_RENDER_TIMEOUT_TEXT,
    _command_error_message,
    condition_failure,
    condition_result,
    render_success,
)
from ha_mcp.runtime.template_results import (
    raise_render_failure as _pure_render_failure,
)
from ha_mcp.runtime.template_results import (
    validate_choice as _pure_validate_choice,
)
from ha_mcp.runtime.templates import TemplateTools

from ..client.rest_client import HomeAssistantCommandError, HomeAssistantCommandTimeout
from ..client.websocket_client import get_websocket_client
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .tool_hints import read_only_hints

logger = logging.getLogger(__name__)

_raise_render_failure = legacy_errors_sync(_pure_render_failure)
_validate_choice = legacy_errors_sync(_pure_validate_choice)


class UtilityTools:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def eval_template(
        self,
        template: str | None,
        condition: dict[str, Any] | None,
        variables: dict[str, Any] | None,
        strict: bool,
        timeout: int,
        report_errors: bool,
    ) -> dict[str, Any]:
        _validate_choice(template, condition)
        try:
            if condition is not None:
                return await self._test_condition(condition, variables)
            assert template is not None
            return await self._render_template(
                template, variables, strict, timeout, report_errors
            )
        except ToolError:
            raise
        except Exception as e:
            error_str = str(e)
            suggestions = ["Check Home Assistant WebSocket connection"]

            if "403" in error_str and "Forbidden" in error_str:
                suggestions = [
                    "The request was blocked (403 Forbidden) - this may be caused by:",
                    "  • Reverse proxy security rules (Apache, Nginx, Traefik)",
                    "  • Rate limiting from multiple simultaneous requests",
                    "  • Complex template triggering security filters",
                    "Try simplifying the template (remove newlines, reduce complexity)",
                    "Break complex templates into multiple simpler calls",
                    "Use ha_report_issue to check Home Assistant logs for details",
                ] + suggestions

            exception_to_structured_error(
                e,
                context=(
                    {"condition": condition}
                    if condition is not None
                    else {"template": template}
                ),
                suggestions=suggestions,
            )
            raise  # unreachable: exception_to_structured_error always raises

    async def _render_template(
        self,
        template: str,
        variables: dict[str, Any] | None,
        strict: bool,
        timeout: int,
        report_errors: bool,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "type": "render_template",
            "template": template,
            "timeout": timeout,
            "report_errors": report_errors,
            "strict": strict,
        }
        if variables is not None:
            message["variables"] = variables

        result = await self._client.send_websocket_message(message)

        if result.get("success"):
            return render_success(template, result)

        await self._raise_render_failure(
            template, variables, strict, timeout, report_errors, result
        )
        raise AssertionError("unreachable: _raise_render_failure always raises")

    async def _raise_render_failure(
        self,
        template: str,
        variables: dict[str, Any] | None,
        strict: bool,
        timeout: int,
        report_errors: bool,
        result: dict[str, Any],
    ) -> NoReturn:
        diagnosis, unavailable = None, None
        if not result.get("client_error") and HA_RENDER_TIMEOUT_TEXT not in str(
            result.get("error", "")
        ):
            diagnosis, unavailable = await self._diagnose_template(
                template, variables, strict, timeout
            )
        _raise_render_failure(template, report_errors, result, diagnosis, unavailable)
        raise AssertionError("unreachable")

    async def _diagnose_template(
        self,
        template: str,
        variables: dict[str, Any] | None,
        strict: bool,
        timeout: int,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Ask the ha_mcp_tools component where in the template the render fails.

        Home Assistant reports a template error without its line. The component
        renders the same template in-process, where the Jinja exception still
        carries it. Returns ``(diagnosis, None)``, or ``(None, reason)`` when a
        component that advertises the capability could not answer; without
        the capability both are ``None`` and the error stands as Home
        Assistant reported it.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "template_diagnose"):
            return None, None
        params: dict[str, Any] = {
            "template": template,
            "strict": strict,
            "timeout": timeout,
            # The component may spend up to ``timeout`` in its guarded render.
            "_wait_timeout": timeout + 10,
        }
        if variables is not None:
            params["variables"] = variables
        try:
            ws = await get_websocket_client(
                url=self._client.base_url,
                token=self._client.token,
                verify_ssl=getattr(self._client, "verify_ssl", None),
            )
            raw = await ws.send_command("ha_mcp_tools/template_diagnose", **params)
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
                return None, None
            logger.warning("ha_mcp_tools/template_diagnose failed: %r", exc)
            return None, f"line lookup failed: {exc}"
        except Exception as exc:
            logger.warning("ha_mcp_tools/template_diagnose unavailable", exc_info=True)
            return None, f"line lookup failed: {exc}"
        diagnosis = raw.get("result")
        if not isinstance(diagnosis, dict):
            return None, "line lookup returned no diagnosis"
        return diagnosis, None

    async def _test_condition(
        self,
        condition: dict[str, Any],
        variables: dict[str, Any] | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"condition": condition}
        if variables is not None:
            params["variables"] = variables
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        try:
            response = await ws.send_command("test_condition", **params)
        except HomeAssistantCommandError as exc:
            raise_tool_error(
                condition_failure(condition, _command_error_message(exc), exc.code)
            )
            raise AssertionError("unreachable: raise_tool_error always raises") from exc
        return condition_result(condition, response.get("result") or {})


def register_utility_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    tools = LegacyTemplateTools(client)
    mcp.add_tool(tools.ha_eval_template)


class LegacyTemplateTools(TemplateTools):
    def __init__(self, client: Any) -> None:
        super().__init__(UtilityTools(client).eval_template)

    ha_eval_template = tool(
        tags={"Utilities"},
        annotations=read_only_hints("Evaluate Template", open_world=False),
    )(log_tool_usage(TemplateTools.ha_eval_template))


__all__ = [
    "UtilityTools",
    "register_utility_tools",
]
