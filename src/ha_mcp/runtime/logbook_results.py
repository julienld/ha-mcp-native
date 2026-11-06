"""Logbook filters and pagination, detached from transport selection."""

from datetime import UTC, datetime
from typing import Any, Literal

from .log_common import _coerce_limit, _compact_logbook_entries


class LogbookResults:
    @staticmethod
    def _coerce_logbook_params(
        hours_back: int,
        limit: int | None,
        offset: int,
    ) -> tuple[int, int, int]:
        effective_limit = _coerce_limit(limit)
        return hours_back, effective_limit, offset

    @staticmethod
    def _build_pagination_hint(
        offset_int: int,
        effective_limit: int,
        total_entries: int,
        paginated_entries: Any,
        hours_back_int: int,
        end_time: str | None,
        entity_id: str | None,
        device_id: str | None,
        context_id: str | None,
        search: str | None,
        compact_bool: bool,
        order: Literal["newest", "oldest"] = "newest",
    ) -> str:
        """Build reproducible pagination hint string for logbook results.

        String values are quoted: the hint is meant to be copied back as a
        call, and an unquoted ``search=front door`` is not one.
        """
        next_offset = offset_int + effective_limit
        param_parts = [
            f"hours_back={hours_back_int}",
            f"limit={effective_limit}",
            f"offset={next_offset}",
        ]
        if entity_id:
            param_parts.append(f"entity_id={entity_id!r}")
        if device_id:
            param_parts.append(f"device_id={device_id!r}")
        if context_id:
            param_parts.append(f"context_id={context_id!r}")
        if end_time:
            param_parts.append(f"end_time={end_time!r}")
        if search:
            param_parts.append(f"search={search!r}")
        if not compact_bool:
            param_parts.append("compact=False")
        if order != "newest":
            param_parts.append(f"order={order!r}")
        param_str = ", ".join(param_parts)
        return (
            f"Showing entries {offset_int + 1}-{offset_int + len(paginated_entries)} of {total_entries}. "
            f"To get the next page, use: ha_get_logs({param_str})"
        )

    @staticmethod
    def _filter_logbook_by_search(
        response: Any, search: str | None
    ) -> tuple[Any, dict[str, str]]:
        """Filter logbook entries by search term across name/message/entity_id.

        Returns the (possibly filtered) response and a filters_applied dict
        recording which filters were used.
        """
        filters_applied: dict[str, str] = {}
        if search and isinstance(response, list):
            search_lower = search.lower()
            response = [
                e
                for e in response
                if isinstance(e, dict)
                and (
                    search_lower in str(e.get("name", "")).lower()
                    or search_lower in str(e.get("message", "")).lower()
                    or search_lower in str(e.get("entity_id", "")).lower()
                )
            ]
            filters_applied["search"] = search
        return response, filters_applied

    @staticmethod
    def _paginate_logbook_entries(
        response: Any,
        offset_int: int,
        effective_limit: int,
        order: Literal["newest", "oldest"],
    ) -> tuple[Any, int, bool]:
        """Slice logbook entries into the requested page.

        HA's /logbook returns entries oldest-first. Takes a window from
        the end for newest-first (default), or from the start for
        oldest-first, with offset paging deeper in the chosen order.
        """
        total_entries = len(response) if isinstance(response, list) else 1

        if isinstance(response, list):
            if order == "newest":
                end = total_entries - offset_int
                start = max(end - effective_limit, 0)
                paginated_entries = (
                    list(reversed(response[start:end])) if end > 0 else []
                )
            else:
                paginated_entries = response[offset_int : offset_int + effective_limit]
            has_more = offset_int + len(paginated_entries) < total_entries
        else:
            paginated_entries = response
            has_more = False

        return paginated_entries, total_entries, has_more

    @staticmethod
    def _split_logbook_filter(value: str | None) -> list[str]:
        """Split a comma-separated native filter, dropping blank items."""
        if not value:
            return []
        return [item.strip() for item in value.split(",") if item.strip()]

    @staticmethod
    def _normalize_logbook_events(events: Any, transport: str) -> Any:
        """Normalize the WebSocket fallback rows onto the REST-shaped contract.

        ``logbook/get_events`` returns ``when`` as epoch seconds and omits the
        ``name`` field on state rows. The component command and REST both
        return Core's REST shape (ISO ``when``, names from Core's own
        ``EventProcessor``), so they pass through untouched; only the
        WebSocket fallback is rewritten. Names are not reconstructed here:
        without the component or REST the rows simply carry no ``name`` (the
        caller warns).
        """
        if transport != "websocket" or not isinstance(events, list):
            return events
        normalized: list[Any] = []
        for event in events:
            if not isinstance(event, dict):
                normalized.append(event)
                continue
            row = dict(event)
            when = row.get("when")
            if isinstance(when, (int, float)) and not isinstance(when, bool):
                row["when"] = datetime.fromtimestamp(float(when), UTC).isoformat()
            normalized.append(row)
        return normalized

    def logbook_result(
        self,
        response: Any,
        hours_back_int: int,
        effective_limit: int,
        offset_int: int,
        start_timestamp: str,
        end_dt: datetime,
        entity_id: str | None,
        device_id: str | None,
        context_id: str | None,
        search: str | None,
        compact: bool,
        order: Literal["newest", "oldest"],
    ) -> dict[str, Any]:
        response, filters_applied = self._filter_logbook_by_search(response, search)

        paginated_entries, total_entries, has_more = self._paginate_logbook_entries(
            response, offset_int, effective_limit, order
        )

        # In compact mode, strip entries to essential fields only.
        # This prevents full attribute dictionaries from exhausting
        # the LLM context window during debugging workflows.
        if compact and isinstance(paginated_entries, list):
            paginated_entries = _compact_logbook_entries(paginated_entries)

        logbook_data: dict[str, Any] = {
            "success": True,
            "source": "logbook",
            "entries": paginated_entries,
            "period": f"{hours_back_int} hours back from {end_dt.isoformat()}",
            "start_time": start_timestamp,
            "end_time": end_dt.isoformat(),
            "entity_filter": entity_id,
            "device_filter": device_id,
            "context_filter": context_id,
            "total_entries": total_entries,
            "returned_entries": len(paginated_entries)
            if isinstance(paginated_entries, list)
            else 1,
            "limit": effective_limit,
            "offset": offset_int,
            "order": order,
            "has_more": has_more,
        }
        if filters_applied:
            logbook_data["filters_applied"] = filters_applied
        if has_more:
            logbook_data["pagination_hint"] = self._build_pagination_hint(
                offset_int,
                effective_limit,
                total_entries,
                paginated_entries,
                hours_back_int,
                end_dt.isoformat(),
                entity_id,
                device_id,
                context_id,
                search,
                compact,
                order,
            )

        return logbook_data
