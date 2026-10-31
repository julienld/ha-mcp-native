"""Shared integration inspection with finite metadata, flow and diagnostics ports."""

import asyncio
import logging
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure
from .flows.settings import redaction_enabled
from .integration_entries import IntegrationEntryReads
from .integration_inspection import fetch_entry_options_with_status
from .integration_logs import get_logger_levels
from .projection import build_pagination_metadata

logger = logging.getLogger(__name__)


class IntegrationListReads(IntegrationEntryReads):
    async def _list_entries(
        self,
        domain: str | None,
        query: str | None,
        include_opts: bool | None,
        exact_match: bool | None,
        limit_int: int,
        offset_int: int,
    ) -> dict[str, Any]:
        """List config entries with optional domain/query filtering and pagination."""
        # Component fast path: one in-process read (domain filtered server-side,
        # options materialized on each row) replaces the REST list + per-entry
        # OptionsFlow probes. Normalize the domain to HA's canonical lowercase so
        # the component's exact-match filter mirrors the legacy client-side one.
        domain_norm = domain.strip().lower() if domain else None
        rows = await self._component_entries(domain=domain_norm)
        if rows is not None:
            return await self._list_entries_from_component(
                rows, domain, query, include_opts, exact_match, limit_int, offset_int
            )

        # Use REST API endpoint for config entries
        response = await self.backend.execute("config.entries.list", {}, self.context)

        if not isinstance(response, list):
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from Home Assistant",
                    context={"response_type": type(response).__name__},
                )
            )

        entries = response

        # Apply domain filter before formatting
        if domain:
            domain_lower = domain.strip().lower()
            entries = [
                e for e in entries if e.get("domain", "").lower() == domain_lower
            ]

        # Fetch current logger levels once; enrich each entry with its effective level.
        level_warnings: list[str] = []
        logger_levels = await get_logger_levels(self._flow_client(), level_warnings)

        # `_format_entry` is sync and cannot probe the OptionsFlow; options
        # are filled in by a second async pass below for entries that
        # advertise supports_options=True. See `fetch_entry_options_with_status`.
        formatted_entries = [
            self._format_entry(entry, include_opts, logger_levels, bool(level_warnings))
            for entry in entries
        ]

        # quiet=True: per-entry probe failures are aggregated into a response
        # warning below instead of one log line each (bulk fan-out).
        probe_failures: list[str] = []
        if include_opts:
            options_targets = [
                e for e in formatted_entries if e.get("supports_options")
            ]
            if options_targets:
                fetched = await asyncio.gather(
                    *(
                        fetch_entry_options_with_status(
                            self._flow_client(), e["entry_id"], quiet=True
                        )
                        for e in options_targets
                    ),
                    return_exceptions=True,
                )
                for entry, probe in zip(options_targets, fetched, strict=True):
                    if isinstance(probe, BaseException):
                        raise probe
                    opts, probe_ok = probe
                    entry["options"] = opts
                    if not probe_ok:
                        probe_failures.append(entry["entry_id"])

        return self._finalize_entry_list(
            formatted_entries,
            domain,
            query,
            exact_match,
            limit_int,
            offset_int,
            probe_failures,
            level_warnings,
        )

    async def _list_entries_from_component(
        self,
        rows: list[dict[str, Any]],
        domain: str | None,
        query: str | None,
        include_opts: bool | None,
        exact_match: bool | None,
        limit_int: int,
        offset_int: int,
    ) -> dict[str, Any]:
        """List config entries from a component ``config_entries`` read.

        The component already filtered by ``domain`` (server-side) and
        materialized each entry's ``options`` on the row, so the read itself
        needs no per-entry OptionsFlow probe. ``options`` are raw persisted
        values (a field never set may be absent) and may contain
        ``"**redacted**"`` markers.
        With ``redact_secrets`` on, the returned page IS probed per entry
        after pagination (``_redact_component_options``) to learn each
        entry's password markers — the one case where this path probes.
        """
        level_warnings: list[str] = []
        logger_levels = await get_logger_levels(self._flow_client(), level_warnings)
        formatted_entries = [
            self._format_entry(row, include_opts, logger_levels, bool(level_warnings))
            for row in rows
        ]
        result = self._finalize_entry_list(
            formatted_entries,
            domain,
            query,
            exact_match,
            limit_int,
            offset_int,
            [],
            level_warnings,
        )
        # Component options are raw persisted values — under redact_secrets
        # every RETURNED row must be redacted (#2157). Runs on the paginated
        # slice only: entries outside it are not in the response, and
        # bounding the probe fan-out to the page keeps the component fast
        # path from probing an entire installation per list call.
        if include_opts and redaction_enabled():
            redaction_warnings: list[str] = []
            probes = await asyncio.gather(
                *(
                    self._redact_component_options(
                        formatted.get("entry_id"), formatted, redaction_warnings
                    )
                    for formatted in result["entries"]
                ),
                return_exceptions=True,
            )
            # A failing flow must not detach sibling probes from this owned call.
            for probe in probes:
                if isinstance(probe, BaseException):
                    raise probe
            if redaction_warnings:
                result.setdefault("warnings", []).extend(redaction_warnings)
        return result

    def _finalize_entry_list(
        self,
        formatted_entries: list[dict[str, Any]],
        domain: str | None,
        query: str | None,
        exact_match: bool | None,
        limit_int: int,
        offset_int: int,
        probe_failures: list[str],
        extra_warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        """Query-filter, summarize, and paginate formatted entries.

        Shared tail of the component-served and legacy list paths so their
        response shapes stay identical. ``probe_failures`` is always empty on
        the component path (options ride the same read — no per-entry probe).
        """
        # Apply search filter if query provided
        if query and query.strip():
            formatted_entries = self._filter_by_query(
                formatted_entries, query, exact_match
            )

        # Group by state for summary (computed before pagination for full picture)
        state_summary: dict[str, int] = {}
        for entry in formatted_entries:
            state = entry.get("state", "unknown")
            state_summary[state] = state_summary.get(state, 0) + 1

        # Apply pagination
        total_entries = len(formatted_entries)
        paginated_entries = formatted_entries[offset_int : offset_int + limit_int]

        result_data: dict[str, Any] = {
            "success": True,
            **build_pagination_metadata(
                total_entries, offset_int, limit_int, len(paginated_entries)
            ),
            "entries": paginated_entries,
            "state_summary": state_summary,
            "query": query if query else None,
        }
        if domain:
            result_data["domain_filter"] = domain.strip().lower()
        if extra_warnings:
            result_data.setdefault("warnings", []).extend(extra_warnings)
        if probe_failures:
            result_data.setdefault("warnings", []).append(
                f"options probe failed for {len(probe_failures)} "
                f"entr{'y' if len(probe_failures) == 1 else 'ies'} "
                f"({', '.join(probe_failures)}) - their 'options' may be "
                "incomplete; empty options does not mean an entry has none"
            )
        return result_data

    @staticmethod
    def _format_entry(
        entry: dict[str, Any],
        include_opts: bool | None,
        logger_levels: dict[str, dict[str, Any]] | None = None,
        levels_unknown: bool = False,
    ) -> dict[str, Any]:
        """Format a raw config entry into the response shape."""
        formatted_entry: dict[str, Any] = {
            "entry_id": entry.get("entry_id"),
            "domain": entry.get("domain"),
            "title": entry.get("title"),
            "state": entry.get("state"),
            "source": entry.get("source"),
            "supports_options": entry.get("supports_options", False),
            "supports_unload": entry.get("supports_unload", False),
            "supports_reconfigure": entry.get("supports_reconfigure", False),
            "disabled_by": entry.get("disabled_by"),
        }

        # Surface the effective Python logger level for this integration
        # ("DEFAULT" = no override; falls back to the root logger level).
        # `log_level_raw` is the original numeric level (None when no override
        # exists or HA returned a string instead of an int).
        if logger_levels is not None:
            domain = entry.get("domain") or ""
            level_info = logger_levels.get(domain)
            # UNKNOWN, not DEFAULT: an unreadable level is not evidence that the
            # integration runs at the default one (#1947).
            formatted_entry["log_level"] = (
                "UNKNOWN"
                if levels_unknown
                else (level_info["name"] if level_info else "DEFAULT")
            )
            formatted_entry["log_level_raw"] = level_info["raw"] if level_info else None

        # Include options when requested (for auditing template definitions, etc.)
        if include_opts:
            formatted_entry["options"] = entry.get("options", {})

        # Include pref_disable_new_entities and pref_disable_polling if present
        if "pref_disable_new_entities" in entry:
            formatted_entry["pref_disable_new_entities"] = entry[
                "pref_disable_new_entities"
            ]
        if "pref_disable_polling" in entry:
            formatted_entry["pref_disable_polling"] = entry["pref_disable_polling"]

        return formatted_entry

    @staticmethod
    def _filter_by_query(
        entries: list[dict[str, Any]], query: str, exact_match: bool | None
    ) -> list[dict[str, Any]]:
        """Filter formatted entries by query string with exact or fuzzy matching."""
        matches: list[tuple[int, dict[str, Any]]] = []
        query_lower = query.strip().lower()

        for entry in entries:
            domain_lower = (entry.get("domain") or "").lower()
            title_lower = (entry.get("title") or "").lower()

            # Check for exact substring matches first (highest priority)
            if query_lower in domain_lower or query_lower in title_lower:
                matches.append((100, entry))
            elif not exact_match:
                # Fuzzy matching only when exact_match is disabled
                from ha_mcp.utils.fuzzy_search import calculate_ratio

                domain_score = calculate_ratio(query_lower, domain_lower)
                title_score = calculate_ratio(query_lower, title_lower)
                best_score = max(domain_score, title_score)

                if best_score >= 70:  # threshold for fuzzy matches
                    matches.append((best_score, entry))

        # Sort by score descending
        matches.sort(key=lambda x: x[0], reverse=True)
        return [match[1] for match in matches]
