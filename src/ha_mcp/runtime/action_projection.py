"""Shared call-service response, confirmation and partial-outcome projection."""

from typing import Any

from ha_mcp.errors import (
    create_entity_not_found_error,
    create_entity_unavailable_after_dispatch_error,
)

from .action_constants import (
    _COMPONENT_RESPONSE_UNCONFIRMED_WARNING,
    _NON_ENVELOPE_WARNING,
    is_single_entity_target,
)
from .errors import raise_failure
from .projection import compact_service_result, project_entity_record


class ActionProjection:
    @staticmethod
    def _build_timeout_response(
        domain: str,
        service: str,
        entity_id: str | None,
        data: str | dict[str, Any] | None,
        *,
        return_response: bool = False,
    ) -> dict[str, Any]:
        """Build a partial-success response for service call timeouts.

        When ``return_response`` was requested the key is emitted here too, so a
        caller never has to branch on whether the key exists: every successful
        reply carries it. It is necessarily null — the reply never arrived — and
        that null proves nothing about what the service returned, so it comes
        with the same ambiguity warning the component's unconfirmed path uses.
        """
        response: dict[str, Any] = {
            "success": True,
            "partial": True,
            "domain": domain,
            "service": service,
            "entity_id": entity_id,
            "parameters": data,
            "message": (
                f"Service {domain}.{service} was dispatched but Home Assistant "
                f"did not respond within the timeout period. The operation is likely "
                f"still running in the background."
            ),
            "warnings": [
                "Response timed out. This is normal for long-running services "
                f"like updates or firmware installs. Use ha_get_state('{entity_id}') "
                "to check the current status."
                if entity_id
                else "Response timed out. This is normal for long-running services. "
                "The service was dispatched and may still be executing."
            ],
        }
        if return_response:
            response["service_response"] = None
            response["warnings"].append(_COMPONENT_RESPONSE_UNCONFIRMED_WARNING)
        return response

    @staticmethod
    def _split_return_response_envelope(
        result: Any, *, return_response: bool
    ) -> tuple[Any, Any, bool, list[str]]:
        """Split HA's reply into (changed states, response, present, warnings).

        HA answers ``return_response=true`` with an envelope
        ``{"changed_states": [...], "service_response": ...}``. The response data
        belongs at the top level of ha_call_service's reply exactly ONCE — the
        placement the component path (``_build_component_call_response``) already
        uses — so it is peeled off here, BEFORE projection, leaving only the
        changed states to project into ``result``. Returning it in both places
        shipped it twice, byte-identical, doubling its token cost (issue #2085).

        A legitimately null ``service_response`` is still reported as present
        (``True``) so the caller emits the key.

        With ``return_response`` false there is no envelope to split: HA returns a
        plain changed-states list, so it passes through untouched and no
        ``service_response`` key is emitted (``present`` False). That is the
        overwhelming majority of calls.

        Anything else — a non-dict reply, a missing key, or a ``changed_states``
        that is not a list — means a non-conforming responder (HA core always
        sends both keys, with a list). Every such shape takes ONE path: the whole
        reply becomes the response data, ``result`` stays empty, and a warning
        says so. Uniformity is the point — peeling a recognised key out of an
        unrecognised envelope would silently discard whatever else it carried,
        and an empty ``result`` would otherwise read as an affirmative "no entity
        states changed". A caller that asked for response data must never get back
        neither the data nor an explanation.
        """
        if not return_response:
            return result, None, False, []
        if (
            isinstance(result, dict)
            and "service_response" in result
            and isinstance(result.get("changed_states"), list)
        ):
            return result["changed_states"], result["service_response"], True, []
        # Any other shape is non-conforming. Hand the WHOLE reply back as the
        # response data rather than guessing which part is which: splitting one
        # recognised key out of an unrecognised envelope discards the rest (a
        # non-list ``changed_states`` would vanish entirely) and would make the
        # warning's "the whole reply is reported under 'service_response'" a lie.
        # Projecting nothing into ``result`` keeps the records from shipping twice.
        return [], result, True, [_NON_ENVELOPE_WARNING]

    @staticmethod
    def _project_service_result(
        result: Any,
        *,
        entity_id: str | None,
        verbose: bool,
        fields: list[str] | None,
        attribute_keys: list[str] | None,
    ) -> tuple[Any, list[str]]:
        """Apply compact / explicit projection to a service-call ``result``.

        Issue #1446. Precedence:

        - ``verbose=True``: bypass every transformation; return ``result`` as-is.
          (``result`` here is always changed-state records, never an envelope:
          the legacy path splits the envelope off before calling this — see
          ``_split_return_response_envelope`` — and the component path passes
          transition ``new_state``s, which are never enveloped to begin with.)
        - Explicit ``fields`` or ``attribute_keys``: apply per-record projection
          via ``project_entity_record`` to every record. No compaction; this is
          the power-user path.
        - Default: apply ``compact_service_result`` (filter to ``entity_id``
          record when single string, drop top-level metadata + heavy lists).

        Returns ``(projected, warnings)``. ``warnings`` collects per-record
        typo-guard diagnostics from ``project_entity_record`` (e.g. all-empty
        ``attribute_keys`` filter) — deduplicated so an N-record list with the
        same typo doesn't emit N copies of the same warning.
        """
        if verbose:
            return result, []
        if fields is None and attribute_keys is None:
            return compact_service_result(result, entity_id), []
        if not isinstance(result, list):
            return result, []
        warnings: list[str] = []
        # ``result_attribute_keys`` only takes effect when ``attributes`` is in
        # the projected ``result_fields`` (or ``result_fields`` is None). Surface
        # a warning rather than silently ignoring the parameter — mirrors
        # ha_get_state's attribute_keys_no_effect handling.
        if (
            attribute_keys is not None
            and fields is not None
            and "attributes" not in fields
        ):
            warnings.append(
                "result_attribute_keys was ignored because 'attributes' is not "
                "in result_fields. Add 'attributes' to result_fields (or omit "
                "result_fields) to apply result_attribute_keys."
            )
        projected: list[Any] = []
        seen_warnings: set[str] = set()
        for record in result:
            new_record, warn = project_entity_record(record, fields, attribute_keys)
            projected.append(new_record)
            if warn and warn not in seen_warnings:
                seen_warnings.add(warn)
                warnings.append(warn)
        return projected, warnings

    @staticmethod
    def _component_verified_state(
        transitions: list[Any], entity_id: str | None
    ) -> str | None:
        """The confirmed post-state for ``entity_id`` from the component transitions.

        ``ha_call_service`` targets a single entity, so the component returns one
        transition for it. Returns the transition's ``new_state.state`` (the REAL
        post-dispatch state), or ``None`` when the entity vanished / has no state.
        """
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            if entity_id is None or transition.get("entity_id") == entity_id:
                new_state = transition.get("new_state")
                if isinstance(new_state, dict):
                    return new_state.get("state")
        return None

    @staticmethod
    def _raise_for_missing_or_unavailable_target(
        transitions: list[Any], entity_id: str
    ) -> None:
        """Report why a confirmation never landed, once it's certain it never will.

        Only reached when the component result was NOT confirmed. No-op
        (returns) unless ``entity_id`` is a single literal entity ID (see
        ``is_single_entity_target``): a comma-joined multi-target or a Home
        Assistant magic broadcast target (``"all"`` / ``"none"``) is never a
        literal entity in the state machine, so its transition row carries a
        null ``old_state`` unconditionally -- that proves nothing about
        whether the (possibly successful) broadcast landed, unlike a genuine
        single-entity miss.

        For a real single-entity target: the transition's ``old_state`` is
        ``None`` for an entity absent from the state machine at pre-dispatch
        time — the component excludes it from the wait entirely (it
        structurally cannot ever confirm), so an unconfirmed result with a
        null ``old_state`` is unambiguous: raise ENTITY_NOT_FOUND immediately.
        A real state dict whose ``state`` is ``"unavailable"`` stayed IN the
        component's confirmation wait (unlike a nonexistent target, it can
        legitimately reconnect and transition mid-dispatch — the caller only
        reaches this branch after that chance has already lapsed); what
        happened to it by ``new_state`` (the best-available POST-dispatch
        read: ``_post_state`` re-reads live if the listener's exact
        expected-state event never arrived) then decides the outcome:

        * ``new_state`` is ``None`` — the entity was REMOVED during the
          dispatch, a more specific outcome than "still unavailable" (which
          would falsely assert it still exists), so raise ENTITY_NOT_FOUND.
        * ``new_state`` still shows ``"unavailable"`` — raise
          ENTITY_UNAVAILABLE.
        * ``new_state`` shows anything else — the target reconnected but
          settled on some state other than the hint (e.g. a lock that
          reached "locking", not yet "locked"), demonstrably no longer
          unavailable even though this op never confirmed. Reporting it as
          ENTITY_UNAVAILABLE from the stale pre-state alone would discard
          that real result, so no-op instead.

        No-op also for any other unconfirmed target — a real, available
        entity whose confirming event simply never arrived, OR no transition
        row at all (``_dispatched_unconfirmed_result``'s empty
        ``transitions`` after a post-dispatch formatting failure — the write
        already landed, so an absent row proves nothing about existence) —
        leaving the caller's existing generic partial/timeout wording for
        those genuine-ambiguity cases.
        """
        if not is_single_entity_target(entity_id):
            return
        transition = next(
            (
                t
                for t in transitions
                if isinstance(t, dict) and t.get("entity_id") == entity_id
            ),
            None,
        )
        if transition is None:
            return
        old_state = transition.get("old_state")
        if old_state is None:
            raise_failure(create_entity_not_found_error(entity_id))
        if not (
            isinstance(old_state, dict) and old_state.get("state") == "unavailable"
        ):
            return
        new_state = transition.get("new_state")
        if new_state is None:
            # Removed during the dispatch -- a more specific outcome than
            # "still unavailable", which would falsely assert it still exists.
            raise_failure(create_entity_not_found_error(entity_id))
        if isinstance(new_state, dict) and new_state.get("state") == "unavailable":
            raise_failure(create_entity_unavailable_after_dispatch_error(entity_id))

    def _build_component_call_response(
        self,
        component_result: dict[str, Any],
        *,
        domain: str,
        service: str,
        entity_id: str | None,
        data: str | dict[str, Any] | None,
        should_wait: bool,
        return_response: bool,
        verbose: bool,
        fields: list[str] | None,
        attribute_keys: list[str] | None,
    ) -> dict[str, Any]:
        """Map the component ``call_service`` result into ha_call_service's shape.

        The component's real pre->post transition replaces the WS-subscribe-and-sample
        verification (``_SERVICE_TO_STATE`` is now handed to the component as a
        confirmation-timing HINT, not read here as the returned value): the
        transition ``new_state`` records are the same ``State.as_dict()`` shape the
        legacy REST POST returns, so they feed the SAME ``_project_service_result``
        projection; the confirmed target's ``new_state.state`` becomes
        ``verified_state``; and the component's ``partial`` flag (dispatched but the
        confirming event lapsed) drives the same partial-success shape the legacy
        timeout path produces (``_build_timeout_response``).
        """
        transitions = component_result.get("transitions") or []
        # A nonexistent target's pre-dispatch read excludes it from the wait
        # entirely, so it can never reach ``confirmed``; an "unavailable" one
        # stays eligible and CAN reach it (a genuine reconnect-and-confirm) —
        # only an UNCONFIRMED result is checked here, before building a generic
        # partial/timeout response, using the same ``old_state``/``new_state``
        # signal ``device_control.py``'s bulk-op mapping already reads from
        # this identical transitions shape. A genuine confirmation lapse on a
        # real, available target (old_state present, not "unavailable") falls
        # through unchanged to the existing partial-success wording below.
        if (
            should_wait
            and entity_id is not None
            and not component_result.get("confirmed")
        ):
            self._raise_for_missing_or_unavailable_target(transitions, entity_id)
        # The transition new_states are State.as_dict() records — the same shape the
        # legacy REST POST returns — so the existing projection helpers apply
        # unchanged (compact filters to the target, drops metadata / heavy lists).
        new_states = [
            transition["new_state"]
            for transition in transitions
            if isinstance(transition, dict)
            and isinstance(transition.get("new_state"), dict)
        ]
        if "changed_states" in component_result:
            new_states = component_result["changed_states"]
        projected_result, projection_warnings = self._project_service_result(
            new_states,
            entity_id=entity_id,
            verbose=verbose,
            fields=fields,
            attribute_keys=attribute_keys,
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
        if projection_warnings:
            response.setdefault("warnings", []).extend(projection_warnings)
        if component_result.get("warnings"):
            response.setdefault("warnings", []).extend(component_result["warnings"])
        if return_response:
            # Emit the key whenever it was requested, even for a null response —
            # the legacy path does the same (``_split_return_response_envelope``),
            # and gating on ``is not None`` made the two paths answer the same
            # call with different shapes. The component only sets the key for a
            # non-null response, so a null one arrives as an absent key here.
            response["service_response"] = component_result.get("service_response")
            # ...which makes an absent key ambiguous: genuinely-null response, or
            # one the component produced and dropped with the confirmation on its
            # dispatched-unconfirmed path. Only the latter is possible when
            # ``partial``, so warn there rather than on every null.
            if "service_response" not in component_result and component_result.get(
                "partial"
            ):
                response.setdefault("warnings", []).append(
                    _COMPONENT_RESPONSE_UNCONFIRMED_WARNING
                )
        if should_wait:
            if component_result.get("partial"):
                # Reached only when _raise_for_missing_or_unavailable_target above
                # did NOT raise -- i.e. a genuine confirmation lapse on a real,
                # available target whose confirming state_changed simply never
                # arrived within the wait. success stays True here (the same
                # partial-success contract the legacy timeout path reports);
                # unlike this generic case, a target proven nonexistent or still
                # unavailable is a raised ToolError, not a partial success.
                response["partial"] = True
                response.setdefault("warnings", []).append(
                    "Service executed but state change could not be verified "
                    "within timeout."
                )
            else:
                verified_state = self._component_verified_state(transitions, entity_id)
                if verified_state is not None:
                    response["verified_state"] = verified_state
        return response
