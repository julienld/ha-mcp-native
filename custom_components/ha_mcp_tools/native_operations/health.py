"""Canonical system-health reads through the registered Core owners."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_bytes

from ha_mcp.errors import HomeAssistantAPIError
from ha_mcp.runtime.diagnostics import (
    _project_cap_and_paginate_diagnostics,
    parse_diagnostics_fields,
)
from ha_mcp.runtime.health_results import (
    HealthResults,
    dead_entities_result,
    repair_section,
    summarize_theme_listing,
    thread_network_result,
    zha_network_result,
    zwave_network_result,
)
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io

from .health_info import read_health_info
from .health_radios import thread_status, zwave_status
from .integration_inspection import diagnostics
from .registry_access import Authorize, authorize_actor, registry_failure


@dataclass(slots=True)
class NativeHealth(HealthResults):
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)

    async def get(
        self,
        include: str | None,
        include_dismissed_repairs: bool,
        config_entry_id: str | None,
        device_id: str | None,
        diagnostics_fields: list[str] | str | None,
        diagnostics_truncate_at_bytes: int | None,
        diagnostics_data_path: str | None,
        diagnostics_data_offset: int | None,
        diagnostics_data_limit: int | None,
    ) -> dict[str, Any]:
        try:
            await authorize_actor(self.actor, self.authorize)
            includes = self._parse_includes(include)
            try:
                result = await read_health_info(self.hass, self.actor, self.authorize)
            except UnsupportedOperation:
                if not includes.intersection(
                    {
                        "repairs",
                        "zha_network",
                        "zha_network_full",
                        "zwave_network",
                        "thread_network",
                        "matter_network",
                        "themes",
                        "diagnostics",
                        "config_check",
                        "dead_entities",
                    }
                ):
                    raise
                result = {
                    "success": True,
                    "baseline_available": False,
                    "health_info": {},
                    "component_count": 0,
                    "message": "System health baseline unavailable.",
                    "warnings": [
                        "The system_health integration is not loaded; returning independently available Core sections."
                    ],
                }
            self._warn_unknown_includes(result, includes)
            fields = parse_diagnostics_fields(diagnostics_fields)
            snapshot = self._snapshot(includes)
            sections = self._sections(includes, snapshot, include_dismissed_repairs)
            if "diagnostics" in includes:
                sections.append(
                    (
                        "diagnostics",
                        lambda: self._diagnostics(
                            config_entry_id,
                            device_id,
                            fields,
                            diagnostics_truncate_at_bytes,
                            diagnostics_data_path,
                            diagnostics_data_offset or 0,
                            diagnostics_data_limit,
                        ),
                    )
                )
            elif (
                config_entry_id
                or device_id
                or diagnostics_fields is not None
                or diagnostics_truncate_at_bytes is not None
                or diagnostics_data_path is not None
                or diagnostics_data_limit is not None
                or (diagnostics_data_offset or 0) > 0
            ):
                result.setdefault("warnings", []).append(
                    "config_entry_id, device_id, diagnostics_fields, diagnostics_truncate_at_bytes, diagnostics_data_path, diagnostics_data_offset, and/or diagnostics_data_limit were provided but ignored because 'diagnostics' is not in include"
                )
            values = await await_owned_io(
                asyncio.gather(
                    *(self._section(name, read) for name, read in sections),
                    return_exceptions=True,
                )
            )
            for (name, _read), value in zip(sections, values, strict=True):
                if isinstance(value, BaseException):
                    raise value
                if warnings := value.pop("_warnings", None):
                    result.setdefault("warnings", []).extend(warnings)
                result[name] = value
            await authorize_actor(self.actor, self.authorize)
            return result
        except Exception as error:
            raise registry_failure(error, {"include": include}) from error

    def _snapshot(self, includes: set[str]) -> dict[str, Any] | Exception:
        if not includes.intersection(
            {"repairs", "zwave_network", "matter_network", "dead_entities"}
        ):
            return {}
        from ..websocket_api.system import _do_system_snapshot

        try:
            captured = _do_system_snapshot(
                self.hass,
                {
                    "include_issues": "repairs" in includes,
                    "include_config_entries": bool(
                        includes.intersection(
                            {"zwave_network", "matter_network", "dead_entities"}
                        )
                    ),
                    "include_entities": "dead_entities" in includes,
                    "include_states": "dead_entities" in includes,
                },
            )
            result: dict[str, Any] = json.loads(json_bytes(captured))
            return result
        except Exception as error:  # noqa: BLE001 - preserve acquisition failure for dependent sections
            return error

    def _sections(
        self,
        includes: set[str],
        snapshot: dict[str, Any] | Exception,
        include_dismissed: bool,
    ) -> list[tuple[str, Callable[[], Awaitable[dict[str, Any]]]]]:
        sections: list[tuple[str, Callable[[], Awaitable[dict[str, Any]]]]] = []
        if "repairs" in includes:
            sections.append(
                ("repairs", lambda: self._repairs(snapshot, include_dismissed))
            )
        if includes.intersection({"zha_network", "zha_network_full"}):
            sections.append(
                ("zha_network", lambda: self._zha("zha_network_full" in includes))
            )
        if "zwave_network" in includes:
            sections.append(("zwave_network", lambda: self._zwave(snapshot)))
        if "thread_network" in includes:
            sections.append(("thread_network", self._thread))
        if "matter_network" in includes:
            sections.append(("matter_network", lambda: self._matter(snapshot)))
        if "themes" in includes:
            sections.append(("themes", self._themes))
        if "dead_entities" in includes:
            sections.append(("dead_entities", lambda: self._dead(snapshot)))
        if "config_check" in includes:
            sections.append(("config_check", self._config_check))
        return sections

    async def _section(
        self, name: str, read: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        try:
            await authorize_actor(self.actor, self.authorize)
            result = await read()
            await authorize_actor(self.actor, self.authorize)
            return result
        except ToolFailure as error:
            if error.code == "AUTH_FORBIDDEN":
                raise
            return {
                **_empty_section(name),
                "error": str(error),
                "error_code": error.code,
            }
        except Exception as error:  # noqa: BLE001 - independent health sections embed provider failures
            return {**_empty_section(name), "error": f"{name} not available: {error}"}

    @staticmethod
    def _rows(snapshot: dict[str, Any] | Exception, name: str) -> list[dict[str, Any]]:
        if isinstance(snapshot, Exception):
            raise snapshot
        result: list[dict[str, Any]] = snapshot[name]
        return result

    async def _repairs(
        self, snapshot: dict[str, Any] | Exception, dismissed: bool
    ) -> dict[str, Any]:
        return repair_section(self._rows(snapshot, "issues"), dismissed)

    async def _zha(self, full: bool) -> dict[str, Any]:
        from ..native_registry_ops import _zha_devices

        rows = await await_owned_io(_zha_devices(self.hass, {}))
        return zha_network_result(json.loads(json_bytes(rows)), full)

    async def _zwave(self, snapshot: dict[str, Any] | Exception) -> dict[str, Any]:
        entry = next(
            (
                row
                for row in self._rows(snapshot, "config_entries")
                if row.get("domain") == "zwave_js"
            ),
            None,
        )
        if entry is None:
            raise ToolFailure("RADIO_UNAVAILABLE", "Z-Wave JS integration not found")
        value = await zwave_status(
            self.hass, entry["entry_id"], self.actor, self.authorize
        )
        return zwave_network_result(json.loads(json_bytes(value)))

    async def _thread(self) -> dict[str, Any]:
        return thread_network_result(
            await thread_status(self.hass, self.actor, self.authorize)
        )

    async def _matter(self, snapshot: dict[str, Any] | Exception) -> dict[str, Any]:
        entry = next(
            (
                row
                for row in self._rows(snapshot, "config_entries")
                if row.get("domain") == "matter"
            ),
            None,
        )
        if entry is None:
            raise ToolFailure("RADIO_UNAVAILABLE", "Matter integration not found")
        return {
            "config_entry_id": entry.get("entry_id"),
            "state": entry.get("state"),
            "title": entry.get("title"),
        }

    async def _themes(self) -> dict[str, Any]:
        from homeassistant.components.frontend import (
            DATA_DEFAULT_DARK_THEME,
            DATA_DEFAULT_THEME,
            DATA_THEMES,
        )

        if self.hass.config.recovery_mode or self.hass.config.safe_mode:
            return summarize_theme_listing({"themes": {}, "default_theme": "default"})
        return summarize_theme_listing(
            {
                "themes": self.hass.data[DATA_THEMES],
                "default_theme": self.hass.data[DATA_DEFAULT_THEME],
                "default_dark_theme": self.hass.data.get(DATA_DEFAULT_DARK_THEME),
            }
        )

    async def _dead(self, snapshot: dict[str, Any] | Exception) -> dict[str, Any]:
        entries = self._rows(snapshot, "config_entries")
        rows = self._rows(snapshot, "entities")
        states = {
            state["entity_id"]: state
            for state in self._rows(snapshot, "states")
            if isinstance(state, dict) and state.get("entity_id")
        }
        live = (
            {entry["entry_id"] for entry in entries if entry.get("entry_id")}
            if entries
            else None
        )
        result: dict[str, Any] = await await_owned_io(
            self.hass.async_add_executor_job(dead_entities_result, states, rows, live)
        )
        result["config_entries_checked"] = bool(entries)
        if not entries:
            result["_warnings"] = [
                "No integrations are configured; config_entry_orphans detection was skipped. stale_restored was still computed."
            ]
        return result

    async def _config_check(self) -> dict[str, Any]:
        from homeassistant.helpers import check_config

        checked = await await_owned_io(
            check_config.async_check_ha_config_file(self.hass)
        )
        return {
            "result": "invalid" if checked.errors else "valid",
            "is_valid": not bool(checked.errors),
            "errors": checked.error_str or [],
            "warnings": checked.warning_str or [],
        }

    async def _diagnostics(
        self,
        entry_id: str | None,
        device_id: str | None,
        fields: list[str] | None,
        cap: int | None,
        path: str | None,
        offset: int,
        limit: int | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"config_entry_id": entry_id}
        if device_id:
            result["device_id"] = device_id
        if not entry_id:
            result["error"] = (
                "config_entry_id is required for diagnostics fetch. Use ha_get_integration() to find the config_entry_id for the integration."
            )
            return result
        try:
            result["data"] = await await_owned_io(
                diagnostics(
                    self.hass,
                    {"entry_id": entry_id, "device_id": device_id, "timeout": 60},
                )
            )
        except HomeAssistantAPIError as error:
            if error.status_code != 404:
                result["error"] = f"Diagnostics fetch failed: {error}"
                return result
            scope = "device" if device_id else "config entry"
            result["error"] = (
                f"Diagnostics not available for this {scope}: integration may not implement the diagnostics platform, or the id is invalid. Verify via ha_get_integration()."
            )
            return result
        except ToolFailure as error:
            if error.code == "AUTH_FORBIDDEN":
                raise
            result.update(error=str(error), error_code=error.code)
            return result
        except Exception as error:  # noqa: BLE001 - preserve target identity on independent provider failure
            result["error"] = f"Diagnostics fetch failed: {error}"
            return result
        if result["data"] is None:
            result.pop("data")
            result["error"] = "Diagnostics endpoint returned an empty body"
            return result
        await await_owned_io(
            self.hass.async_add_executor_job(
                _project_cap_and_paginate_diagnostics,
                result,
                fields,
                cap,
                path,
                offset,
                limit,
            )
        )
        return result


def _empty_section(name: str) -> dict[str, Any]:
    if name == "repairs":
        return {"issues": [], "count": 0}
    if name == "zha_network":
        return {"devices": [], "count": 0, "total_count": 0}
    if name == "zwave_network":
        return {"controller": {}, "nodes": [], "count": 0, "total_count": 0}
    if name == "thread_network":
        return {"border_routers": [], "count": 0}
    if name == "themes":
        return {
            "themes": [],
            "count": 0,
            "default_theme": None,
            "default_dark_theme": None,
        }
    if name == "config_check":
        return {"result": "unknown", "is_valid": False, "errors": []}
    return {}
