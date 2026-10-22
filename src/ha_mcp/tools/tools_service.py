"""
Service call and device operation tools for Home Assistant MCP server.

This module provides service execution and WebSocket-enabled operation monitoring tools.
"""

import logging
from typing import Any, NoReturn, cast

import httpx
from pydantic import SkipValidation

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors, legacy_errors_sync
from ..backends.remote_events import RemoteEvents
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import get_websocket_client
from ..errors import (
    ErrorCode,
    create_connection_error,
    create_entity_not_found_error,
    create_entity_unavailable_after_dispatch_error,
    create_error_response,
    create_validation_error,
)
from ..read_only import require_write_access
from ..runtime.action_constants import (
    _COMPONENT_RESPONSE_UNCONFIRMED_WARNING as _COMPONENT_RESPONSE_UNCONFIRMED_WARNING,
)
from ..runtime.action_constants import _NON_ENVELOPE_WARNING as _NON_ENVELOPE_WARNING
from ..runtime.action_constants import (
    _NON_STATE_CHANGING_DOMAINS as _NON_STATE_CHANGING_DOMAINS,
)
from ..runtime.action_constants import (
    _STATE_CHANGING_SERVICES as _STATE_CHANGING_SERVICES,
)
from ..runtime.action_params import (
    _parse_json_dict_param as _shared_parse_json_dict_param,
)
from ..runtime.actions import ServiceCallTools as SharedServiceCallTools
from ..runtime.bulk_params import BulkControlOperation as BulkControlOperation
from ..runtime.bulk_params import (
    _parse_bulk_operations as _shared_parse_bulk_operations,
)
from ..runtime.bulk_safety import (
    _INFRASTRUCTURE_ERROR_SUGGESTIONS,
    _attach_resolution_to_response,
    _selector_only_parameter_message,
    _selector_only_parameter_offender,
    reject_group_member_conflicts,
)
from ..runtime.bulk_safety import (
    _expand_membership_transitively as _expand_membership_transitively,
)
from ..runtime.bulk_safety import (
    _find_group_member_conflicts as _find_group_member_conflicts,
)
from ..runtime.bulk_safety import (
    _format_member_list as _format_member_list,
)
from ..runtime.bulk_safety import (
    _GroupConflict as _GroupConflict,
)
from ..runtime.bulk_tools import BulkFacade as _BulkContractTools
from ..runtime.event_tools import EventTools as _EventContractTools
from ..tool_api import ToolFailure
from .backup_access import guard_snapshot_route
from .bulk_selector import (
    BulkControlSelector,
    BulkSelectorInfrastructureError,
    BulkSelectorValidationError,
    InfrastructureErrorCause,
    resolve_bulk_selector,
)
from .coercion import parse_json_param
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import read_only_hints, write_hints
from .util_helpers import (
    _SERVICE_TO_STATE,
    BLOCKED_WS_WRITE_COMMANDS,
    is_single_entity_target,
)
from .ws_waiters import wait_for_state_change

# The ha_mcp_tools/call_service WS command: the first WRITE capability (Phase 3,
# issue #1813). When the component advertises ``call_service`` the consumer routes a
# single service call through this one in-process frame, which fires exactly one
# ``async_call`` and returns the REAL pre->post transition, replacing the legacy
# REST POST + hardcoded ``_SERVICE_TO_STATE`` guess + WS-subscribe verification.
# Named once so the routing helper and its tests agree on the wire string.
WS_CALL_SERVICE = "ha_mcp_tools/call_service"


_parse_bulk_operations = legacy_errors_sync(_shared_parse_bulk_operations)
_reject_group_member_conflicts = legacy_errors_sync(reject_group_member_conflicts)


async def _reject_operations_group_member_conflicts(
    client: HomeAssistantClient, operations: list[Any]
) -> None:
    """Fail closed when an operations-mode batch targets a group/aggregate
    entity together with one or more of its own members.

    Confirmed live (2026-08-23): Home Assistant (Hue Room/Zone groups in
    particular) fans a service call on the GROUP entity out to every member
    regardless of what else is in the same batch -- an operations batch
    that turned off a Hue Room group plus 4 of its 5 members (deliberately
    omitting the 5th, to exclude it) still turned the 5th member off too,
    purely from the group row's own cascade. Listing a member row
    separately does not shield it, so this fails the WHOLE batch closed
    (nothing dispatched) rather than silently letting the group row's
    cascade override an exclusion operations mode has no way to express.

    Fails closed on its own states-fetch failure too: an unverifiable batch
    is not a verified-safe one, and this is the same infrastructure-failure
    stance ``bulk_selector.py`` takes for the analogous selector-mode read.
    ``client.get_states()`` itself now raises ``HomeAssistantConnectionError``
    rather than swallowing a malformed response to ``[]`` (see its
    docstring in ``rest_client.py``) -- that used to be the actual fail-OPEN
    hole here: an HA hiccup silently produced an empty states list, which
    read as "verified, no conflicts" instead of "could not verify".

    A single-entity (or empty/all-malformed) batch cannot contain a
    group/member conflict by construction, so the states fetch is skipped
    entirely for it. Every batch with 2+ distinct entities still pays one
    uncached, full ``GET /states`` here regardless of whether any of them
    turn out to be a group -- this trades that fixed per-call cost for
    simplicity over fetching only the batch's own (and transitively
    reachable) entities, which would need N concurrent per-entity reads
    instead of one bulk one and is bounded by batch size rather than
    instance size. Revisit if that cost becomes a real bottleneck.
    """
    entity_ids = {
        op["entity_id"]
        for op in operations
        if isinstance(op, dict) and isinstance(op.get("entity_id"), str)
    }
    if len(entity_ids) < 2:
        return
    try:
        states = await client.get_states()
    except ToolError:
        raise
    except Exception as exc:
        exception_to_structured_error(
            exc,
            context={"operation": "bulk operations group-safety check"},
            suggestions=[
                "Could not verify this operations batch against group/"
                + "aggregate membership, so nothing was dispatched.",
                "Retry the request; this may be a transient Home Assistant "
                + "connectivity issue.",
            ],
        )
        raise  # unreachable: exception_to_structured_error always raises
    _reject_group_member_conflicts(entity_ids, states)


class _AmbiguousDispatch:
    """Sentinel type for a post-send-ambiguous component write (see below)."""


# Returned by ``_call_service_via_component`` when the component frame was SENT but
# its response/confirmation never arrived (a response-wait timeout or a post-send
# transport drop): the write MAY have landed, so the caller reports it as ``partial``
# and MUST NOT re-POST via the legacy path (D9 at-most-once — an ambiguous post-send
# outcome is never retried). Distinct from ``None`` (the component provably never
# dispatched → a safe legacy first fire).
_COMPONENT_DISPATCH_AMBIGUOUS = _AmbiguousDispatch()


def _parse_json_dict_param(data: Any, *, type_error_message: str) -> Any:
    try:
        return _shared_parse_json_dict_param(
            data, type_error_message=type_error_message
        )
    except ToolFailure as exc:
        raise_tool_error(exc.data)


logger = logging.getLogger(__name__)

# Services that produce observable state changes on entities

# Domains where a service call does not move the TARGET entity's own primary
# state to a value the verifier can wait for. ``scene`` belongs here with
# ``automation``/``script``: activating a scene changes the member entities,
# but the scene entity's own state is a last-activated timestamp that never
# becomes "on"/"off" — so waiting for ``turn_on`` -> "on" always times out and
# only appends a spurious "could not be verified" warning (~10s wasted).

#: Emitted when a ``return_response=true`` reply is not HA's
#: ``{"changed_states": [...], "service_response": ...}`` envelope. Without it an
#: empty ``result`` reads as an affirmative "no entity states changed" rather than
#: "the records could not be separated out". Module-level so the tests that
#: pin the behaviour assert against this exact string instead of a prose fragment
#: that goes tautologically green when the wording is edited.

#: Emitted when the component served a ``return_response`` call but returned no
#: ``service_response`` key. The component only sets that key for a non-null
#: response, so an absent key is ambiguous server-side: the service may genuinely
#: have returned nothing, or the component may have discarded the response on its
#: dispatched-but-unconfirmed path. Only raised alongside ``partial`` — the case
#: where the discard is actually possible — so a plain null response stays quiet.

# ``_SERVICE_TO_STATE`` (the service -> expected primary-state map) is the single
# source of truth in ``util_helpers`` — imported above and shared with the bulk
# consumer (``device_control``) so both write paths hand the component the same
# confirmation hint.


# WebSocket commands that stream events or reply in two phases (an initial ack
# then the real payload as a follow-up event) rather than resolving to a single
# terminal result. ha_call_service's ws_command escape hatch only supports
# one-shot request/response commands, so these are rejected: the "subscribe"
# substring catches subscription commands, and the set names known two-phase /
# streaming commands whose names don't contain "subscribe". The set is a floor,
# not an exhaustive list -- HA's WS command naming isn't consistent enough for a
# name check alone to be fully reliable.
_WS_COMMAND_EVENT_BLOCKLIST = frozenset(
    {
        "render_template",  # event-based; use ha_eval_template instead
        "system_health/info",  # two-phase (see tools_system._fetch_health_info)
        "assist_pipeline/run",  # streams pipeline events
    }
)

# Substrings that mark a WS command as streaming / subscription-based even when
# its name isn't in the blocklist above. Such commands ack once and then push
# follow-up events on the same id; the one-shot send_command path returns the
# ack and leaks the subscription. "subscribe" covers subscribe_* and */subscribe;
# "stream" covers history/stream, logbook/event_stream, camera/stream, ...;
# "start_preview" covers template/start_preview and the config-flow preview family.
_WS_STREAMING_SUBSTRINGS = ("subscribe", "stream", "start_preview")

# One-shot WS commands that re-enter Home Assistant's service invocation. Routing
# them through the escape hatch would bypass the service-mode guards (notably the
# reserved ha_mcp_tools domain block), so they are rejected -- use the
# domain/service parameters for service calls instead.
_WS_COMMAND_SERVICE_INVOKERS = frozenset({"call_service", "execute_script"})

# Reserved WebSocket envelope keys the transport owns. Allowing them inside data
# would let a caller override the validated command type (defeating every check
# below) or collide with the transport's message id, so they are rejected.
_WS_RESERVED_ENVELOPE_KEYS = frozenset({"type", "id"})


def _is_streaming_ws_command(command_type: str) -> bool:
    """Return True for subscription / streaming / two-phase WS commands."""
    lowered = command_type.lower()
    if lowered in _WS_COMMAND_EVENT_BLOCKLIST:
        return True
    return any(sub in lowered for sub in _WS_STREAMING_SUBSTRINGS)


def _build_service_suggestions(
    domain: str, service: str, entity_id: str | None
) -> list[str]:
    """Build common error suggestions for service call failures."""
    return [
        f"Verify {entity_id} exists using ha_get_state()"
        if entity_id
        else "Specify an entity_id for targeted service calls",
        f"Check available services for {domain} domain using ha_get_skill_guide",
        "Use ha_search() to find correct entity IDs",
    ]


class ServiceTools(SharedServiceCallTools):
    """Service call and device operation tools for Home Assistant."""

    def __init__(self, client: HomeAssistantClient, device_tools: Any) -> None:
        self._client = client
        self._device_tools = device_tools

    _parse_service_data = staticmethod(
        legacy_errors_sync(SharedServiceCallTools._parse_service_data)
    )
    _validate_service_call_params = staticmethod(
        legacy_errors_sync(SharedServiceCallTools._validate_service_call_params)
    )
    _reject_incompatible_ws_params = staticmethod(
        legacy_errors_sync(SharedServiceCallTools._reject_incompatible_ws_params)
    )
    _parse_result_projection_params = staticmethod(
        legacy_errors_sync(SharedServiceCallTools._parse_result_projection_params)
    )
    _raise_for_missing_or_unavailable_target = staticmethod(
        legacy_errors_sync(
            SharedServiceCallTools._raise_for_missing_or_unavailable_target
        )
    )
    _build_component_call_response = legacy_errors_sync(
        SharedServiceCallTools._build_component_call_response
    )

    async def _validate_entity_before_wait(self, entity_id: str | None) -> str | None:
        """Fetch entity_id's pre-call state, failing fast on a settled bad target.

        ``entity_id`` stays optional in the signature to match the caller's
        own ``str | None`` (a service call may target zero entities); the
        one current call site only reaches this when ``should_wait`` (which
        embeds an ``entity_id is not None`` check among several AND-ed
        conditions) is true, so ``entity_id`` is always real there in
        practice -- but that invariant lives in a boolean a few lines away,
        not in a form the type checker can see through. Narrowing here
        instead of trusting the caller keeps ``get_entity_state`` (which
        genuinely requires a ``str``) honestly typed.

        A clean 404 (the entity does not exist) settles the question with
        certainty before any service dispatch or 10s state-change wait, so
        raise immediately instead of silently degrading into a wait that can
        never confirm. Any other fetch failure is NOT conclusive -- it
        returns ``None`` and lets the call proceed as before, since the
        entity may well exist and this could be a transient blip.

        Deliberately NOT fast-failed here: a fetched ``"unavailable"``
        state. Unlike a 404, it does not prove the target can never respond
        -- dispatching the service is exactly what can reconnect it (e.g. a
        device that wakes on the very command being sent), and failing here
        would mean that command never reaches Home Assistant at all. The
        entity's initial state is still returned so ``_verify_state_change``
        can classify a genuine confirmation lapse as ENTITY_UNAVAILABLE only
        if a live re-check shows it is STILL unavailable afterward -- mirrors
        the component path's post-dispatch-state check.

        A comma-separated ``entity_id`` ("light.a,light.b") or a Home
        Assistant magic broadcast target (``entity_id="all"`` /
        ``ENTITY_MATCH_ALL``, or ``"none"``) is a valid target for the
        service-call payload, but the single-entity ``/api/states/<id>``
        endpoint has no way to resolve either -- a 404 there proves nothing
        about whether the underlying call can succeed, so the fast-fail is
        skipped entirely for one (see ``is_single_entity_target``; matches
        the component route's own comma exclusion in
        ``_maybe_component_call_service``).
        """
        if entity_id is None:
            return None
        is_single_target = is_single_entity_target(entity_id)
        try:
            state_data = await self._client.get_entity_state(entity_id)
        except HomeAssistantAPIError as e:
            if is_single_target and e.status_code == 404:
                raise_tool_error(create_entity_not_found_error(entity_id))
            logger.debug(
                f"Could not fetch initial state for {entity_id}: {e} — state verification may be degraded"
            )
            return None
        except Exception as e:  # noqa: BLE001
            logger.debug(
                f"Could not fetch initial state for {entity_id}: {e} — state verification may be degraded"
            )
            return None

        return state_data.get("state") if state_data else None

    async def _verify_state_change(
        self,
        entity_id: str,
        service: str,
        initial_state: str | None,
        response: dict[str, Any],
    ) -> None:
        """Wait for and verify entity state change after a service call, updating response in place.

        An entity that was ``"unavailable"`` at dispatch time was NOT failed
        fast (see ``_validate_entity_before_wait``) -- it may have reconnected
        once the command actually reached it. Only once the wait genuinely
        lapses does a fresh live re-check decide whether it's still
        unavailable (report the precise ``ENTITY_UNAVAILABLE``) or reconnected
        to some state other than the hint (leave the existing generic
        partial/timeout wording) -- mirrors the component path's
        post-dispatch-state check.
        """
        try:
            expected = _SERVICE_TO_STATE.get(service)
            new_state = await wait_for_state_change(
                self._client,
                entity_id,
                expected_state=expected,
                initial_state=initial_state,
                timeout=10.0,
            )
            if new_state:
                response["verified_state"] = new_state.get("state")
                return
            if initial_state == "unavailable" and await self._still_unavailable(
                entity_id
            ):
                raise_tool_error(
                    create_entity_unavailable_after_dispatch_error(entity_id)
                )
            response.setdefault("warnings", []).append(
                "Service executed but state change could not be verified within timeout."
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            response.setdefault("warnings", []).append(
                f"Service executed but state verification failed: {e}"
            )

    async def _still_unavailable(self, entity_id: str) -> bool:
        """Live re-check: True only when a successful fetch explicitly shows
        ``"unavailable"``.

        A 404 here means the entity was REMOVED since the initial fetch — a
        more specific outcome than "still unavailable", so it raises
        ENTITY_NOT_FOUND directly rather than being folded into it. Any other
        fetch failure (network blip, an expired token surfacing as
        ``HomeAssistantAuthError`` -- a sibling of ``HomeAssistantAPIError``,
        not a subclass, so it lands in the generic branch below) or a
        response with no usable ``state`` is inconclusive, not proof either
        way, so it returns ``False`` and lets the caller fall through to the
        generic verification-failed wording instead of confidently asserting
        an unproven "still unavailable". Logged at WARNING (matching the
        sibling re-check in ``_validate_entity_before_wait`` and
        ``wait_for_state_change`` in ws_waiters.py) so an operational
        failure here -- an expired token, in particular -- leaves a
        server-side trace instead of silently reading as a generic timeout.
        """
        try:
            current = await self._client.get_entity_state(entity_id)
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                raise_tool_error(create_entity_not_found_error(entity_id))
            logger.warning(
                f"Post-timeout unavailable re-check for {entity_id} failed: {e} — treating as inconclusive"
            )
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Post-timeout unavailable re-check for {entity_id} failed: {e} — treating as inconclusive"
            )
            return False
        return isinstance(current, dict) and current.get("state") == "unavailable"

    def _handle_connection_error(
        self,
        error: HomeAssistantConnectionError,
        *,
        domain: str,
        service: str,
        entity_id: str | None,
        data: str | dict[str, Any] | None,
        return_response: bool = False,
    ) -> dict[str, Any]:
        """Handle a HomeAssistantConnectionError raised while calling a service.

        Timeouts are treated as partial success (the service was dispatched but
        Home Assistant did not respond in time) and return a partial-success
        response. Non-timeout connection errors raise a structured ToolError.
        """
        # Check if this is a timeout - for service calls, timeouts typically
        # mean the service was dispatched but HA didn't respond in time.
        # The operation is likely still running (e.g., update.install, long automations).
        if isinstance(error.__cause__, httpx.TimeoutException):
            return self._build_timeout_response(
                domain, service, entity_id, data, return_response=return_response
            )
        # Non-timeout connection errors are real failures
        exception_to_structured_error(
            error,
            context={
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
            },
            suggestions=_build_service_suggestions(domain, service, entity_id),
        )
        return None  # unreachable: exception_to_structured_error always raises

    @staticmethod
    def _raise_unexpected_call_service_error(
        error: Exception,
        *,
        domain: str,
        service: str,
        entity_id: str | None,
    ) -> NoReturn:
        """Raise a structured ToolError for an unexpected ha_call_service failure."""
        suggestions = _build_service_suggestions(domain, service, entity_id)
        if entity_id:
            suggestions.append(
                f"For universal control: ha_call_service('homeassistant', 'toggle', entity_id='{entity_id}')"
            )
        exception_to_structured_error(
            error,
            context={
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
            },
            suggestions=suggestions,
        )

    async def _send_ws_command_mapped(
        self, command_type: str, command_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Send an arbitrary WS command, mapping a dead transport to ToolError.

        The ws_command branch returns before ``ha_call_service``'s own try
        block, so without this a transport failure raised by
        ``send_websocket_message`` (#1947) would escape the tool unstructured.
        """
        try:
            result: dict[str, Any] = await self._client.send_websocket_message(
                {"type": command_type, **command_params}
            )
            return result
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"ws_command": command_type},
                suggestions=[
                    "Check the Home Assistant connection",
                    "Retry once the WebSocket link is back",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises

    async def _call_ws_command(
        self,
        ws_command: str,
        data: str | dict[str, Any] | None,
        *,
        domain: str | None,
        service: str | None,
    ) -> dict[str, Any]:
        """Send a one-shot WebSocket command via ha_call_service's escape hatch.

        Reaches Home Assistant WebSocket commands that are not registered
        services. Only one-shot request/response commands are supported —
        streaming / subscription commands are rejected up front.
        """
        command_type = ws_command.strip()
        if domain is not None or service is not None:
            raise_tool_error(
                create_validation_error(
                    "Provide either domain + service (a registered service call) "
                    "OR ws_command (a raw WebSocket command), not both.",
                    parameter="ws_command",
                )
            )
        if not command_type:
            raise_tool_error(
                create_validation_error(
                    "ws_command must be a non-empty WebSocket command type "
                    "(the message's 'type' field).",
                    parameter="ws_command",
                )
            )
        if _is_streaming_ws_command(command_type):
            raise_tool_error(
                create_validation_error(
                    f"ws_command '{command_type}' is a streaming or two-phase "
                    "command; ha_call_service only sends one-shot "
                    "request/response commands. For template rendering use "
                    "ha_eval_template.",
                    parameter="ws_command",
                )
            )
        if command_type.lower() in _WS_COMMAND_SERVICE_INVOKERS:
            raise_tool_error(
                create_validation_error(
                    f"ws_command '{command_type}' invokes Home Assistant services "
                    "and would bypass ha_call_service's safeguards. Use the "
                    "domain/service parameters for service calls instead.",
                    parameter="ws_command",
                )
            )
        if command_type.lower().startswith("ha_mcp_tools/"):
            raise_tool_error(
                create_validation_error(
                    "ha_call_service cannot invoke 'ha_mcp_tools/*' WebSocket "
                    "commands. Use the dedicated ha-mcp tools instead.",
                    parameter="ws_command",
                )
            )
        if command_type.lower() in BLOCKED_WS_WRITE_COMMANDS:
            raise_tool_error(
                create_validation_error(
                    f"ws_command '{command_type}' mutates persistent state that a "
                    "dedicated tool guards with backups and conflict checks. Use "
                    "the corresponding ha-mcp tool (e.g. ha_config_set_dashboard, "
                    "ha_set_area_or_floor, ha_remove_entity) instead.",
                    parameter="ws_command",
                )
            )
        command_params = (
            _parse_json_dict_param(
                data, type_error_message="ws_command data must be a JSON object"
            )
            or {}
        )
        reserved = _WS_RESERVED_ENVELOPE_KEYS & command_params.keys()
        if reserved:
            raise_tool_error(
                create_validation_error(
                    "data must not contain the reserved WebSocket envelope key(s) "
                    f"{', '.join(sorted(reserved))}; the command type is set by "
                    "ws_command, and the message id is managed by the transport.",
                    parameter="data",
                )
            )
        # send_websocket_message returns a {"success": ...} dict for anything
        # HA answered with, which the result-shape check below turns into a
        # structured error. A dead transport raises instead (#1947), and this
        # branch runs BEFORE ha_call_service's own try block, so the mapping
        # has to happen here or the exception escapes the tool unstructured.
        guard_snapshot_route(ws_command=command_type, ws_params=command_params)
        result = await self._send_ws_command_mapped(command_type, command_params)

        if not isinstance(result, dict) or not result.get("success", False):
            error_msg = (
                result.get("error") if isinstance(result, dict) else None
            ) or "WebSocket command failed"
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(error_msg),
                    context={"ws_command": command_type},
                    suggestions=[
                        "Verify the command type and the parameters it requires",
                        "Confirm the target still exists",
                    ],
                )
            )

        return {
            "success": True,
            "ws_command": command_type,
            "parameters": command_params or None,
            "result": result.get("result"),
            "message": f"Successfully executed WebSocket command '{command_type}'",
        }

    async def _call_service_via_component(
        self,
        *,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        entity_ids: list[str],
        wait: bool,
        timeout: float,
        return_response: bool,
    ) -> dict[str, Any] | _AmbiguousDispatch | None:
        """Send once through the component and distinguish not-sent from unknown.

        None permits the first legacy dispatch only after a capability miss,
        connection establishment failure, explicit CommandNotSent, or the
        specific unknown_command response. A generic command error is ambiguous:
        a Core handler can mutate before raising. Timeouts, socket failures and
        malformed success frames are likewise never replayed.

        The native backend calls the same prep/do owner directly and has no
        transport fallback. Both paths retain the resolved-domain guard and
        expected-state confirmation hint.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "call_service"):
            return None
        # PRE-SEND: an establishment failure means the frame provably never reached
        # the component. Split into its own try so a POST-SEND failure below is never
        # misclassified as pre-send → a safe legacy first fire.
        try:
            ws = await get_websocket_client(
                url=self._client.base_url,
                token=self._client.token,
                verify_ssl=getattr(self._client, "verify_ssl", None),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s establishment failed; falling back to legacy: %r",
                WS_CALL_SERVICE,
                exc,
            )
            return None
        # ``send_command`` transmits the frame INSIDE itself, AFTER its readiness guard
        # and the actual socket write — so exception TYPE marks the send boundary:
        # ``HomeAssistantCommandNotSent`` is raised ONLY at the readiness guard (the one
        # provably-never-sent site → safe legacy first fire); only unknown_command proves that no owner ran; a response-wait TIMEOUT, a send() that raised (bytes may
        # already be on the socket), or a post-send transport drop (a mid-await socket
        # close raises plain ``HomeAssistantConnectionError``) is POST-SEND/AMBIGUOUS →
        # partial, never retried.
        # The confirmation HINT: the expected primary state after ``service`` (or
        # ``None`` for a service with no known primary state). The component confirms
        # only on REACHING this state — skipping a multi-phase service's intermediate
        # states / attribute-only noise — and immediate-matches an idempotent no-op; a
        # ``None`` hint keeps its any-first-event confirmation. It governs confirmation
        # TIMING only; the component still returns the REAL observed transition.
        expected_state = _SERVICE_TO_STATE.get(service)
        try:
            raw = await ws.send_command(
                WS_CALL_SERVICE,
                domain=domain,
                service=service,
                service_data=service_data,
                entity_ids=entity_ids,
                wait=wait,
                timeout=timeout,
                return_response=return_response,
                expected_state=expected_state,
            )
        except HomeAssistantCommandNotSent as exc:
            # PRE-SEND: the frame provably never left the process (the send_command
            # readiness guard — the one never-sent site). The write never happened, so
            # legacy REST is a safe first fire.
            logger.warning(
                "%s not sent; falling back to legacy: %r",
                WS_CALL_SERVICE,
                exc,
            )
            return None
        except HomeAssistantCommandError as exc:
            # Only unknown_command proves that this owner never ran. Core can
            # mutate and then raise any other error: do not replay that write.
            if is_unknown_command(exc):
                invalidate_caps(self._client)
                return None
            logger.warning(
                "%s command error; reporting unknown outcome without retry: %r",
                WS_CALL_SERVICE,
                exc,
            )
            return _COMPONENT_DISPATCH_AMBIGUOUS
        except Exception as exc:  # noqa: BLE001
            # HomeAssistantCommandTimeout (response-wait expired — the frame WAS sent)
            # or any post-send transport drop (e.g. a pooled-WS drop after send). The
            # component may still be lawfully mid-write, so this is ambiguous-
            # dispatched: report partial, NEVER re-POST (D9 at-most-once).
            logger.warning(
                "%s post-send timeout/drop; reporting partial (not retried): %r",
                WS_CALL_SERVICE,
                exc,
            )
            return _COMPONENT_DISPATCH_AMBIGUOUS
        result = raw.get("result")
        # A SUCCESS result frame is produced ONLY after the prep ran to completion (the
        # single async_call fired), so a malformed/unusable success envelope means the
        # write already HAPPENED. Report it AMBIGUOUS (partial, never re-POSTed) — a
        # ``None`` here would route to the legacy REST path and DOUBLE-APPLY. The
        # happy-path envelope is a dict whose ``dispatched`` is True: presence of the
        # key is not enough, since a received post-dispatch envelope whose
        # ``dispatched`` isn't True is one we cannot trust to re-fire.
        if not isinstance(result, dict) or result.get("dispatched") is not True:
            return _COMPONENT_DISPATCH_AMBIGUOUS
        return result

    async def _maybe_component_call_service(
        self,
        *,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        entity_id: str | None,
        data: str | dict[str, Any] | None,
        should_wait: bool,
        return_response: bool,
        verbose: bool,
        fields: list[str] | None,
        attribute_keys: list[str] | None,
    ) -> dict[str, Any] | None:
        """Route a confirmable single call through the component; ``None`` → do legacy.

        The component route is taken ONLY when confirming a single entity
        (``should_wait``). The capability's entire value is the real confirmed
        pre->post transition; for a non-confirmed call (``wait=False``, no / multi
        entity, or a non-state-changing domain) the component returns
        ``transitions=[]`` → ``result:[]``, silently dropping the changed-states body
        the legacy REST POST returns (e.g. a scene's member states). For those the
        legacy single POST costs the same one round-trip and is strictly richer, so
        this returns ``None`` and the caller stays on legacy.

        Returns a FINAL ``ha_call_service`` response when the component served the
        call: the mapped transition, or — on a post-send timeout / transport drop
        (the ambiguous sentinel) — the same dispatched-but-unconfirmed ``partial`` the
        legacy timeout path builds, NEVER re-POSTed (D9 at-most-once). Returns ``None``
        when the component was not used or provably never dispatched, so the caller
        runs the legacy REST path as a safe first fire.

        ``verbose`` routes to legacy too: it promises the FULL propagation chain
        (every downstream changed state), which the component path cannot deliver — it
        returns only the confirmation targets' ``new_state``s — so the richer legacy
        POST serves it instead.
        """
        # A comma-separated entity_id ("light.a,light.b") is a valid multi-target the
        # compaction path expands, but the component confirms one LITERAL entity_id: it
        # would wait for the nonexistent literal "light.a,light.b" and report a false
        # ``partial`` with an empty result even though HA changed both real entities.
        # Treat a comma as the multi-target signal → legacy REST POST (parity with the
        # verbose / non-confirmed early-outs above, which the component cannot serve).
        if not should_wait or verbose or (entity_id and "," in entity_id):
            return None
        component_result = await self._call_service_via_component(
            domain=domain,
            service=service,
            service_data=service_data,
            entity_ids=[entity_id] if entity_id else [],
            wait=True,
            timeout=10.0,
            return_response=return_response,
        )
        if isinstance(component_result, _AmbiguousDispatch):
            return self._build_timeout_response(
                domain, service, entity_id, data, return_response=return_response
            )
        if component_result is None:
            return None
        response: dict[str, Any] = self._build_component_call_response(
            component_result,
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
        return response

    ha_call_service = tool(
        name="ha_call_service",
        tags={"Service & Device Control"},
        annotations=write_hints(
            "Call Service", destructive=True, idempotent=False, open_world=False
        ),
    )(log_tool_usage(legacy_errors(SharedServiceCallTools.ha_call_service)))

    def _require_write_access(self) -> None:
        require_write_access("ha_call_service")

    async def _check_service_policy(self, domain: str, service: str) -> None:
        guard_snapshot_route(domain=domain, service=service)

    async def _call_raw_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        *,
        return_response: bool,
    ) -> Any:
        return await self._client.call_service(
            domain, service, service_data, return_response=return_response
        )

    def _map_service_error(self, error: Exception, **kwargs: Any) -> Any:
        if isinstance(error, HomeAssistantConnectionError):
            return self._handle_connection_error(error, **kwargs)
        if isinstance(error, (ToolError, ToolFailure)):
            raise error
        self._raise_unexpected_call_service_error(
            error,
            domain=kwargs["domain"],
            service=kwargs["service"],
            entity_id=kwargs["entity_id"],
        )

    ha_get_operation_status = tool(
        name="ha_get_operation_status",
        tags={"Service & Device Control"},
        annotations=read_only_hints("Get Operation Status", open_world=False),
    )(log_tool_usage(legacy_errors(_BulkContractTools.ha_get_operation_status)))

    async def _get_operation_status(
        self, operation_id: str | list[str], timeout_seconds: float = 8
    ) -> dict[str, Any]:
        try:
            if isinstance(operation_id, list):
                result = await self._device_tools.get_bulk_operation_status(
                    operation_ids=operation_id, timeout_seconds=timeout_seconds
                )
                return cast(dict[str, Any], result)
            result = await self._device_tools.get_device_operation_status(
                operation_id=operation_id, timeout_seconds=timeout_seconds
            )
            return cast(dict[str, Any], result)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 - legacy transport failures become structured tool errors
            op_context: dict[str, Any] = {"operation_id": operation_id}
            exception_to_structured_error(
                e,
                context=op_context,
                suggestions=[
                    "Verify the operation ID(s) are valid",
                    "Use ha_get_state() to check current entity states instead",
                ],
            )
            return None

    ha_bulk_control = tool(
        name="ha_bulk_control",
        tags={"Service & Device Control"},
        annotations=write_hints(
            "Bulk Control", destructive=True, idempotent=False, open_world=False
        ),
    )(log_tool_usage(legacy_errors(_BulkContractTools.ha_bulk_control)))

    async def _bulk_control(
        self,
        operations: list[SkipValidation[BulkControlOperation]] = cast(Any, None),
        parallel: bool = True,
        ctx: Context | None = None,
        selector: SkipValidation[BulkControlSelector] | None = None,
        action: str | None = None,
        parameters: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        validate_first: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if operations is None and selector is None:
            raise_tool_error(
                create_validation_error(
                    "Provide exactly one of operations or selector; neither was given",
                    parameter="operations",
                )
            )
        if operations is not None and selector is not None:
            raise_tool_error(
                create_validation_error(
                    "Provide exactly one of operations or selector; both were given",
                    parameter="selector",
                )
            )
        if selector is not None:
            return await self._run_bulk_selector(
                cast(dict[str, Any], selector),
                action=action,
                parameters=parameters,
                timeout_seconds=timeout_seconds,
                validate_first=validate_first,
                dry_run=dry_run,
                parallel=parallel,
                ctx=ctx,
            )
        offending_parameter = _selector_only_parameter_offender(
            action=action,
            parameters=parameters,
            timeout_seconds=timeout_seconds,
            validate_first=validate_first,
            dry_run=dry_run,
        )
        if offending_parameter is not None:
            raise_tool_error(
                create_validation_error(
                    _selector_only_parameter_message(offending_parameter),
                    parameter=offending_parameter,
                )
            )
        operations_list = _parse_bulk_operations(operations)
        await _reject_operations_group_member_conflicts(self._client, operations_list)
        result = await self._device_tools.bulk_device_control(
            operations=operations_list, parallel=parallel, ctx=ctx
        )
        return cast(dict[str, Any], result)

    async def _run_bulk_selector(
        self,
        selector: dict[str, Any],
        *,
        action: str | None,
        parameters: dict[str, Any] | None,
        timeout_seconds: float | None,
        validate_first: bool,
        dry_run: bool,
        parallel: bool,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Resolve one structural selector and, unless ``dry_run``, dispatch it.

        Split out of ``ha_bulk_control`` to keep that tool's own McCabe
        complexity under the repo's C901 threshold
        (docs/agents/development.md — no per-file exemptions).
        """
        if action is None:
            raise_tool_error(
                create_validation_error(
                    "Selector mode requires action", parameter="action"
                )
            )
        try:
            parsed_selector = parse_json_param(selector, "selector")
            if not isinstance(parsed_selector, dict):
                raise BulkSelectorValidationError("selector must be a JSON object")
        except ValueError as exc:
            # BulkSelectorValidationError subclasses ValueError; catching it
            # separately alongside ValueError was redundant.
            parameter = getattr(exc, "parameter", "selector")
            raise_tool_error(create_validation_error(str(exc), parameter=parameter))
        try:
            resolution = await resolve_bulk_selector(
                self._client,
                parsed_selector,
                action=action,
                parameters=parameters,
                timeout_seconds=timeout_seconds,
                validate_first=validate_first,
            )
        except BulkSelectorValidationError as exc:
            # Caller-fixable: the selector itself is wrong. Unlike the
            # `except ValueError` above (which also catches plain ValueErrors
            # with no `.parameter`), every BulkSelectorValidationError sets
            # one (default "selector") -- no getattr fallback needed.
            raise_tool_error(create_validation_error(str(exc), parameter=exc.parameter))
        except BulkSelectorInfrastructureError as exc:
            # NOT caller-fixable by editing the selector. Routed through
            # create_connection_error (not create_validation_error) so an
            # agent doesn't try to rewrite a selector that was never the
            # problem -- but the specific next step depends on exc.cause,
            # since not every cause is actually a connectivity problem.
            logger.warning(
                "ha_bulk_control: selector resolution infrastructure failure (%s): %s",
                exc.cause,
                exc,
            )
            message, suggestions = _INFRASTRUCTURE_ERROR_SUGGESTIONS.get(
                exc.cause,
                _INFRASTRUCTURE_ERROR_SUGGESTIONS[
                    InfrastructureErrorCause.CONNECTIVITY
                ],
            )
            raise_tool_error(
                create_connection_error(
                    message, details=str(exc), suggestions=suggestions
                )
            )
        except Exception as exc:
            exception_to_structured_error(
                exc,
                context={"operation": "resolve bulk selector"},
            )
            raise  # unreachable: exception_to_structured_error always raises

        if dry_run:
            response: dict[str, Any] = {
                "success": True,
                "dry_run": True,
                "dispatched": False,
            }
            _attach_resolution_to_response(response, resolution)
            return response
        try:
            result = await self._device_tools.bulk_device_control(
                operations=resolution.operations,
                parallel=parallel,
                ctx=ctx,
            )
        except ToolError:
            # bulk_device_control already raises a fully structured ToolError
            # (e.g. "every operation failed validation" -- see its own
            # raise_tool_error call sites) -- re-raise it untouched. Without
            # this guard, the except Exception below would catch it too and
            # re-classify it via exception_to_structured_error, discarding
            # its real code/message/suggestions for a generic one.
            raise
        except Exception as exc:
            # Attach the frozen resolution to the error context: a
            # non-transactional, partially-executed bulk write must leave a
            # record of which entities were resolved and attempted, in the
            # response the agent sees, not just in whatever
            # bulk_device_control itself logged.
            logger.warning(
                "ha_bulk_control: dispatch failed after resolving %d entities: %s",
                len(resolution.resolved_entity_ids),
                exc,
            )
            exception_to_structured_error(
                exc,
                context={
                    "operation": "bulk device control",
                    "resolution": resolution.summary(),
                },
            )
            raise  # unreachable: exception_to_structured_error always raises
        response = cast(dict[str, Any], result)
        _attach_resolution_to_response(response, resolution)
        return response

    @property
    def events(self) -> RemoteEvents:
        return RemoteEvents(self._client)

    ha_call_event = tool(
        name="ha_call_event",
        tags={"Service & Device Control"},
        annotations=write_hints(
            "Call Event", destructive=True, idempotent=False, open_world=False
        ),
    )(log_tool_usage(legacy_errors(_EventContractTools.ha_call_event)))


def register_service_tools(
    mcp: Any, client: HomeAssistantClient, **kwargs: Any
) -> None:
    """Register service call and operation monitoring tools with the MCP server."""
    device_tools = kwargs.get("device_tools")
    if not device_tools:
        raise ValueError("device_tools is required for service tools registration")
    register_tool_methods(mcp, ServiceTools(client, device_tools))
