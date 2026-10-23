"""Bounded recorder reads on Core's dedicated database executor."""

import json
from collections.abc import Callable
from contextlib import suppress
from functools import lru_cache
from typing import Any, Literal, get_args, get_origin, get_type_hints

from ha_mcp.tool_api import ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io

from .command_validation import _validated_payload
from .registry_access import authorize_actor


@lru_cache(maxsize=4)
def default_statistic_types(owner: Callable[..., Any]) -> frozenset[str]:
    """Core's query requires explicit types; request its declared complete set.

    Core 2026.10's WS default is all supported fields. Resolve that bounded
    Literal from the installed owner instead of duplicating an enum here.
    """
    with suppress(KeyError, IndexError, NameError, TypeError):
        annotation = get_type_hints(owner)["types"]
        inner = get_args(annotation)[0]
        values = get_args(inner)
        if (
            get_origin(annotation) is set
            and get_origin(inner) is Literal
            and values
            and all(isinstance(value, str) for value in values)
        ):
            return frozenset(values)
    raise UnsupportedOperation("Core statistics default types cannot be resolved")


def _dates(payload: dict[str, Any]) -> dict[str, Any]:
    from homeassistant.util import dt as dt_util

    result = dict(payload)
    for key in ("start_time", "end_time"):
        if key not in result:
            continue
        parsed = dt_util.parse_datetime(result[key])
        if parsed is None:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER", f"Invalid {key}", status="not_started"
            )
        result[key] = dt_util.as_utc(parsed)
    return result


def _parameters(hass: Any, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return _validated_payload(hass, command, arguments)
    except ToolFailure as error:
        detail = error.data.get("error")
        if not error.code.startswith("VALIDATION_") or not isinstance(detail, dict):
            raise
        message = f"Invalid recorder parameters: {error}"
        code = (
            "VALIDATION_INVALID_PARAMETER"
            if error.code == "VALIDATION_FAILED"
            else error.code
        )
        payload = {
            **error.data,
            "error": {**detail, "code": code, "message": message},
        }
        raise ToolFailure(code, message, data=payload, status=error.status) from error


class RecorderQueries:
    def __init__(self, service: Any) -> None:
        self.service = service

    async def _query(
        self, owner: Callable[..., Any], arguments: dict[str, Any]
    ) -> dict[str, Any]:
        from homeassistant.components.recorder import get_instance
        from homeassistant.helpers.json import json_bytes

        service = self.service
        await authorize_actor(service.actor, service.authorize)
        try:
            recorder = get_instance(service.hass)
        except KeyError as error:
            raise UnsupportedOperation("Core recorder is not loaded") from error

        def read() -> dict[str, Any]:
            # Core also serializes inside this executor: rows can contain lazy
            # state objects tied to database models. Never move those to HA's loop.
            result: dict[str, Any] = json.loads(
                json_bytes(owner(service.hass, **arguments))
            )
            return result

        result: dict[str, Any] = await await_owned_io(
            recorder.async_add_executor_job(read)
        )
        await authorize_actor(service.actor, service.authorize)
        return result

    async def history(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from homeassistant.components.recorder.history import get_significant_states
        from homeassistant.core import valid_entity_id
        from homeassistant.util import dt as dt_util

        service = self.service
        payload = _dates(
            _parameters(service.hass, "history/history_during_period", arguments)
        )
        for entity_id in payload["entity_ids"]:
            if service.hass.states.get(entity_id) is None and not valid_entity_id(
                entity_id
            ):
                raise ToolFailure(
                    "SERVICE_CALL_FAILED",
                    "Invalid entity_ids",
                    status="not_started",
                )
        if payload["start_time"] > dt_util.utcnow():
            return {}
        if "compressed_state_format" in payload or "filters" in payload:
            raise UnsupportedOperation("history result representation overrides")
        return await self._query(
            get_significant_states, {**payload, "compressed_state_format": True}
        )

    async def statistics(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        payload = _dates(
            _parameters(
                self.service.hass, "recorder/statistics_during_period", arguments
            )
        )
        payload["statistic_ids"] = set(payload["statistic_ids"])
        payload.setdefault("units", None)
        if payload.get("types") is None:
            payload["types"] = set(default_statistic_types(statistics_during_period))

        def read(hass: Any, **query: Any) -> dict[str, Any]:
            rows: dict[str, Any] = statistics_during_period(hass, **query)
            # The public tool's existing rows use milliseconds. Core's Python
            # owner returns seconds; its WS adapter makes this same conversion.
            for values in rows.values():
                for row in values:
                    for key in ("start", "end", "last_reset"):
                        if row.get(key) is not None:
                            row[key] = int(row[key] * 1000)
            return rows

        return await self._query(read, payload)
