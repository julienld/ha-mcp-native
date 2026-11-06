"""AI-friendly system overview with intelligent categorization."""

import asyncio
import logging
from typing import Any

from ha_mcp.runtime.overview_results import OverviewResultBuilder

from ...visibility.resolver import load_hidden_set
from ..helpers import exception_to_structured_error
from ._base import _SearchBase

logger = logging.getLogger(__name__)


class SystemOverviewMixin(OverviewResultBuilder, _SearchBase):
    """``get_system_overview`` and its analysis/format/paginate helpers."""

    async def _fetch_legacy_overview_slices(self) -> list[Any]:
        """Fetch broad legacy slices in parallel after states succeeds."""
        states = await self.client.get_states()
        optional_results = await asyncio.gather(
            self.client.get_services(),
            self.client.send_websocket_message({"type": "config/area_registry/list"}),
            self.client.send_websocket_message({"type": "config/entity_registry/list"}),
            self.client.send_websocket_message({"type": "config/device_registry/list"}),
            return_exceptions=True,
        )
        return [states, *optional_results]

    async def get_system_overview(
        self,
        detail_level: str = "standard",
        max_entities_per_domain: int | None = None,
        include_state: bool | None = None,
        include_entity_id: bool | None = None,
        domains_filter: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        prefetched_slices: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Get AI-friendly system overview with intelligent categorization.

        Args:
            detail_level: Level of detail to return:
                - "minimal": 10 entities/domain sample, top-5 states (friendly_name only)
                - "standard": ALL entities, top-10 states (friendly_name only)
                - "full": ALL entities with entity_id + friendly_name + state + full states
            max_entities_per_domain: Override default entity cap (0 = no limit)
            include_state: Override whether to include state field
            include_entity_id: Override whether to include entity_id field
            domains_filter: Only include these domains (None = all)
            limit: Max total entities to include across all domains.
                Defaults to None (no limit) for minimal, 200 for standard/full.
                Domain counts and states_summary are always complete regardless.
            offset: Number of entities to skip for pagination (default: 0)
            prefetched_slices: The five overview reads (``states``, ``services``,
                ``area_registry``, ``entity_registry``, ``device_registry``)
                already fetched by a caller — the ``ha_mcp_tools/overview``
                component collapses them into one round-trip. When given, the
                parallel gather is skipped and these are used verbatim (bare
                ``states``/``services`` lists; the three registries in the
                ``{success, result}`` envelope ``_extract_registry_list`` /
                ``load_hidden_set`` unwrap). ``None`` (default) fetches them
                itself, mirroring the ``prefetched_states``/``prefetched_registry``
                threading used by the search paths.

        Returns:
            System overview optimized for AI understanding at requested detail level
        """
        try:
            if prefetched_slices is not None:
                # Component raw-slice path: the reads were already returned in
                # one WS round-trip, so use them instead of fetching. Downstream
                # unwrapping is unchanged — states/services are bare lists and the
                # registries carry the {success, result} envelope.
                results: list[Any] = [
                    prefetched_slices["states"],
                    prefetched_slices["services"],
                    prefetched_slices["area_registry"],
                    prefetched_slices["entity_registry"],
                    prefetched_slices["device_registry"],
                ]
            else:
                # Fetch mandatory states first, then read optional slices in
                # parallel; optional failures degrade independently below.
                results = await self._fetch_legacy_overview_slices()

            entities = results[0]
            # A cancelled services/registry sub-task must propagate too, not be
            # silently degraded by the fail-open handlers below.
            for sub_result in results[1:]:
                if isinstance(sub_result, asyncio.CancelledError):
                    raise sub_result

            # Opt-in visibility filter: drop out-of-scope entities from the whole
            # overview universe before any domain stats/samples are computed, so
            # every count stays coherent. results[3] is the raw entity registry,
            # results[4] the device registry (so the area/label dimensions match a
            # device-bound entity by its device's area/labels).
            # Fails open (empty set on any error); do NOT wrap in try/except.
            visibility_hidden, visibility_warnings = await load_hidden_set(
                results[3], results[0], self.client, results[4]
            )
            if visibility_hidden:
                entities = [
                    e for e in entities if e.get("entity_id") not in visibility_hidden
                ]

            # Services failure means the total count + catalog are genuinely
            # incomplete, so it sets `partial`. Visibility warnings are kept
            # separate: they annotate an otherwise-complete result (a config typo
            # or a skipped Assist dimension), so they must surface in `warnings`
            # without flipping `partial` — aligning with ha_search, which reports
            # the same visibility warnings and never marks itself partial for them.
            partial_warnings: list[str] = []
            if isinstance(results[1], Exception):
                logger.warning(f"Could not fetch services: {results[1]}")
                partial_warnings.append(f"Services unavailable: {results[1]}")
                services: Any = []
            else:
                services = results[1]

            # Registry failures degrade area enrichment only, so they keep the
            # overview rather than failing it — but each one names itself in
            # `partial_warnings`, since an overview whose area stats are thin
            # because a registry never arrived is otherwise indistinguishable
            # from one whose areas really are empty (#1947).
            area_registry = self._extract_registry_list(
                results[2], "area registry", partial_warnings
            )
            entity_registry = self._extract_registry_list(
                results[3], "entity registry", partial_warnings
            )
            device_registry = self._extract_registry_list(
                results[4], "device registry", partial_warnings
            )
            return self._build_overview_image(
                entities,
                services,
                area_registry,
                entity_registry,
                device_registry,
                detail_level=detail_level,
                max_entities_per_domain=max_entities_per_domain,
                include_state=include_state,
                include_entity_id=include_entity_id,
                domains_filter=domains_filter,
                limit=limit,
                offset=offset,
                partial_warnings=partial_warnings,
                visibility_warnings=visibility_warnings,
            )

        except Exception as e:
            logger.error(f"Error in get_system_overview: {e}")
            exception_to_structured_error(
                e,
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify API token permissions",
                    "Try test_connection first",
                ],
                context={
                    "total_entities": 0,
                    "entity_summary": {},
                    "controllable_devices": {},
                },
            )
            # ``exception_to_structured_error`` always raises (NoReturn); the
            # explicit re-raise makes the exit unambiguous and matches the
            # sibling handlers in get_entities_by_area / _fetch_search_entities.
            raise
