"""Read execution traces through Core owners and their original serializers."""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from homeassistant.core import HomeAssistant

from ha_mcp.runtime.progress import report_progress
from ha_mcp.runtime.trace_results import (
    _diagnostic_suggestion,
    _format_detailed_trace,
    _format_trace_list,
    trace_target,
)
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io

from .registry_access import Authorize, authorize_actor, registry_failure


@dataclass(slots=True)
class NativeTraces:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)

    async def get(
        self,
        automation_id: str,
        run_id: str | None,
        limit: int,
        deduplicate: bool,
        detailed: bool,
        sections: str | None,
        offset: int,
        order: Literal["newest", "oldest"],
        ctx: Any = None,
    ) -> dict[str, Any]:
        try:
            await authorize_actor(self.actor, self.authorize)
            domain, object_id = trace_target(automation_id)
            await report_progress(
                self.actor.progress,
                progress=0,
                total=3,
                message="resolving trace target",
            )
            from homeassistant.helpers import entity_registry as er

            entry = er.async_get(self.hass).async_get(automation_id)
            item_id = str(entry.unique_id) if entry and entry.unique_id else object_id
            await report_progress(
                self.actor.progress,
                progress=1,
                total=3,
                message=f"fetching trace {'detail' if run_id else 'list'}",
            )
            await authorize_actor(self.actor, self.authorize)
            encoded, empty = await await_owned_io(self._read(domain, item_id, run_id))
            await authorize_actor(self.actor, self.authorize)
            diagnostics = self._diagnostics(automation_id, domain) if empty else None

            def format_result() -> dict[str, Any]:
                snapshot = json.loads(encoded)
                if run_id:
                    return _format_detailed_trace(
                        automation_id,
                        run_id,
                        snapshot,
                        deduplicate=deduplicate,
                        detailed=detailed,
                        sections=sections,
                    )
                return _format_trace_list(
                    automation_id,
                    snapshot,
                    limit,
                    diagnostics,
                    offset=offset,
                    order=order,
                )

            result: dict[str, Any] = await await_owned_io(
                self.hass.async_add_executor_job(format_result)
            )
            await report_progress(
                self.actor.progress,
                progress=3,
                total=3,
                message="trace retrieval complete",
            )
            await authorize_actor(self.actor, self.authorize)
            return result
        except Exception as error:
            raise registry_failure(
                error,
                {
                    "automation_id": automation_id,
                    **({"run_id": run_id} if run_id else {}),
                },
            ) from error

    async def _read(
        self, domain: str, item_id: str, run_id: str | None
    ) -> tuple[bytes | str, bool]:
        from homeassistant.components.trace.const import DATA_TRACE, DATA_TRACE_STORE
        from homeassistant.components.trace.util import (
            async_get_trace,
            async_list_traces,
        )
        from homeassistant.helpers.json import ExtendedJSONEncoder, json_bytes

        if DATA_TRACE not in self.hass.data or DATA_TRACE_STORE not in self.hass.data:
            raise UnsupportedOperation("Core trace storage is not loaded")
        key = f"{domain}.{item_id}"
        if run_id:
            try:
                result = await async_get_trace(self.hass, key, run_id)
            except KeyError as error:
                raise ToolFailure(
                    "SERVICE_CALL_FAILED",
                    "The trace could not be found",
                    status="not_started",
                ) from error
            # Serialize live TraceElements on the HA loop, as Core does. Only the
            # detached JSON is decoded/formatted in the executor.
            return json.dumps(result, cls=ExtendedJSONEncoder, allow_nan=False), False
        summaries = await async_list_traces(self.hass, domain, key)
        return json_bytes(summaries), not summaries

    def _diagnostics(self, entity_id: str, domain: str) -> dict[str, Any]:
        from homeassistant.components.trace.const import CONF_STORED_TRACES
        from homeassistant.helpers.json import json_bytes

        state = self.hass.states.get(entity_id)
        diagnostics: dict[str, Any] = {
            "automation_exists": state is not None,
            "automation_enabled": False,
            "trace_storage_enabled": None,
            "last_triggered": None,
            "suggestion": "",
        }
        if state is None:
            diagnostics["suggestion"] = (
                f"Could not find {entity_id}. Verify the entity_id is correct using ha_search()."
            )
            return diagnostics
        diagnostics["automation_enabled"] = (
            state.state == "on"
            if domain == "automation"
            else state.state in {"on", "off"}
        )
        diagnostics["last_triggered"] = state.attributes.get("last_triggered")
        if domain == "automation":
            from homeassistant.components.automation import DATA_COMPONENT

            component = self.hass.data.get(DATA_COMPONENT)
        else:
            from homeassistant.components.script.const import DOMAIN

            component = self.hass.data.get(DOMAIN)
        entity = component.get_entity(entity_id) if component is not None else None
        trace_config = getattr(entity, "_trace_config", None)
        if trace_config is not None and CONF_STORED_TRACES in trace_config:
            diagnostics["trace_storage_enabled"] = trace_config[CONF_STORED_TRACES] > 0
        if state.state not in {"on", "off"}:
            diagnostics["suggestion"] = f"The {domain} is currently {state.state}."
        elif diagnostics["trace_storage_enabled"] is False:
            diagnostics["suggestion"] = (
                f"Trace storage is disabled for this {domain}. "
                "Set 'trace.stored_traces' to a positive number in its config."
            )
        elif diagnostics["trace_storage_enabled"] is None:
            diagnostics["suggestion"] = (
                "No traces are stored. The loaded entity's trace configuration could not be read."
            )
        else:
            diagnostics["suggestion"] = _diagnostic_suggestion(diagnostics, domain)
        result: dict[str, Any] = json.loads(json_bytes(diagnostics))
        return result
