"""Shared integration inspection with finite metadata, flow and diagnostics ports."""

import asyncio
import logging
from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import BackendTools, ToolFailure

from .errors import raise_classified, raise_failure, websocket_error_message
from .flows.form import iter_schema_fields
from .flows.session import FlowSession
from .flows.settings import redaction_enabled
from .redaction import (
    harvest_secret_strings,
    is_password_flow_field,
    redact_flow_schema,
    redact_options_by_flow_schema,
    sentinel_replacement,
)

logger = logging.getLogger(__name__)


def _preserve_uncertain_probe(error: Exception) -> None:
    if isinstance(error, ToolFailure) and error.status == "outcome_unknown":
        raise error


def options_from_form_flow(flow: dict[str, Any]) -> dict[str, Any]:
    """Extract ``{field_name: current_value}`` from a form-type OptionsFlow.

    Reads each ``data_schema`` entry's ``description.suggested_value``
    first: HA's ``add_suggested_values_to_schema`` injects the entry's
    *persisted* option there (voluptuous renders ``suggested_value=...``
    into the ``description`` sub-object, not as a top-level field key),
    and it is what the HA UI renders as the current value. Falls back to
    ``default`` (the static schema default a brand-new form would show)
    and then ``value`` (constant-type fields ship ``value`` instead of
    ``default``). A field can carry both ``suggested_value`` and
    ``default`` at once — e.g. a group helper's ``hide_members`` stored as
    ``True`` over a schema default of ``False`` — and the stored value
    must win (issue #1575). Nested section fields are flattened into the
    returned top-level map. Fields with a missing or ``None`` value are skipped.
    """
    out: dict[str, Any] = {}
    # Defensive: HA should always return a list of dict fields, but guard
    # against malformed shapes so a bad response degrades to {} instead of
    # raising AttributeError (e.g. a string data_schema would iterate chars).
    data_schema = flow.get("data_schema")
    if not isinstance(data_schema, list):
        return out
    redact = redaction_enabled()
    for field in iter_schema_fields(data_schema):
        name = field.get("name")
        if name is None:
            continue
        value = None
        description = field.get("description")
        if isinstance(description, dict):
            value = description.get("suggested_value")
        if value is None:
            value = field.get("default", field.get("value"))
        if value is not None:
            if redact and is_password_flow_field(field):
                # Password-marked field (issue #2157): remember the live
                # value(s) for the global known-value scrub, then emit
                # set/empty sentinel(s) instead of the value itself
                # (multiple-value selectors keep their list shape).
                harvest_secret_strings(value)
                out[name] = sentinel_replacement(value)
            else:
                out[name] = value
    return out


async def fetch_entry_options_with_status(
    client: Any, entry_id: str, *, quiet: bool = False
) -> tuple[dict[str, Any], bool]:
    """Read a config entry's ``options`` and report whether the probe succeeded.

    Starts the entry's OptionsFlow, harvests ``{name: current_value}`` from
    its first-step form via :func:`options_from_form_flow` (the persisted
    option from ``description.suggested_value``, falling back to the schema
    ``default``), and aborts the flow so it doesn't sit half-open. Returns
    ``(options, ok)`` so callers can tell a probe *failure* apart from a
    genuinely-empty options form — both yield ``{}`` for ``options``, but
    ``ok`` is:

    - ``True`` only when a form first-step was read (even if it harvested no
      fields: a genuinely-empty options form is a successful read).
    - ``False`` when the OptionsFlow could not be read into options: the flow
      raised, or its first step was not a form (a menu / abort / create_entry),
      so no options could be harvested.

    The flag lets callers surface degraded reads instead of passing ``{}``
    off as real options: ``smart_search`` flips ``partial`` when a probe
    fails mid-search, and ``ha_get_integration`` attaches a ``warnings``
    entry on its single-entry and list responses. The abort in ``finally``
    is cleanup; a failed abort does not flip ``ok`` (the options were
    already harvested).

    Home Assistant does not expose ``ConfigEntry.options`` through any
    read-only REST or WebSocket endpoint — ``/api/config/config_entries/entry``
    deliberately omits the field. The closest approximation that the HA UI
    itself uses is the OptionsFlow's first-step ``data_schema``: HA injects
    the persisted options into each field's ``description.suggested_value``
    (via ``add_suggested_values_to_schema``), which
    :func:`options_from_form_flow` prefers over the static schema
    ``default`` (issue #1575).

    Probe failures log at ``warning`` (so breakage of a deliberate
    single-entry probe is discoverable) unless ``quiet=True``, which demotes
    them to ``debug`` for bulk fan-out callers (e.g. ``smart_search`` probes
    one entry per flow-helper on every ``ha_search``; a per-entry
    warning there would spam the log on routine searches).

    Exposed at module level (not as a method) so non-class callers such as
    ``smart_search._search_flow_helpers`` can probe flow-helper config
    without instantiating ``IntegrationTools``.
    """
    async with client.write_scope(entry_id):
        log_probe_failure = logger.debug if quiet else logger.warning
        flow_id: str | None = None
        flow_type = None
        try:
            flow = await client.start_options_flow(entry_id)
            flow_id = flow.get("flow_id")
            flow_type = flow.get("type")
            if flow_type != "form":
                log_probe_failure(
                    f"OptionsFlow for {entry_id} returned type={flow_type!r}, "
                    f"not a form — cannot extract options"
                )
                return {}, False
            return options_from_form_flow(flow), True
        except Exception as exc:  # noqa: BLE001
            _preserve_uncertain_probe(exc)
            log_probe_failure(
                f"Failed to fetch options for {entry_id}: {type(exc).__name__}: {exc}"
            )
            return {}, False
        finally:
            if flow_id and flow_type not in {"abort", "create_entry"}:
                try:
                    await client.abort_options_flow(flow_id)
                except Exception as abort_err:  # noqa: BLE001
                    log_probe_failure(
                        f"Failed to abort options flow {flow_id}: "
                        f"{type(abort_err).__name__}: {abort_err}"
                    )


def _split_component_entry_row(
    row: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split a component ``config_entries`` row into ``(entry, subentries)``.

    The returned ``entry`` mirrors the legacy REST per-entry shape: the row's
    identity + status fields and its already-materialized ``options`` (raw
    persisted, secret-scrubbed), with the nested ``subentries`` list lifted out
    so callers surface subentries at the top level exactly like the legacy
    ``include_subentries`` branch — and never leak them onto ``entry`` when
    subentries were not requested. ``options`` values may be ``"**redacted**"``
    where the component scrubbed a resolved ``!secret``.
    """
    entry = dict(row)
    subentries = entry.pop("subentries", None)
    if not isinstance(subentries, list):
        subentries = []
    return entry, subentries


class IntegrationInspection(BackendTools):
    def _flow_client(self) -> FlowSession:
        return FlowSession(
            self.backend,
            self.context,
            entry_guard=getattr(self, "_entry_write_guard", None),
        )

    async def _component_entries(self, **query: Any) -> list[dict[str, Any]] | None:
        result = await self.backend.execute(
            "integrations.entries",
            {key: value for key, value in query.items() if value is not None},
            self.context,
        )
        return result["entries"] if result is not None else None

    def _raise_inspection_error(
        self,
        error: Exception,
        context: dict[str, Any] | None = None,
        *,
        suggestions: list[str] | None = None,
    ) -> NoReturn:
        raise_classified(error, context, suggestions=suggestions)

    async def _fetch_config_subentries(self, entry_id: str) -> list[dict[str, Any]]:
        """Fetch config subentries for a detailed entry response."""
        result = await self._flow_client().list_config_subentries(entry_id)
        if not isinstance(result, dict) or not result.get("success"):
            error_msg = websocket_error_message(result.get("error", "Operation failed"))
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list config subentries: {error_msg}",
                    context={"entry_id": entry_id},
                )
            )

        subentries = result.get("result")
        if not isinstance(subentries, list):
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected config subentry list response",
                    context={"entry_id": entry_id, "details": result},
                )
            )

        return [subentry for subentry in subentries if isinstance(subentry, dict)]

    async def _fetch_config_subentry_schema(
        self,
        entry_id: str,
        resp: dict[str, Any],
        *,
        subentry_type: str | None,
        subentry_id: str | None,
        show_advanced_options: bool,
    ) -> None:
        """Start a config subentry flow to read its schema, then abort it."""
        client = self._flow_client()
        async with client.write_scope(entry_id):
            if not subentry_type:
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "subentry_type is required when include_subentry_schema=True",
                        suggestions=[
                            "Call ha_get_integration(entry_id=..., "
                            "include_subentries=True) to inspect existing subentry "
                            "types, then retry with subentry_type.",
                        ],
                        context={"entry_id": entry_id},
                    )
                )

            flow_id = None
            flow_type = None
            try:
                flow_result = await client.start_config_subentry_flow(
                    entry_id,
                    subentry_type,
                    subentry_id=subentry_id,
                    show_advanced_options=show_advanced_options,
                )
                flow_id = flow_result.get("flow_id")
                flow_type = flow_result.get("type")
                schema: dict[str, Any] = {
                    "subentry_type": subentry_type,
                    "subentry_id": subentry_id,
                    "flow_type": flow_type,
                    "step_id": flow_result.get("step_id"),
                    "description_placeholders": flow_result.get(
                        "description_placeholders", {}
                    ),
                }
                if flow_type == "form":
                    data_schema = flow_result.get("data_schema", [])
                    # Subentry schema echoes carry live suggested_values just
                    # like the options-flow echo — same redaction (#2157).
                    schema["data_schema"] = (
                        redact_flow_schema(data_schema)
                        if redaction_enabled()
                        else data_schema
                    )
                elif flow_type == "menu":
                    schema["menu_options"] = flow_result.get("menu_options", [])
                else:
                    schema["details"] = flow_result
                resp["subentry_schema"] = schema
            finally:
                if flow_id and flow_type not in {"abort", "create_entry"}:
                    try:
                        await asyncio.wait_for(
                            client.abort_config_subentry_flow(flow_id),
                            timeout=5.0,
                        )
                    except Exception as abort_err:  # noqa: BLE001
                        logger.warning(
                            "Failed to abort config subentry introspection flow %s: %s",
                            flow_id,
                            abort_err,
                        )

    @staticmethod
    def _options_from_form_flow(flow: dict[str, Any]) -> dict[str, Any]:
        """Class-method alias for :func:`options_from_form_flow`."""
        return options_from_form_flow(flow)

    async def _redact_component_options(
        self, entry_id: Any, entry: dict[str, Any], warnings: list[str]
    ) -> None:
        """Redact password-marked fields on component-served options (#2157).

        The component row carries raw persisted options with no schema, so
        the password markers must come from a live options-flow probe. When
        the entry supports an options flow but no form schema could be read
        (probe failed, or the flow opens with a menu), every option value is
        replaced with a set/empty sentinel — without the schema the password
        fields cannot be told apart, so this fails closed. Entries without
        an options flow carry no password markers anywhere (the documented
        metadata limit of the toggle); their options pass through and the
        known-value scrub remains the only line for them.
        """
        client = self._flow_client()
        async with client.write_scope(entry_id):
            options = entry.get("options")
            if not isinstance(options, dict) or not options:
                return
            if not entry.get("supports_options"):
                return
            data_schema: Any = None
            flow_id = None
            flow_type = None
            try:
                flow = await client.start_options_flow(entry_id)
                flow_id = flow.get("flow_id")
                flow_type = flow.get("type")
                if flow.get("type") == "form":
                    # An empty/missing schema on a form (e.g. a confirm-only
                    # step) classifies nothing — persisted options can outlive
                    # fields removed from the flow, so treat it as unreadable
                    # and take the fail-closed branch below instead of
                    # "redacting" zero fields.
                    candidate = flow.get("data_schema")
                    if isinstance(candidate, list) and candidate:
                        data_schema = candidate
            except Exception as exc:  # noqa: BLE001
                _preserve_uncertain_probe(exc)
                logger.warning(
                    "redact_secrets: options-flow probe failed for %s: %r",
                    entry_id,
                    exc,
                )
            finally:
                if flow_id and flow_type not in {"abort", "create_entry"}:
                    try:
                        await client.abort_options_flow(flow_id)
                    except Exception as abort_err:  # noqa: BLE001
                        logger.warning(
                            "redact_secrets: failed to abort probe flow %s: %r",
                            flow_id,
                            abort_err,
                        )
            if isinstance(data_schema, list):
                entry["options"] = redact_options_by_flow_schema(options, data_schema)
            else:
                entry["options"] = {
                    key: sentinel_replacement(value) for key, value in options.items()
                }
                warnings.append(
                    f"redact_secrets: no options schema readable for {entry_id} — "
                    "every option value was redacted conservatively (the password "
                    "fields cannot be told apart without the schema)"
                )

    async def _fetch_options_schema(
        self, entry_id: str, resp: dict[str, Any], *, populate_options: bool = True
    ) -> None:
        """Start an options flow to read the schema, then abort it.

        Also populates ``resp["entry"]["options"]`` for form-type flows from
        the same flow result so callers requesting both schema and options
        don't pay for two round-trips. Pass ``populate_options=False`` on the
        component-served path, where ``options`` already carry the raw persisted
        values and must NOT be overwritten by the OptionsFlow-derived
        suggested-value shape.
        """
        client = self._flow_client()
        async with client.write_scope(entry_id):
            flow_id = None
            flow_type = None
            try:
                flow_result = await client.start_options_flow(entry_id)
                flow_id = flow_result.get("flow_id")
                flow_type = flow_result.get("type")
                entry = (
                    resp.get("entry") if isinstance(resp.get("entry"), dict) else None
                )
                if flow_type == "form":
                    data_schema = flow_result.get("data_schema", [])
                    # The schema echo carries the live persisted value of every
                    # field in description.suggested_value — under redact_secrets
                    # password-marked fields get sentinels here too (#2157).
                    # redact_flow_schema deep-copies, so flow_result stays raw for
                    # the options extraction below (which redacts independently).
                    schema_out = (
                        redact_flow_schema(data_schema)
                        if redaction_enabled()
                        else data_schema
                    )
                    resp["options_schema"] = {
                        "flow_type": "form",
                        "step_id": flow_result.get("step_id"),
                        "data_schema": schema_out,
                    }
                    if entry is not None and populate_options:
                        entry["options"] = self._options_from_form_flow(flow_result)
                    elif entry is not None and redaction_enabled():
                        # Component-served path: options carry raw persisted
                        # values (the component only scrubs resolved !secret
                        # references). Now that the flow schema is in hand,
                        # redact the fields it marks as passwords.
                        entry["options"] = redact_options_by_flow_schema(
                            entry.get("options"), data_schema
                        )
                elif flow_type == "menu":
                    resp["options_schema"] = {
                        "flow_type": "menu",
                        "step_id": flow_result.get("step_id"),
                        "menu_options": flow_result.get("menu_options", []),
                    }
            except Exception as schema_err:  # noqa: BLE001
                _preserve_uncertain_probe(schema_err)
                logger.warning(
                    f"Failed to fetch options schema for {entry_id}: "
                    f"{type(schema_err).__name__}: {schema_err}"
                )
                schema_warnings: list[str] = resp.setdefault("warnings", [])
                schema_warnings.append(
                    f"options schema probe failed for {entry_id}: "
                    f"{type(schema_err).__name__} — 'options_schema' is missing "
                    "and 'options' may be incomplete"
                )
            finally:
                if flow_id and flow_type not in {"abort", "create_entry"}:
                    try:
                        await client.abort_options_flow(flow_id)
                    except Exception as abort_err:  # noqa: BLE001
                        logger.warning(
                            f"Failed to abort options flow {flow_id}: "
                            f"{type(abort_err).__name__}: {abort_err}"
                        )
