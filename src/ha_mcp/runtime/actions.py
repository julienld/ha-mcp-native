"""Shared ha_call_service orchestration and signature for both backends."""

import logging
from typing import Annotated, Any

from pydantic import Field

from ha_mcp.tool_api import BackendTools, ToolFailure
from ha_mcp.tools.coercion import JSON_STRING_COERCION

from .action_constants import (
    _NON_STATE_CHANGING_DOMAINS,
    _SERVICE_TO_STATE,
    _STATE_CHANGING_SERVICES,
)
from .action_params import ActionParameters
from .action_projection import ActionProjection
from .websocket_policy import validate_ws_request

_LOGGER = logging.getLogger(__name__)


class ServiceCallTools(ActionParameters, ActionProjection, BackendTools):
    # Only the legacy adapter can return None from its capability probe; it
    # supplies these historical transport hooks for that never-dispatched path.
    _validate_entity_before_wait: Any
    _call_raw_service: Any
    _verify_state_change: Any

    def _require_write_access(self) -> None:
        if self.context.read_only:
            raise ToolFailure("READ_ONLY", "Writes are disabled for this caller.")

    async def _check_service_policy(self, domain: str, service: str) -> None:
        await self.backend.execute(
            "services.policy", {"domain": domain, "service": service}, self.context
        )

    async def _call_ws_command(
        self, ws_command: str, data: Any, *, domain: str | None, service: str | None
    ) -> dict[str, Any]:
        try:
            command_type, command_params = validate_ws_request(
                ws_command, data, domain=domain, service=service
            )
        except ToolFailure as error:
            error.status = "not_started"
            raise
        result: dict[str, Any] = await self.backend.execute(
            "services.websocket_command",
            {
                "ws_command": command_type,
                "data": command_params or None,
                "domain": domain,
                "service": service,
            },
            self.context,
        )
        return result

    def _map_service_error(self, error: Exception, **kwargs: Any) -> dict[str, Any]:
        raise error

    async def _maybe_component_call_service(
        self,
        *,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        entity_id: str | None,
        data: Any,
        should_wait: bool,
        return_response: bool,
        verbose: bool,
        fields: list[str] | None,
        attribute_keys: list[str] | None,
    ) -> dict[str, Any] | None:
        result = await self.backend.execute(
            "services.call_observe",
            {
                "domain": domain,
                "service": service,
                "service_data": service_data,
                "entity_ids": [part.strip() for part in entity_id.split(",")]
                if entity_id
                else [],
                "wait": should_wait,
                "timeout": 10.0,
                "return_response": return_response,
                "expected_state": _SERVICE_TO_STATE.get(service),
                "include_changed_states": verbose
                or not should_wait
                or bool(entity_id and "," in entity_id),
            },
            self.context,
        )
        if result.get("unknown_outcome"):
            return self._build_timeout_response(
                domain, service, entity_id, data, return_response=return_response
            )
        return self._build_component_call_response(
            result,
            domain=domain,
            service=service,
            entity_id=entity_id,
            data=data,
            should_wait=should_wait,
            return_response=return_response,
            verbose=verbose,
            fields=fields,
            attribute_keys=attribute_keys,
        )

    async def ha_call_service(
        self,
        domain: Annotated[
            str | None,
            Field(
                description=(
                    "Service domain (e.g. 'light', 'climate', 'automation'). Required for a"
                    " service call."
                ),
            ),
        ] = None,
        service: Annotated[
            str | None,
            Field(
                description=(
                    "Service name within domain (e.g. 'turn_on', 'set_temperature', "
                    "'trigger'). Required for a service call."
                ),
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                description=(
                    "Entity ID(s) the service call targets — one ID ('light.living_room') "
                    "or several comma-separated ('light.a,light.b'). Optional for services "
                    "that don't target a specific entity."
                ),
            ),
        ] = None,
        data: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Extra service-call parameters beyond entity_id (e.g. "
                    "{'temperature': 22} for climate.set_temperature). Also carries "
                    "the raw command payload when ws_command is set. If entity_id "
                    "is also present in data, the entity_id parameter wins."
                ),
            ),
        ] = None,
        return_response: Annotated[
            bool,
            Field(
                description=(
                    "If True, the service's response data is returned once, as the "
                    "top-level 'service_response' key — never nested inside 'result'."
                ),
            ),
        ] = False,
        wait: Annotated[
            bool,
            Field(
                description=(
                    "If True (default), wait for the entity state to change "
                    "before returning. Applies only to state-changing services "
                    "called with a single entity_id. A comma-separated "
                    "multi-target does not get confirmed by this: it falls "
                    "through to a legacy path that polls for the literal "
                    "composite entity_id and times out after 10s. Set "
                    "wait=False for multi-target calls."
                ),
            ),
        ] = True,
        verbose: Annotated[
            bool,
            Field(
                description=(
                    "Return HA's raw changed-state records unchanged. With "
                    "return_response=True the response data still surfaces once as the "
                    "top-level service_response key, never nested in result. Large for "
                    "nested-group targets — prefer result_fields / result_attribute_keys."
                ),
            ),
        ] = False,
        result_fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Project each record in 'result' to only these top-level keys (e.g. "
                    "['entity_id', 'state']). Setting this DISABLES default compaction — no"
                    " entity-id filter, no metadata strip — and applies the explicit "
                    "projection instead."
                ),
            ),
        ] = None,
        result_attribute_keys: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Project each record's 'attributes' dict to only these keys (e.g. "
                    "['brightness', 'rgb_color']). Setting this DISABLES default "
                    "compaction. Requires 'attributes' to be present in result_fields (or "
                    "result_fields=None)."
                ),
            ),
        ] = None,
        ws_command: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Advanced escape hatch: send a raw one-shot Home Assistant "
                    "WebSocket command that is NOT a registered service and that "
                    "no dedicated tool covers. When set, "
                    "omit domain/service and the other service params. "
                    "Streaming/two-phase and "
                    "service-invoking commands (call_service, execute_script) are "
                    "rejected."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Call any Home Assistant service or one-shot WebSocket command: the catch-all escape hatch.

        When NOT to use: a dedicated tool that covers the job. It validates the
        request, reports the outcome in its own terms, and stays available where
        a security policy gates this tool. For example: turn an automation on or
        off or run it now with ha_config_set_automation (enabled / run_actions);
        start or stop a script with ha_config_set_script (run); activate a scene
        with ha_config_set_scene (activate); manage apps (add-ons) with
        ha_manage_app; handle updates and Repairs issues with ha_manage_updates;
        set an integration's log level with ha_set_integration (log_level).

        When to use: a service no dedicated tool covers. Services follow the
        pattern domain.service (e.g., light.turn_on, climate.set_temperature);
        ha_list_services lists them with their fields.

        EXAMPLES:
        - ha_call_service("light", "turn_on", entity_id="light.living_room")
        - ha_call_service("climate", "set_temperature", entity_id="climate.thermostat", data={"temperature": 22})

        Result compaction (default ON): ``result`` is trimmed to the targeted
        entity's record (drops parent-group propagation) and stripped of
        ``context`` / ``last_*`` metadata and heavy attribute lists
        (``effect_list``, ``hue_scenes``).

        For detailed service documentation, use ha_get_skill_guide.

        **WebSocket command escape hatch (advanced):**
        Some Home Assistant operations are WebSocket-only commands, not
        registered services. Pass ``ws_command`` (instead of domain/service) to
        send one no dedicated tool covers, with its parameters in ``data``:
        ha_call_service(ws_command="<command type>", data={...}). Only one-shot
        request/response commands are supported, and the other service
        parameters (entity_id, return_response, etc.) don't apply.

        Unavailable in Read Only Mode, including read-like services and WebSocket
        commands. Use dedicated read tools while that mode is enabled.
        """
        # Guard the escape hatch itself as well as the MCP catalog/middleware:
        # direct dispatch must never reach either transport in read-only mode.
        self._require_write_access()

        # WebSocket-command escape hatch (issue #1839): reach one-shot WS
        # commands that aren't registered services.
        if ws_command is not None:
            self._reject_incompatible_ws_params(
                entity_id,
                return_response,
                verbose,
                result_fields,
                result_attribute_keys,
            )
            return await self._call_ws_command(
                ws_command, data, domain=domain, service=service
            )

        domain, service = self._validate_service_call_params(domain, service)
        try:
            await self._check_service_policy(domain, service)
            service_data = self._parse_service_data(data, entity_id)

            return_response_bool = return_response
            wait_bool = wait
            verbose_bool = verbose
            parsed_result_fields, parsed_result_attribute_keys = (
                self._parse_result_projection_params(
                    result_fields, result_attribute_keys
                )
            )

            # Determine if we should wait for state change:
            # Only for state-changing services with an entity_id set, not for
            # trigger/reload/fire-and-forget services or services without entities.
            # A comma-separated multi-target entity_id is not excluded here — it
            # still falls through to the legacy path, which polls for the literal
            # composite entity_id and times out after 10s (see the wait Field
            # description and _maybe_component_call_service).
            # This server-side decision (D6) also chooses whether to hand the
            # component wait+entity_ids: a non-state-changing call passes wait
            # implicitly false and no confirmation targets.
            should_wait = (
                wait_bool
                and entity_id is not None
                and service in _STATE_CHANGING_SERVICES
                and domain not in _NON_STATE_CHANGING_DOMAINS
            )

            # Route a confirmable single call through the component capability (D8);
            # a returned response means it served the call, None means fall through to
            # the legacy REST path below (a safe first fire, D9 at-most-once).
            component_response = await self._maybe_component_call_service(
                domain=domain,
                service=service,
                service_data=service_data,
                entity_id=entity_id,
                data=data,
                should_wait=should_wait,
                return_response=return_response_bool,
                verbose=verbose_bool,
                fields=parsed_result_fields,
                attribute_keys=parsed_result_attribute_keys,
            )
            if component_response is not None:
                return component_response

            # Legacy REST path (component absent, or it never dispatched): capture
            # initial state before the call for the WS-subscribe verification. A
            # settled 404 (single-entity target definitely does not exist) raises
            # here, BEFORE the dispatch below, so a known-invalid call never
            # reaches the service POST or the 10s confirmation wait. An
            # "unavailable" target is NOT fast-failed here -- dispatching is
            # exactly what might reconnect it; see _verify_state_change for its
            # post-dispatch classification instead.
            initial_state = None
            if should_wait:
                initial_state = await self._validate_entity_before_wait(entity_id)

            result = await self._call_raw_service(
                domain, service, service_data, return_response=return_response_bool
            )

            # Peel the return_response envelope apart BEFORE projection so the
            # response data is emitted once, top-level, and only the changed
            # states reach ``result`` (issue #2085).
            result, service_response, has_response_envelope, envelope_warnings = (
                self._split_return_response_envelope(
                    result, return_response=return_response_bool
                )
            )

            projected_result, projection_warnings = self._project_service_result(
                result,
                entity_id=entity_id,
                verbose=verbose_bool,
                fields=parsed_result_fields,
                attribute_keys=parsed_result_attribute_keys,
            )

            response: dict[str, Any] = {
                "success": True,
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
                "parameters": data,
                "result": projected_result,
                "message": f"Successfully executed {domain}.{service}",
            }
            call_warnings = [*projection_warnings, *envelope_warnings]
            if call_warnings:
                response.setdefault("warnings", []).extend(call_warnings)

            if has_response_envelope:
                response["service_response"] = service_response

            # Wait for entity state to change
            if should_wait and entity_id is not None:
                await self._verify_state_change(
                    entity_id,
                    service,
                    initial_state,
                    response,
                )

            return response
        except Exception as error:
            _LOGGER.debug("Mapping call-service failure", exc_info=True)
            return self._map_service_error(
                error,
                domain=domain,
                service=service,
                entity_id=entity_id,
                data=data,
                return_response=return_response,
            )
