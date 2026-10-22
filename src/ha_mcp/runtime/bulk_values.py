"""Bulk row validation and result projection, independent of acquisition/dispatch."""

import json
import logging
import math
from typing import Any

from ha_mcp.errors import (
    ErrorCode,
    create_entity_unavailable_after_dispatch_error,
    create_error_response,
)
from ha_mcp.runtime.action_constants import is_single_entity_target

from .bulk_params import BulkControlOperation

logger = logging.getLogger(__name__)
_BULK_OPERATION_KEYS = frozenset(BulkControlOperation.__annotations__)
_BULK_ROW_SHAPE_SUGGESTION = (
    "Use entity_id and action (e.g. action='off', not service='turn_off'); "
    "send no domain or service keys and put action data in parameters"
)


class BulkOperationValues:
    @staticmethod
    def _normalize_bulk_parameters(
        value: Any,
    ) -> tuple[dict[str, Any] | None, ErrorCode | None, str]:
        """Normalize a present ``parameters`` value and identify invalid ones.

        Returns ``(parameters, error_code, detail)``. ``detail`` carries the
        decoder's own reason and offset for a JSON failure — without it the
        caller can only say "invalid JSON", which does not tell the sender
        where its string went wrong. It is empty for every other outcome.
        """
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                return (
                    None,
                    ErrorCode.VALIDATION_INVALID_JSON,
                    f"{exc.msg} at position {exc.pos}",
                )
            except RecursionError:
                # json.loads recurses per nesting level; a pathologically
                # nested string must skip the row, not abort the batch.
                return (
                    None,
                    ErrorCode.VALIDATION_INVALID_JSON,
                    "JSON nested too deeply",
                )
        if not isinstance(value, dict):
            # A present-but-null value is malformed, same as timeout_seconds
            # and validate_first; an absent key never reaches this helper.
            return None, ErrorCode.VALIDATION_INVALID_PARAMETER, ""
        return value, None, ""

    @staticmethod
    def _normalize_bulk_timeout(value: Any) -> float | None:
        """Normalize a non-negative finite timeout, or ``None`` when invalid.

        Bools are rejected before ``float()`` sees them: ``float(True)`` is 1.0,
        which would silently accept ``timeout_seconds: true`` as a one-second
        wait. Strings are rejected for the same reason — the advertised schema
        is ``strict``, so coercing ``"10"`` here would silently accept what the
        schema tells the model is invalid. ``None`` is invalid here too — the
        caller only calls this when the key is present, and a present-but-null
        timeout is a malformed row.
        """
        if isinstance(value, (bool, str)) or value is None:
            return None
        try:
            timeout = float(value)
        except (TypeError, ValueError, OverflowError):
            # OverflowError: an int too large for float must skip the row,
            # not abort the batch through the generic exception handler.
            return None
        return timeout if math.isfinite(timeout) and timeout >= 0 else None

    @staticmethod
    def _normalized_bulk_operation(
        operation: dict[str, Any],
        parameters: dict[str, Any] | None,
        timeout: float | None,
    ) -> dict[str, Any]:
        """Return one normalized valid bulk operation."""
        normalized = dict(operation)
        if parameters is not None:
            normalized["parameters"] = parameters
        if timeout is not None:
            normalized["timeout_seconds"] = timeout
        return normalized

    @staticmethod
    def _skip_bulk_operation(
        skipped_operations: list[dict[str, Any]],
        op: Any,
        code: ErrorCode,
        error: str,
        context: dict[str, Any],
        suggestions: list[str] | None = None,
    ) -> None:
        """Log one rejected bulk row and record its structured skip entry.

        ``create_error_response`` lifts every ``context`` key to the top level,
        so ``index`` needs no separate write; ``operation`` is not a context
        key and is attached here so callers can see the row they sent.
        """
        logger.warning(f"Bulk control: {error}")
        err_response = create_error_response(
            code, error, suggestions=suggestions, context=context
        )
        err_response["operation"] = op
        skipped_operations.append(err_response)

    @classmethod
    def _validate_bulk_operations(
        cls,
        operations: list[Any],
        skipped_operations: list[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any], str, str]]:
        valid: list[tuple[int, dict[str, Any], str, str]] = []
        for i, op in enumerate(operations):
            if not isinstance(op, dict):
                cls._skip_bulk_operation(
                    skipped_operations,
                    op,
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Operation at index {i} is not a dict: {type(op).__name__}",
                    {"index": i},
                )
                continue

            # str() before sorting: a direct Python caller can pass non-string
            # keys, and mixed key types make sorted()/join() raise mid-batch.
            obsolete_keys = sorted(str(key) for key in set(op) - _BULK_OPERATION_KEYS)
            if obsolete_keys:
                key_list = ", ".join(obsolete_keys)
                cls._skip_bulk_operation(
                    skipped_operations,
                    op,
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Operation at index {i} contains unsupported key(s): {key_list}",
                    {"index": i, "unsupported_keys": obsolete_keys},
                    suggestions=[_BULK_ROW_SHAPE_SUGGESTION],
                )
                continue

            entity_id = op.get("entity_id")
            action = op.get("action")
            missing = [f for f in ("entity_id", "action") if not op.get(f)]

            if missing:
                cls._skip_bulk_operation(
                    skipped_operations,
                    op,
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    f"Operation at index {i} missing required fields: "
                    f"{', '.join(missing)}",
                    {"index": i},
                )
                continue

            if not isinstance(entity_id, str) or not isinstance(action, str):
                cls._skip_bulk_operation(
                    skipped_operations,
                    op,
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Operation at index {i} requires string entity_id and "
                    "action fields",
                    {"index": i},
                )
                continue

            parameters, parameter_error, parameter_detail = (
                cls._normalize_bulk_parameters(op["parameters"])
                if "parameters" in op
                else (None, None, "")
            )
            if parameter_error is not None:
                parameter_message = (
                    f"Operation at index {i} has invalid JSON parameters: "
                    f"{parameter_detail}"
                    if parameter_error == ErrorCode.VALIDATION_INVALID_JSON
                    else f"Operation at index {i} parameters must be a JSON object"
                )
                cls._skip_bulk_operation(
                    skipped_operations,
                    op,
                    parameter_error,
                    parameter_message,
                    {"index": i},
                )
                continue

            timeout: float | None = None
            if "timeout_seconds" in op:
                timeout = cls._normalize_bulk_timeout(op["timeout_seconds"])
                if timeout is None:
                    cls._skip_bulk_operation(
                        skipped_operations,
                        op,
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Operation at index {i} timeout_seconds must be a "
                        "non-negative number",
                        {"index": i},
                    )
                    continue

            if "validate_first" in op and not isinstance(op["validate_first"], bool):
                cls._skip_bulk_operation(
                    skipped_operations,
                    op,
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Operation at index {i} validate_first must be a boolean",
                    {"index": i},
                )
                continue

            normalized_op = cls._normalized_bulk_operation(op, parameters, timeout)
            valid.append((i, normalized_op, entity_id, action))

        return valid

    def _map_component_op_result(
        self,
        op: dict[str, Any],
        entity_id: str,
        action: str,
        row: dict[str, Any],
        component_op: dict[str, Any],
    ) -> dict[str, Any]:
        """Map one component op result into the legacy per-op result shape.

        The component confirms inline, so the op returns already-verified with a
        synthesized terminal status instead of the legacy ``pending_verification`` +
        operation-id handle. A ``command_sent: True`` result is counted ``successful``
        by ``_build_bulk_response`` exactly as the legacy dispatch result is; an op
        whose ``async_call`` raised under the batch (``error`` set / not dispatched)
        maps to a structured ``SERVICE_CALL_FAILED`` error and is NOT re-dispatched
        (D9). When ``validate_first`` (the default) is set and ``entity_id`` is a
        single literal entity ID (see ``is_single_entity_target`` — a comma-joined
        multi-target or magic broadcast target like ``"all"`` is skipped entirely,
        since its transition row proves nothing about whether the possibly-
        successful dispatch landed), a captured pre-state that is null (the entity
        does not exist) maps to a structured ``ENTITY_NOT_FOUND`` failure — parity
        with the legacy per-op validation. A captured pre-state of ``"unavailable"``
        maps to a distinct ``ENTITY_UNAVAILABLE`` failure, but only once BOTH the op
        is unconfirmed AND the post-dispatch state still shows unavailable — unlike
        the not-found branch, an unavailable target can legitimately reconnect and
        confirm mid-dispatch, so this check is doubly gated rather than symmetric
        with it.
        """
        service_call = {
            "domain": row["domain"],
            "service": row["service"],
            "data": row["service_data"],
        }
        if component_op.get("error") is not None or not component_op.get("dispatched"):
            err = create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                str(component_op.get("error") or "Operation failed to dispatch"),
                context={"entity_id": entity_id, "action": action},
            )
            err["service_call"] = service_call
            return err
        transitions = component_op.get("transitions") or []
        # I3: honor validate_first (default). The component captured each target's
        # pre-state; a null old_state on a confirmable op means the entity does not
        # exist (HA no-ops the dispatch, then the wait stalls to partial). The legacy
        # path returns a structured ENTITY_NOT_FOUND per-op failure — match it from
        # the pre-state already in the transition (no extra hops), rather than
        # counting a phantom entity as a successful command. I2 (length-drift)
        # guards the BATCH-level op-count, not this: an individual op can still
        # legitimately carry an EMPTY transitions list (dispatched, but post-
        # dispatch formatting raised — see _dispatched_unconfirmed_bulk_result) at
        # the correct array length, and that proves nothing about existence. So
        # look for a transition row at all first; only a present row with a null
        # old_state means a genuinely absent entity.
        if (
            op.get("validate_first", True)
            and row.get("entity_ids")
            and is_single_entity_target(entity_id)
        ):
            transition = next(
                (
                    t
                    for t in transitions
                    if isinstance(t, dict) and t.get("entity_id") == entity_id
                ),
                None,
            )
            if transition is not None:
                old_state = transition.get("old_state")
                if old_state is None:
                    err = create_error_response(
                        ErrorCode.ENTITY_NOT_FOUND,
                        f"Entity not found: {entity_id}",
                        suggestions=[
                            "Use ha_search to find the correct entity",
                            "Check the entity is not disabled in Home Assistant",
                        ],
                        context={"entity_id": entity_id, "action": action},
                    )
                    err["service_call"] = service_call
                    return err
                if (
                    isinstance(old_state, dict)
                    and old_state.get("state") == "unavailable"
                ):
                    new_state = transition.get("new_state")
                    if new_state is None and not component_op.get("confirmed"):
                        # Removed during the dispatch -- a more specific outcome
                        # than "still unavailable", which would falsely assert it
                        # still exists.
                        err = create_error_response(
                            ErrorCode.ENTITY_NOT_FOUND,
                            f"Entity not found: {entity_id}",
                            suggestions=[
                                "Use ha_search to find the correct entity",
                                "Check the entity is not disabled in Home Assistant",
                            ],
                            context={"entity_id": entity_id, "action": action},
                        )
                        err["service_call"] = service_call
                        return err
                    if (
                        not component_op.get("confirmed")
                        and isinstance(new_state, dict)
                        and new_state.get("state") == "unavailable"
                    ):
                        # Gated on NOT confirmed AND still unavailable per the
                        # best-available post-dispatch read: unlike a nonexistent
                        # entity, an "unavailable" one legitimately stays in the
                        # component's confirmation wait and can reconnect and
                        # transition mid-dispatch (see _confirmable_entity_ids) —
                        # a confirmed op, or one whose new_state shows it
                        # reconnected to some OTHER state (just not the expected
                        # hint), must never be reported as this failure just
                        # because its PRE-state was unavailable.
                        err = create_entity_unavailable_after_dispatch_error(entity_id)
                        err["action"] = action
                        err["service_call"] = service_call
                        return err
        final_state = next(
            (
                t["new_state"].get("state")
                for t in transitions
                if isinstance(t, dict) and isinstance(t.get("new_state"), dict)
            ),
            None,
        )
        confirmed = bool(component_op.get("confirmed"))
        partial = bool(component_op.get("partial"))
        return {
            "entity_id": entity_id,
            "action": action,
            "parameters": op.get("parameters") or {},
            "command_sent": True,
            # M-status: an inline-confirmed op has no operation_id, so a
            # "pending_verification" status is misleading (nothing will poll it). Use
            # "dispatched_unconfirmed" — the same label the ambiguous batch path uses.
            "status": "completed" if confirmed else "dispatched_unconfirmed",
            "confirmed": confirmed,
            "partial": partial,
            "final_state": final_state,
            "transitions": transitions,
            "service_call": service_call,
            "verification_method": "component_state_change",
            "message": (
                f"{entity_id} {action} " + ("confirmed" if confirmed else "dispatched")
            ),
        }

    @staticmethod
    def _has_duplicate_entity_ids(
        valid_operations: list[tuple[int, dict[str, Any], str, str]],
    ) -> bool:
        """True when two+ ops in the batch target the same entity_id.

        The component keys ONE transition waiter per entity_id, so the first
        ``state_changed`` satisfies every op sharing that entity — both would report the
        same transition/final_state. The legacy sequential path confirms each op
        independently, so ``_bulk_via_component`` routes the WHOLE batch to legacy when
        this returns True.
        """
        entity_ids = [entity_id for _idx, _op, entity_id, _action in valid_operations]
        return len(entity_ids) != len(set(entity_ids))

    @staticmethod
    def _bulk_frame_timeout(
        valid_operations: list[tuple[int, dict[str, Any], str, str]],
    ) -> float:
        """The batch confirmation-wait bound, honoring per-op ``timeout_seconds``.

        Legacy runs each op with its own ``timeout_seconds`` (default 10); the single
        batch frame shares ONE bounded wait, so use the max over the valid ops (capped
        at 60s) rather than a hardcoded 10s (M-timeout). ``valid_operations`` is
        non-empty here (an empty batch returned to legacy before the send). An explicit
        ``timeout_seconds`` of 0 is honored (parity with legacy and the component
        schema); only an ABSENT key defaults to 10.
        """
        return min(
            60.0,
            max(
                float(v) if (v := op.get("timeout_seconds")) is not None else 10.0
                for _idx, op, _eid, _action in valid_operations
            ),
        )

    @staticmethod
    def _component_bulk_op_results(
        raw: dict[str, Any], expected_len: int
    ) -> list[Any] | None:
        """The per-op results list from a batch frame, or ``None`` if malformed.

        The component preserves op order and returns exactly one result per row it was
        sent. A non-dict result or an op-list whose length drifts from ``expected_len``
        is an unusable/incompatible SUCCESS envelope — the caller treats ``None`` as
        AMBIGUOUS (the writes already landed; never reconcile a mismatched batch).
        """
        result = raw.get("result")
        if not isinstance(result, dict):
            return None
        op_results = result.get("operations")
        if not isinstance(op_results, list) or len(op_results) != expected_len:
            return None
        return op_results

    def _build_bulk_response(
        self,
        operations: list[Any],
        results: list[dict[str, Any]],
        operation_ids: list[str],
        skipped_operations: list[dict[str, Any]],
        parallel: bool,
    ) -> dict[str, Any]:
        successful = len(
            [r for r in results if isinstance(r, dict) and r.get("command_sent")]
        )
        executed_failed = len(results) - successful
        # Total failed includes both execution failures and skipped operations
        total_failed = executed_failed + len(skipped_operations)

        response: dict[str, Any] = {
            "total_operations": len(operations),
            "successful_commands": successful,
            "failed_commands": total_failed,
            "skipped_operations": len(skipped_operations),
            "execution_mode": "parallel" if parallel else "sequential",
            "operation_ids": operation_ids,
            "results": results,
            "follow_up": (
                {
                    "message": (
                        f"Use ha_get_operation_status(operation_id=[...]) to check all "
                        f"{len(operation_ids)} operations"
                    ),
                    "operation_ids": operation_ids,
                }
                if operation_ids
                else None
            ),
        }

        # Include skipped operation details if any were skipped
        if skipped_operations:
            response["skipped_details"] = skipped_operations
            response["suggestions"] = [
                _BULK_ROW_SHAPE_SUGGESTION,
                "Some operations were skipped due to validation errors",
                "Each operation requires 'entity_id' and 'action' fields",
                "Check skipped_details for specific errors",
                "Example format: {'entity_id': 'light.living_room', 'action': 'on'}",
            ]

        return response
