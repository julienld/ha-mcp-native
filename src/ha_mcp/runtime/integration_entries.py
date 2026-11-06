"""Shared integration inspection with finite metadata, flow and diagnostics ports."""

import logging
from typing import Any

from ha_mcp.errors import HomeAssistantAPIError
from ha_mcp.tool_api import ToolFailure

from .errors import websocket_error_message
from .flows.settings import redaction_enabled
from .integration_inspection import (
    IntegrationInspection,
    _split_component_entry_row,
    fetch_entry_options_with_status,
)
from .integration_logs import get_logger_levels

logger = logging.getLogger(__name__)


class IntegrationEntryReads(IntegrationInspection):
    async def _get_single_entry(
        self,
        entry_id: str,
        include_schema: bool | None,
        *,
        include_subentries: bool,
        include_subentry_schema: bool,
        subentry_type: str | None,
        subentry_id: str | None,
        show_advanced_options: bool,
    ) -> dict[str, Any]:
        """Fetch a single config entry by ID, optionally including its options schema."""
        try:
            rows = await self._component_entries(entry_id=entry_id)
            if rows is not None:
                return await self._single_entry_from_component(
                    entry_id,
                    rows,
                    include_schema,
                    include_subentries=include_subentries,
                    include_subentry_schema=include_subentry_schema,
                    subentry_type=subentry_type,
                    subentry_id=subentry_id,
                    show_advanced_options=show_advanced_options,
                )

            result = await self._flow_client().get_config_entry(entry_id)
            entry_domain = result.get("domain") if isinstance(result, dict) else None

            # Surface `options` on every per-entry response (HA's REST endpoint
            # omits the field). For entries with supports_options=True we probe
            # via OptionsFlow — see `fetch_entry_options_with_status`. When
            # include_schema is also requested, `_fetch_options_schema` below
            # populates options from the same flow init so we don't pay for
            # two round-trips.
            probe_warnings: list[str] = []
            await self._probe_legacy_entry_options(
                result, entry_id, include_schema, probe_warnings
            )

            resp: dict[str, Any] = {
                "success": True,
                "entry_id": entry_id,
                "entry": result,
            }
            if probe_warnings:
                resp["warnings"] = probe_warnings

            # Surface the effective Python logger level for this integration
            # so users can confirm log level changes took effect.
            # Emit unconditionally for symmetry with the list path (_format_entry).
            level_warnings: list[str] = []
            logger_levels = await get_logger_levels(self._flow_client(), level_warnings)
            level_info = logger_levels.get(entry_domain or "")
            # UNKNOWN, not DEFAULT: an unreadable level is not evidence that the
            # integration runs at the default one (#1947).
            resp["log_level"] = (
                "UNKNOWN"
                if level_warnings
                else (level_info["name"] if level_info else "DEFAULT")
            )
            resp["log_level_raw"] = level_info["raw"] if level_info else None
            if level_warnings:
                resp.setdefault("warnings", []).extend(level_warnings)

            # Optionally fetch options flow schema (logically read-only: start+abort)
            if include_schema and result.get("supports_options"):
                await self._fetch_options_schema(entry_id, resp)

            if include_subentries:
                subentries = await self._fetch_config_subentries(entry_id)
                resp["subentry_count"] = len(subentries)
                resp["subentries"] = subentries

            if include_subentry_schema:
                await self._fetch_config_subentry_schema(
                    entry_id,
                    resp,
                    subentry_type=subentry_type,
                    subentry_id=subentry_id,
                    show_advanced_options=show_advanced_options,
                )

            return resp
        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001
            self._raise_inspection_error(
                e,
                context={"entry_id": entry_id},
                suggestions=[
                    "Use ha_get_integration() without entry_id to see all "
                    "config entries",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises

    async def _probe_legacy_entry_options(
        self,
        result: Any,
        entry_id: str,
        include_schema: bool | None,
        probe_warnings: list[str],
    ) -> None:
        """Fill a legacy REST entry's ``options`` via OptionsFlow, noting misses.

        Mutates ``result['options']`` in place and appends to ``probe_warnings``
        when the probe fails. No-op for a non-dict ``result`` or when
        ``include_schema`` is set (the schema path populates options instead).
        """
        if isinstance(result, dict):
            result.setdefault("options", {})
            if result.get("supports_options") and not include_schema:
                options, probe_ok = await fetch_entry_options_with_status(
                    self._flow_client(), entry_id
                )
                result["options"] = options
                if not probe_ok:
                    probe_warnings.append(
                        f"options probe failed for {entry_id}: the "
                        "OptionsFlow could not be read, so 'options' may "
                        "be incomplete — empty options does not mean the "
                        "entry has none"
                    )

    async def _single_entry_from_component(
        self,
        entry_id: str,
        rows: list[dict[str, Any]],
        include_schema: bool | None,
        *,
        include_subentries: bool,
        include_subentry_schema: bool,
        subentry_type: str | None,
        subentry_id: str | None,
        show_advanced_options: bool,
    ) -> dict[str, Any]:
        """Build the single-entry response from a component ``config_entries`` read.

        The component row already carries the entry identity, its raw persisted
        ``options`` (secret-scrubbed), and its ``subentries`` identity rows — so
        this one read replaces the legacy REST list-all + OptionsFlow
        start/abort probe + subentries WS call. The options schema (and the
        subentry schema) still come from the legacy live flow: a schema only
        exists inside an open flow, which the component cannot serialize. When a
        schema is requested the component's ``options`` are kept
        (``populate_options=False``) rather than overwritten by the
        OptionsFlow-derived suggested-value shape.

        ``log_level`` / ``log_level_raw`` come from ``get_logger_levels`` on both
        paths (the component does not carry logger overrides).
        """
        if not rows:
            # ``async_get_entry(entry_id)`` found nothing — an authoritative
            # not-found, mapped to the same 404 the legacy REST get raises.
            raise HomeAssistantAPIError(
                f"Config entry not found: {entry_id}", status_code=404
            )
        entry, subentries = _split_component_entry_row(rows[0])
        entry.setdefault("options", {})

        resp: dict[str, Any] = {
            "success": True,
            "entry_id": entry_id,
            "entry": entry,
        }

        # Surface the effective Python logger level for this integration
        # (unconditionally, for symmetry with the legacy path and _format_entry).
        level_warnings: list[str] = []
        logger_levels = await get_logger_levels(self._flow_client(), level_warnings)
        level_info = logger_levels.get(entry.get("domain") or "")
        # UNKNOWN, not DEFAULT — see the component path above.
        resp["log_level"] = (
            "UNKNOWN"
            if level_warnings
            else (level_info["name"] if level_info else "DEFAULT")
        )
        resp["log_level_raw"] = level_info["raw"] if level_info else None
        if level_warnings:
            resp.setdefault("warnings", []).extend(level_warnings)

        # Component options are raw persisted values — under redact_secrets
        # they must be redacted on EVERY read, not only when a schema was
        # requested (#2157). Runs even when the include_schema fetch below
        # will probe again: that path only redacts when the flow opens with
        # a form, so relying on it alone would fail OPEN on menu-first flows
        # and probe failures. Redaction is idempotent, so the double
        # application costs one extra probe and nothing else.
        if redaction_enabled():
            redact_warnings: list[str] = []
            await self._redact_component_options(entry_id, entry, redact_warnings)
            if redact_warnings:
                resp.setdefault("warnings", []).extend(redact_warnings)

        # Options schema only exists in a live options flow — read it from the
        # legacy flow, but keep the component-provided options (populate_options
        # False) so the raw persisted values win over the flow-derived shape.
        if include_schema and entry.get("supports_options"):
            await self._fetch_options_schema(entry_id, resp, populate_options=False)

        if include_subentries:
            resp["subentry_count"] = len(subentries)
            resp["subentries"] = subentries

        if include_subentry_schema:
            await self._fetch_config_subentry_schema(
                entry_id,
                resp,
                subentry_type=subentry_type,
                subentry_id=subentry_id,
                show_advanced_options=show_advanced_options,
            )

        return resp

    async def _attach_knx_project(self, resp: dict[str, Any], entry_id: str) -> None:
        """Attach the parsed KNX ETS project to a single-entry response.

        Reads ``knx/get_knx_project`` (the same command the KNX panel uses) and
        attaches the group-address table, group-range hierarchy, and project
        metadata under ``resp["knx_project"]``. The KNX integration exposes one
        project globally, so the command takes no entry scope; this is gated on
        the entry actually being a KNX integration to avoid attaching unrelated
        data to a non-KNX entry.

        Failures are surfaced as warnings rather than raised — the primary
        entry read already succeeded, mirroring how diagnostics fetch failures
        are handled. The "KNX integration not loaded" case collapses into
        ha_get_integration's existing no-such-entry handling (no KNX entry → no
        entry_id to pass here), so it needs no special casing.
        """
        entry = resp.get("entry") if isinstance(resp.get("entry"), dict) else {}
        domain = entry.get("domain") if isinstance(entry, dict) else None
        if domain != "knx":
            resp.setdefault("warnings", []).append(
                f"include_knx_project ignored: entry {entry_id} is not a KNX "
                f"integration (domain={domain!r})"
            )
            return

        result = await self.backend.execute(
            "integrations.knx_project", {}, self.context
        )
        if not isinstance(result, dict) or not result.get("success"):
            error_msg = websocket_error_message(
                result.get("error", "Unknown error")
                if isinstance(result, dict)
                else result
            )
            resp.setdefault("warnings", []).append(
                f"Failed to fetch KNX project: {error_msg}"
            )
            return

        project = result.get("result")
        if not project:
            # No ETS project uploaded yet — get_knxproject() returns None.
            resp["knx_project"] = {
                "count": 0,
                "group_addresses": {},
                "group_ranges": {},
                "info": {},
                "note": (
                    "The KNX integration is loaded but no ETS project has been "
                    "uploaded yet. Upload a .knxproj file via the KNX panel to "
                    "populate the group-address table."
                ),
            }
            return

        group_addresses = project.get("group_addresses", {})
        resp["knx_project"] = {
            "count": len(group_addresses),
            "group_addresses": group_addresses,
            "group_ranges": project.get("group_ranges", {}),
            "info": project.get("info", {}),
        }
