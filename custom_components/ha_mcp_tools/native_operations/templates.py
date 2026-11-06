"""One-shot rendering and condition checks through Core's actual owners."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.errors import raise_failure
from ha_mcp.runtime.template_results import (
    condition_failure,
    condition_result,
    raise_render_failure,
    render_success,
    validate_choice,
)
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io

from ..helper_collections import _INVALID_ERRORS
from ..template_diagnose import async_diagnose, describe_failure
from .command_validation import _validated_payload
from .owner import CoreOperationOwner


class NativeTemplates(CoreOperationOwner):
    """Retain failed cleanup callbacks so component shutdown can retry them."""

    def __init__(self, hass: Any, authorize: Any) -> None:
        super().__init__(hass, authorize)
        self.cleanups: dict[object, Any] = {}

    async def evaluate(
        self,
        context: CallContext,
        template: str | None,
        condition: dict[str, Any] | None,
        variables: dict[str, Any] | None,
        strict: bool,
        timeout: int,
        report_errors: bool,
    ) -> dict[str, Any]:
        validate_choice(template, condition)
        await self.authorize(context)
        if condition is not None:
            arguments: dict[str, Any] = {"condition": condition}
            if variables is not None:
                arguments["variables"] = variables
            response = await await_owned_io(self._condition(arguments, context))
            return condition_result(condition, response)
        assert template is not None
        arguments = {
            "template": template,
            "strict": strict,
            "timeout": timeout,
            "report_errors": report_errors,
        }
        if variables is not None:
            arguments["variables"] = variables
        payload = _validated_payload(self.hass, "render_template", arguments)
        result = await self._render(payload, context)
        if result.get("success"):
            return render_success(template, result)
        return raise_render_failure(
            template, report_errors, result, result.get("diagnosis")
        )

    async def _render(
        self, payload: dict[str, Any], context: CallContext
    ) -> dict[str, Any]:
        from homeassistant.exceptions import TemplateError
        from homeassistant.helpers.template import Template

        template = Template(payload["template"], self.hass)
        warnings: list[str] = []

        # The guard thread touches only this local list and Core joins it before
        # returning. No Home Assistant callback or state is called from there.
        @callback
        def log(level: int, message: str) -> None:
            if level < logging.ERROR and message not in warnings:
                warnings.append(message)

        try:
            # Core's own timeout probe owns and joins its render thread. Shield
            # cancellation until it returns; never abandon that thread on unload.
            timed_out = await await_owned_io(
                template.async_render_will_timeout(
                    payload["timeout"],
                    payload.get("variables"),
                    strict=payload["strict"],
                    log_fn=log if payload["report_errors"] else None,
                )
            )
            if timed_out:
                result: dict[str, Any] = {
                    "success": False,
                    "error": f"Exceeded maximum execution time of {payload['timeout']}s",
                    "error_code": "template_error",
                }
            else:
                await self.authorize(context)
                result = await self._track_render(template, payload, log)
        except TemplateError as error:
            result = self._render_error(payload, error)
        diagnosis = result.get("diagnosis")
        if isinstance(diagnosis, dict) and "line" not in diagnosis:
            # Core flattens worker failures into text. Reuse the bounded Core
            # diagnostic render only when that erased the caller's line.
            await self.authorize(context)
            result["diagnosis"] = await await_owned_io(
                async_diagnose(
                    self.hass,
                    payload["template"],
                    payload.get("variables"),
                    payload["strict"],
                    payload["timeout"],
                )
            )
        if warnings:
            result["warnings"] = warnings
        return result

    @staticmethod
    def _render_error(payload: dict[str, Any], error: BaseException) -> dict[str, Any]:
        return {
            "success": False,
            "error": str(error),
            "error_code": "template_error",
            "diagnosis": describe_failure(payload["template"], "render", error),
        }

    async def _track_render(
        self, template: Any, payload: dict[str, Any], log: Any
    ) -> dict[str, Any]:
        from homeassistant.exceptions import TemplateError
        from homeassistant.helpers.event import (
            TrackTemplate,
            async_track_template_result,
        )

        future: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()

        @callback
        def changed(_event: Any, updates: list[Any]) -> None:
            if future.done() or not updates:
                return
            value = updates[-1].result
            future.set_result(
                self._render_error(payload, value)
                if isinstance(value, TemplateError)
                else {"success": True, "result": value, "listeners": info.listeners}
            )

        info = async_track_template_result(
            self.hass,
            [TrackTemplate(template, payload.get("variables"))],
            changed,
            strict=payload["strict"],
            log_fn=log if payload["report_errors"] else None,
        )
        key = object()
        self.cleanups[key] = info.async_remove
        try:
            info.async_refresh()
            async with asyncio.timeout(payload["timeout"] + 2):
                return await future
        finally:
            info.async_remove()
            self.cleanups.pop(key, None)

    async def _condition(
        self, arguments: dict[str, Any], context: CallContext
    ) -> dict[str, Any]:
        from homeassistant.exceptions import HomeAssistantError
        from homeassistant.helpers import condition as owner
        from homeassistant.helpers import trace

        try:
            try:
                payload = _validated_payload(self.hass, "test_condition", arguments)
            except ToolFailure as error:
                if isinstance(error, UnsupportedOperation):
                    raise
                return raise_failure(
                    condition_failure(
                        arguments["condition"], str(error), "invalid_format"
                    )
                )
            validated = await owner.async_validate_condition_config(
                self.hass, payload["condition"]
            )
            condition = await owner.async_from_config(self.hass, validated)
        except _INVALID_ERRORS as error:
            return raise_failure(
                condition_failure(arguments["condition"], str(error), "invalid_format")
            )
        except HomeAssistantError as error:
            return raise_failure(
                condition_failure(
                    arguments["condition"], str(error), "home_assistant_error"
                )
            )
        key = object()
        self.cleanups[key] = condition.async_unload
        try:
            await self.authorize(context)
            condition_trace = trace.trace_get()
            with trace.suppress_template_error_logging():
                value = condition.async_check(variables=payload.get("variables"))
            result = {"result": value}
            errors = [
                message
                for elements in condition_trace.values()
                for element in elements
                for message in element.template_errors
            ]
            if errors:
                result["template_errors"] = errors
            return result
        except HomeAssistantError as error:
            return raise_failure(
                condition_failure(
                    arguments["condition"], str(error), "home_assistant_error"
                )
            )
        finally:
            condition.async_unload()
            self.cleanups.pop(key, None)

    async def stop(self) -> None:
        errors = []
        for key, cleanup in list(self.cleanups.items()):
            try:
                cleanup()
            except Exception as error:  # noqa: BLE001 - retain failed resources for retry
                errors.append(error)
            else:
                self.cleanups.pop(key, None)
        if errors:
            raise RuntimeError("native_template_resources_not_closed") from errors[0]


def template_definition(backend: Any) -> Any:
    """Bind the single public contract to one lifecycle-owned Core evaluator."""
    from functools import partial

    from ha_mcp.runtime.catalog import define_tool
    from ha_mcp.runtime.templates import TemplateTools
    from ha_mcp.tools.tool_hints import read_only_hints

    if backend._templates is None:
        backend._templates = NativeTemplates(backend.hass, backend.authorize)
    owner = backend._templates

    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        result = await TemplateTools(partial(owner.evaluate, actor)).ha_eval_template(
            **arguments
        )
        normalized: dict[str, Any] = json.loads(json_bytes(result))
        return normalized

    return define_tool(
        backend,
        TemplateTools,
        "ha_eval_template",
        handler=invoke,
        source_module="tools_utility",
        tags=frozenset({"Utilities"}),
        annotations=read_only_hints("Evaluate Template", open_world=False),
    )
