"""Canonical bulk control: Core topology, shared resolution and owned dispatch."""

import copy
import json
from dataclasses import dataclass, field
from functools import partial
from itertools import chain
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.errors import ErrorCode, create_error_response, create_validation_error
from ha_mcp.runtime.action_constants import _SERVICE_TO_STATE
from ha_mcp.runtime.bulk_params import _parse_bulk_operations
from ha_mcp.runtime.bulk_safety import (
    _attach_resolution_to_response,
    _expand_membership_transitively,
    _selector_only_parameter_message,
    _selector_only_parameter_offender,
    reject_group_member_conflicts,
)
from ha_mcp.runtime.bulk_selection import (
    MAX_SELECTOR_ENTITIES,
    BulkSelectorResolution,
    BulkSelectorValidationError,
    SelectorTopology,
    prepare_bulk_selection,
    resolve_bulk_selection,
)
from ha_mcp.runtime.bulk_values import BulkOperationValues
from ha_mcp.runtime.device_actions import (
    DeviceActionValues,
    entity_domain,
    validate_device_action,
)
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.runtime.progress import report_progress
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.tools.coercion import parse_json_param
from ha_mcp.utils.entity_membership import normalize_member_entity_ids
from ha_mcp.utils.owned_executor import await_owned_io

from ..native_visibility import hidden_from_core
from .bulk_dispatch import NativeBulkDispatch
from .registry_access import authorize_actor, registry_failure


@dataclass(slots=True)
class NativeBulkControl(DeviceActionValues, BulkOperationValues):
    manager: Any
    actor: CallContext
    targets: set[str] = field(default_factory=set, init=False)
    memberships: dict[str, frozenset[str]] = field(
        default_factory=dict, init=False, repr=False
    )
    serialized_duplicate_targets: bool = field(default=False, init=False)
    selector_mode: bool = field(default=False, init=False)

    async def _hidden(self) -> set[str]:
        config = await self.manager.visibility._config("ha_bulk_control")
        await authorize_actor(self.actor, self.manager.backend.authorize)
        return (
            hidden_from_core(self.manager.hass, config) if config is not None else set()
        )

    def _topology(self, *, include_registries: bool) -> SelectorTopology:
        hass = self.manager.hass
        states = [state.as_dict() for state in hass.states.async_all()]
        if not include_registries:
            return SelectorTopology(json.loads(json_bytes(states)), [], [], [], [])
        from homeassistant.components.config.floor_registry import _entry_dict
        from homeassistant.helpers import area_registry as ar
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        from homeassistant.helpers import floor_registry as fr

        devices = dr.async_get(hass)
        detached = json.loads(
            json_bytes(
                {
                    "states": states,
                    "entities": [
                        entry.as_partial_dict
                        for entry in er.async_get(hass).entities.values()
                    ],
                    "devices": [
                        json.loads(entry.json_repr)
                        for entry in chain(devices.devices, devices.child_devices)
                        if entry.json_repr is not None
                    ],
                    "areas": [
                        entry.json_fragment
                        for entry in ar.async_get(hass).areas.values()
                    ],
                    "floors": [
                        _entry_dict(entry)
                        for entry in fr.async_get(hass).floors.values()
                    ],
                }
            )
        )
        return SelectorTopology(**detached)

    async def _admit_targets(self, actor: CallContext) -> None:
        if actor != self.actor:
            raise PermissionError("Bulk actor cannot be replaced")
        hidden = await self._hidden()
        states = self._current_target_states()
        if any(
            frozenset(_expand_membership_transitively(target, states)) != expected
            for target, expected in self.memberships.items()
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "An aggregate target's membership changed after the batch was resolved; no additional action was sent.",
                status="not_started",
            )

        if self.selector_mode:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(self.manager.hass)
            hidden.update(
                entity_id
                for entity_id in self.targets
                if (entry := registry.async_get(entity_id)) is not None
                and entry.hidden_by is not None
            )
        if hidden & self.targets:
            raise ToolFailure(
                "ENTITY_VISIBILITY_ENFORCED",
                "The frozen bulk target set contains an entity hidden by current policy.",
                status="not_started",
            )

    def _current_target_states(self) -> dict[str, Any]:
        # Re-read only the reachable targets after admission I/O, not the whole
        # instance for every child in a large batch. The shared reducer still
        # owns the membership comparison itself.
        states: dict[str, Any] = {}
        seen: set[str] = set()
        pending = list(self.memberships)
        while pending:
            entity_id = pending.pop()
            if entity_id in seen:
                continue
            seen.add(entity_id)
            if (state := self.manager.hass.states.get(entity_id)) is None:
                continue
            states[entity_id] = state.as_dict()
            pending.extend(normalize_member_entity_ids(state.attributes) or ())
        return states

    async def control(
        self,
        operations: list[Any] | None = None,
        parallel: bool = True,
        ctx: Any = None,
        selector: dict[str, Any] | None = None,
        action: str | None = None,
        parameters: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        validate_first: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        try:
            hidden = await self._hidden()
            if (operations is None) == (selector is None):
                raise_failure(
                    create_validation_error(
                        "Provide exactly one of operations or selector; "
                        + (
                            "neither was given"
                            if operations is None
                            else "both were given"
                        ),
                        parameter="operations" if operations is None else "selector",
                    )
                )
            topology = self._topology(include_registries=selector is not None)
            resolution = None
            if selector is not None:
                self.selector_mode = True
                if action is None:
                    raise_failure(
                        create_validation_error(
                            "Selector mode requires action", parameter="action"
                        )
                    )
                parsed = parse_json_param(selector, "selector")
                if not isinstance(parsed, dict):
                    raise BulkSelectorValidationError("selector must be a JSON object")
                hidden |= {
                    entry["entity_id"]
                    for entry in topology.entities
                    if entry.get("hidden_by") is not None
                }
                prepared = prepare_bulk_selection(topology, parsed, action)
                resolution = await await_owned_io(
                    self.manager.hass.async_add_executor_job(
                        partial(
                            resolve_bulk_selection,
                            prepared,
                            hidden=hidden,
                            visibility_warnings=[],
                            parameters=parameters,
                            timeout_seconds=timeout_seconds,
                            validate_first=validate_first,
                        )
                    )
                )
                operations = resolution.operations
            else:
                offender = _selector_only_parameter_offender(
                    action=action,
                    parameters=parameters,
                    timeout_seconds=timeout_seconds,
                    validate_first=validate_first,
                    dry_run=dry_run,
                )
                if offender is not None:
                    raise_failure(
                        create_validation_error(
                            _selector_only_parameter_message(offender),
                            parameter=offender,
                        )
                    )
                operations = _parse_bulk_operations(operations)
                reject_group_member_conflicts(
                    {
                        op["entity_id"]
                        for op in operations
                        if isinstance(op, dict) and isinstance(op.get("entity_id"), str)
                    },
                    topology.states,
                )
            assert operations is not None
            if dry_run:
                assert resolution is not None
                if await self._hidden() - hidden:
                    raise ToolFailure(
                        "CONFIG_CONFLICT",
                        "Visibility changed while resolving the bulk preview.",
                        status="not_started",
                    )
                response = {"success": True, "dry_run": True, "dispatched": False}
                _attach_resolution_to_response(response, resolution)
                return response
            return await self._control_operations(
                operations, topology, parallel, resolution
            )
        except BulkSelectorValidationError as error:
            raise_failure(
                create_validation_error(str(error), parameter=error.parameter)
            )
        except Exception as error:
            raise registry_failure(
                error, {"operation": "bulk device control"}
            ) from error

    def _resolve(self, operation: dict[str, Any]) -> dict[str, Any]:
        from homeassistant.helpers import config_validation as cv

        entity_id, action = operation["entity_id"], operation["action"]
        domain = entity_domain(entity_id, action)
        validate_device_action(entity_id, domain, action)
        targets = cv.entity_ids(entity_id.split(","))
        call = self._build_service_call(
            entity_id, domain, action, copy.deepcopy(operation.get("parameters"))
        )
        return {
            "domain": call["domain"],
            "service": call["service"],
            "service_data": call["data"],
            "entity_ids": targets,
            "expected_state": _SERVICE_TO_STATE.get(call["service"]),
        }

    def _resolve_rows(
        self, operations: list[Any], skipped: list[dict[str, Any]]
    ) -> tuple[
        list[tuple[int, dict[str, Any], str, str]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        candidates = self._validate_bulk_operations(operations, skipped)
        valid, rows, results = [], [], []
        for index, operation, entity_id, action in candidates:
            try:
                row = self._resolve(operation)
            except Exception as error:  # noqa: BLE001 - invalid rows remain per-item failures
                failure = registry_failure(
                    error, {"entity_id": entity_id, "action": action}
                )
                results.append({**failure.data, "index": index})
            else:
                # Missing services are row failures; policy refusals and the
                # reserved component domain still fail the whole preflight.
                self.manager.backend._check_service_policy(
                    row["domain"], row["service"]
                )
                if (
                    row["domain"].strip().lower() != "ha_mcp_tools"
                    and not self.manager.hass.services.has_service(
                        row["domain"], row["service"]
                    )
                ):
                    results.append(
                        create_error_response(
                            ErrorCode.SERVICE_NOT_FOUND,
                            f"Service not found: {row['domain']}.{row['service']}",
                            context={
                                "entity_id": entity_id,
                                "action": action,
                                "index": index,
                            },
                        )
                    )
                    continue
                valid.append((index, operation, entity_id, action))
                rows.append(row)
        return valid, rows, results

    async def _control_operations(
        self,
        operations: list[Any],
        topology: SelectorTopology,
        parallel: bool,
        resolution: BulkSelectorResolution | None,
    ) -> dict[str, Any]:
        if not operations or len(operations) > MAX_SELECTOR_ENTITIES:
            raise_failure(
                create_validation_error(
                    f"Provide 1 to {MAX_SELECTOR_ENTITIES} operations",
                    parameter="operations",
                )
            )
        skipped: list[dict[str, Any]] = []
        valid, rows, results = self._resolve_rows(operations, skipped)
        if not valid:
            payload = create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"All {len(operations)} operation(s) failed validation; nothing was dispatched",
                context={"skipped_details": skipped, "results": results},
            )
            raise_failure(payload)
        self.targets = {entity_id for row in rows for entity_id in row["entity_ids"]}
        reject_group_member_conflicts(self.targets, topology.states)
        states = {state["entity_id"]: state for state in topology.states}
        self.memberships = {
            target: frozenset(_expand_membership_transitively(target, states))
            for target in self.targets
        }
        for members in self.memberships.values():
            self.targets.update(members)
        await self._admit_targets(self.actor)
        ids = self.manager.device_operations.reserve(
            self.actor, [op for _, op, _, _ in valid], rows
        )
        response = await await_owned_io(
            self._run_and_record(valid, rows, ids, parallel)
        )
        results.extend(response)
        result = self._build_bulk_response(operations, results, ids, skipped, parallel)
        if self.serialized_duplicate_targets:
            result["execution_mode"] = "sequential"
            result.setdefault("warnings", []).append(
                "Repeated entity targets were executed in request order so each action retains its own preimage and confirmation."
            )
        if resolution is not None:
            _attach_resolution_to_response(result, resolution)
        if any(row.get("operation_outcome") == "outcome_unknown" for row in results):
            result["partial"] = True
            result["warnings"] = [
                *result.get("warnings", []),
                "One or more attempted actions have an unknown outcome. They were not replayed; inspect current entities before deciding on another action.",
            ]
        return result

    async def _run_and_record(
        self,
        valid: list[tuple[int, dict[str, Any], str, str]],
        rows: list[dict[str, Any]],
        ids: list[str],
        parallel: bool,
    ) -> list[dict[str, Any]]:
        dispatch = NativeBulkDispatch(
            self.manager.hass,
            self.actor,
            authorize=self._admit_targets,
            service_policy=self.manager.backend._check_service_policy,
        )
        try:
            await report_progress(
                self.actor.progress,
                progress=0,
                total=len(valid),
                message="Dispatching Core device operations",
            )
            result = await dispatch.execute(
                rows, parallel=parallel, timeout=self._bulk_frame_timeout(valid)
            )
        except BaseException as error:
            for identifier in ids:
                self.manager.device_operations.finish(
                    identifier,
                    {
                        "attempted": getattr(error, "status", None) != "not_started",
                        "error": str(error),
                        "error_code": getattr(error, "code", "OUTCOME_UNKNOWN"),
                    },
                )
            raise
        results = []
        self.serialized_duplicate_targets = bool(
            result.get("serialized_duplicate_targets")
        )
        for (_index, operation, entity_id, action), row, identifier, outcome in zip(
            valid, rows, ids, result["operations"], strict=True
        ):
            self.manager.device_operations.finish(identifier, outcome)
            value = self._map_component_op_result(
                operation, entity_id, action, row, outcome
            )
            value["operation_id"] = identifier
            value["attempted"] = outcome["attempted"]
            value["operation_outcome"] = outcome["outcome"]
            if outcome["outcome"] == "outcome_unknown":
                value["error"] = {
                    "code": "OUTCOME_UNKNOWN",
                    "message": "The provider raised after the action was attempted. Inspect the target; no retry was performed.",
                }
            results.append(value)
        await report_progress(
            self.actor.progress,
            progress=len(valid),
            total=len(valid),
            message="Core batch drained; operation outcomes recorded",
        )
        return results
