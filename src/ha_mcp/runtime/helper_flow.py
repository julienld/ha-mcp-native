"""First bounded helper-flow path, using Core's form and flow manager."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import Backend, CallContext, UnsupportedOperation

from .errors import raise_failure
from .helper_schema import _helper_response

_LOGGER = logging.getLogger(__name__)


async def create_single_form_helper(
    backend: Backend,
    context: CallContext,
    helper_type: str,
    name: str | None,
    config: dict[str, Any] | None,
    *,
    wait: bool,
) -> dict[str, Any]:
    if helper_type != "utility_meter":
        raise UnsupportedOperation(f"helpers.flow.{helper_type}")
    values = dict(config or {})
    if name:
        values.setdefault("name", name)
    if not isinstance(values.get("name"), str) or not values["name"].strip():
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "name is required for create action.",
                context={"helper_type": helper_type},
            )
        )
    started = await backend.execute(
        "config.flow.start", {"kind": "config", "handler": helper_type}, context
    )
    flow_id = started["flow_id"]
    completed = False
    try:
        if started.get("type") != "form":
            raise UnsupportedOperation(
                f"helpers.flow.{helper_type}.{started.get('type')}"
            )
        fields = {
            field["name"]
            for field in started.get("data_schema", [])
            if isinstance(field, dict) and isinstance(field.get("name"), str)
        }
        result = await backend.execute(
            "config.flow.configure",
            {
                "flow_id": flow_id,
                "user_input": {
                    key: value for key, value in values.items() if key in fields
                },
            },
            context,
        )
        if result.get("type") != "create_entry":
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Home Assistant did not complete the helper flow.",
                    context={
                        "helper_type": helper_type,
                        "errors": result.get("errors"),
                        "data_schema": result.get("data_schema"),
                    },
                )
            )
        completed = True
    finally:
        if not completed:
            await _abort(backend, context, flow_id)
    entry = result["result"]
    entry_id, title = entry["entry_id"], entry["title"]
    entities, warnings = await _entry_entities(backend, context, entry_id, wait)
    return _helper_response(
        "create",
        helper_type,
        data={"entry_id": entry_id, "title": title},
        message=f"{helper_type} helper created successfully",
        method="config_flow",
        entry_id=entry_id,
        title=title,
        entity_ids=entities,
        warnings=warnings,
    )


async def _entry_entities(
    backend: Backend, context: CallContext, entry_id: str, wait: bool
) -> tuple[list[str], list[str]]:
    deadline = asyncio.get_running_loop().time() + (5 if wait else 0)
    while True:
        response = await backend.execute(
            "helpers.entities.for_entry", {"config_entry_id": entry_id}, context
        )
        entities = [
            entity["entity_id"]
            for entity in response["entities"]
            if entity.get("entity_id")
        ]
        if entities or asyncio.get_running_loop().time() >= deadline:
            return entities, response.get("warnings", [])
        await asyncio.sleep(0.2)


async def _abort(backend: Backend, context: CallContext, flow_id: str) -> None:
    try:
        await backend.execute("config.flow.abort", {"flow_id": flow_id}, context)
    except Exception:
        # A completed/revoked flow cannot be resumed; the owned backend also
        # expires and drains its remaining flows independently of this waiter.
        _LOGGER.debug("Helper flow cleanup could not abort %s", flow_id, exc_info=True)
