"""Detached overview projections shared by Core and standalone discovery."""

import random
from itertools import islice
from typing import Any

from ha_mcp.utils.device_registry_semantics import (
    build_device_registry_snapshot,
    effective_entity_area_id,
)

from .overview_values import _simplify_states_summary


class OverviewResultBuilder:
    def _build_overview_image(
        self,
        entities: list[dict[str, Any]],
        services: Any,
        area_registry: list[dict[str, Any]],
        entity_registry: list[dict[str, Any]],
        device_registry: list[dict[str, Any]],
        *,
        detail_level: str,
        max_entities_per_domain: int | None,
        include_state: bool | None,
        include_entity_id: bool | None,
        domains_filter: list[str] | None,
        limit: int | None,
        offset: int,
        partial_warnings: list[str],
        visibility_warnings: list[str],
    ) -> dict[str, Any]:
        entity_area_map = self._build_entity_area_map(entity_registry, device_registry)

        (
            max_entities_per_domain,
            uncap_all,
            include_state,
            include_entity_id,
        ) = self._resolve_overview_display_opts(
            detail_level, max_entities_per_domain, include_state, include_entity_id
        )

        # Pre-populate area_stats so empty areas still appear
        area_stats = self._init_area_stats(area_registry)

        domains_filter_set: set[str] | None = None
        if domains_filter:
            domains_filter_set = {d.strip().lower() for d in domains_filter}

        # Count all domains before filtering (for system_summary)
        all_domains = {e["entity_id"].split(".")[0] for e in entities}

        domain_stats, device_types = self._analyze_entities_by_domain(
            entities,
            domains_filter_set,
            area_stats,
            entity_area_map,
            include_state,
            include_entity_id,
        )

        sorted_domains = sorted(
            domain_stats.items(), key=lambda x: x[1]["count"], reverse=True
        )
        service_stats, total_services = self._build_service_stats(services)
        ai_insights = self._build_ai_insights(domain_stats, sorted_domains)
        formatted_domain_stats = self._format_domain_stats(
            sorted_domains, max_entities_per_domain, detail_level, uncap_all
        )
        pagination_metadata = self._paginate_overview_entities(
            formatted_domain_stats, limit, offset, detail_level
        )

        # totals reflect the full (visibility-filtered) system and are not
        # narrowed by the domains_filter display filter below
        system_summary: dict[str, Any] = {
            "total_entities": len(entities),
            "total_domains": len(all_domains),
            "total_services": total_services,
            "total_areas": len(area_registry),
        }
        if domains_filter_set:
            system_summary["filtered_domains"] = sorted(domains_filter_set)

        return self._assemble_overview_response(
            system_summary=system_summary,
            formatted_domain_stats=formatted_domain_stats,
            area_stats=area_stats,
            ai_insights=ai_insights,
            detail_level=detail_level,
            pagination_metadata=pagination_metadata,
            partial_warnings=partial_warnings,
            visibility_warnings=visibility_warnings,
            device_types=device_types,
            service_stats=service_stats,
        )

    @staticmethod
    def _build_entity_area_map(
        entity_registry: list[dict[str, Any]],
        device_registry: list[dict[str, Any]],
    ) -> dict[str, str | None]:
        """Map entity IDs to direct area, then device direct-or-parent effective area."""
        device_area_map = build_device_registry_snapshot(
            list(device_registry)
        ).effective_area_by_id

        entity_area_map: dict[str, str | None] = {}
        for entry in entity_registry:
            entity_id = entry.get("entity_id")
            if entity_id:
                entity_area_map[entity_id] = effective_entity_area_id(
                    entry, device_area_map
                )
        return entity_area_map

    @staticmethod
    def _resolve_overview_display_opts(
        detail_level: str,
        max_entities_per_domain: int | None,
        include_state: bool | None,
        include_entity_id: bool | None,
    ) -> tuple[int | None, bool, bool, bool]:
        """Resolve detail-level display defaults.

        ``max_entities_per_domain == 0`` means "uncap everything" (entities +
        states). standard/full keep no default cap (None = all entities).
        """
        uncap_all = max_entities_per_domain == 0
        if max_entities_per_domain is None and detail_level == "minimal":
            max_entities_per_domain = 10
        if include_state is None:
            include_state = detail_level == "full"
        if include_entity_id is None:
            include_entity_id = detail_level == "full"
        return max_entities_per_domain, uncap_all, include_state, include_entity_id

    @staticmethod
    def _init_area_stats(
        area_registry: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Pre-populate per-area stats so areas with no entities still appear."""
        area_stats: dict[str, dict[str, Any]] = {}
        for area in area_registry:
            area_id = area.get("area_id", "")
            if area_id:
                area_stats[area_id] = {
                    "name": area.get("name", area_id),
                    "count": 0,
                    "domains": {},
                }
        return area_stats

    @staticmethod
    def _record_entity_area(
        area_stats: dict[str, dict[str, Any]],
        entity_area_map: dict[str, str | None],
        entity_id: str,
        domain: str,
    ) -> None:
        """Increment per-area + per-area-domain counts for one entity."""
        area_id = entity_area_map.get(entity_id)
        if area_id and area_id in area_stats:
            area_stats[area_id]["count"] += 1
            domains = area_stats[area_id]["domains"]
            domains[domain] = domains.get(domain, 0) + 1

    @staticmethod
    def _record_device_type(
        device_types: dict[str, int], attributes: dict[str, Any]
    ) -> None:
        """Increment the device_class tally for one entity, if it has one."""
        device_class = attributes.get("device_class")
        if device_class:
            device_types[device_class] = device_types.get(device_class, 0) + 1

    def _analyze_entities_by_domain(
        self,
        entities: list[dict[str, Any]],
        domains_filter_set: set[str] | None,
        area_stats: dict[str, dict[str, Any]],
        entity_area_map: dict[str, str | None],
        include_state: bool,
        include_entity_id: bool,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
        """Tally per-domain stats, area stats (mutated in place), and device types."""
        domain_stats: dict[str, dict[str, Any]] = {}
        device_types: dict[str, int] = {}

        for entity in entities:
            entity_id = entity["entity_id"]
            domain = entity_id.split(".")[0]
            if domains_filter_set and domain not in domains_filter_set:
                continue

            attributes = entity.get("attributes", {})
            state = entity.get("state", "unknown")

            stats = domain_stats.setdefault(
                domain, {"count": 0, "states_summary": {}, "all_entities": []}
            )
            stats["count"] += 1
            stats["states_summary"][state] = stats["states_summary"].get(state, 0) + 1

            entity_data: dict[str, Any] = {
                "friendly_name": attributes.get("friendly_name", entity_id),
            }
            if include_entity_id:
                entity_data["entity_id"] = entity_id
            if include_state:
                entity_data["state"] = state
            stats["all_entities"].append(entity_data)

            self._record_entity_area(area_stats, entity_area_map, entity_id, domain)
            self._record_device_type(device_types, attributes)

        return domain_stats, device_types

    @staticmethod
    def _build_service_stats(
        services: Any,
    ) -> tuple[dict[str, dict[str, Any]], int]:
        """Summarize the service catalog into per-domain counts and a grand total."""
        service_stats: dict[str, dict[str, Any]] = {}
        total_services = 0
        if isinstance(services, list):
            for domain_obj in services:
                domain = domain_obj.get("domain", "unknown")
                domain_services = domain_obj.get("services", {})
                service_stats[domain] = {
                    "count": len(domain_services),
                    "services": list(domain_services.keys()),
                }
                total_services += len(domain_services)
        return service_stats, total_services

    @staticmethod
    def _build_ai_insights(
        domain_stats: dict[str, dict[str, Any]],
        sorted_domains: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        """Derive coarse AI-facing hints (common/controllable/monitoring domains)."""
        return {
            "most_common_domains": [domain for domain, _ in sorted_domains[:5]],
            "controllable_devices": [
                domain
                for domain in domain_stats
                if domain in ["light", "switch", "climate", "media_player", "cover"]
            ],
            "monitoring_sensors": [
                domain
                for domain in domain_stats
                if domain in ["sensor", "binary_sensor", "camera"]
            ],
            "automation_ready": "automation" in domain_stats
            and domain_stats["automation"]["count"] > 0,
        }

    @staticmethod
    def _format_domain_stats(
        sorted_domains: list[tuple[str, dict[str, Any]]],
        max_entities_per_domain: int | None,
        detail_level: str,
        uncap_all: bool,
    ) -> dict[str, dict[str, Any]]:
        """Apply the per-domain entity cap and simplify state summaries."""
        formatted_domain_stats: dict[str, dict[str, Any]] = {}
        for domain, stats in sorted_domains:
            all_entities = stats["all_entities"]
            if max_entities_per_domain and len(all_entities) > max_entities_per_domain:
                if detail_level == "minimal":
                    # Random sample so minimal isn't biased to early entities
                    selected_entities = random.sample(
                        all_entities, max_entities_per_domain
                    )
                else:
                    selected_entities = all_entities[:max_entities_per_domain]
                truncated = True
            else:
                selected_entities = all_entities
                truncated = False

            formatted_domain_stats[domain] = {
                "count": stats["count"],
                "states_summary": _simplify_states_summary(
                    stats["states_summary"],
                    "full" if uncap_all else detail_level,
                ),
                "entities": selected_entities,
                "truncated": truncated,
            }
        return formatted_domain_stats

    @staticmethod
    def _allocate_page(
        formatted_domain_stats: dict[str, dict[str, Any]],
        effective_limit: int,
        offset: int,
    ) -> int:
        """Take one stable round-robin page across domains.

        A single ordering must drive every page. Otherwise a specially balanced
        first page followed by sequential offset pages can repeat entities.

        Mutates ``formatted_domain_stats`` in place; returns the count included.
        """
        max_domain_size = max(
            (len(data["entities"]) for data in formatted_domain_stats.values()),
            default=0,
        )
        ordered = (
            (domain, data["entities"][index])
            for index in range(max_domain_size)
            for domain, data in formatted_domain_stats.items()
            if index < len(data["entities"])
        )

        selected: dict[str, list[dict[str, Any]]] = {
            domain: [] for domain in formatted_domain_stats
        }
        for domain, entity in islice(ordered, offset, offset + effective_limit):
            selected[domain].append(entity)

        for domain, data in formatted_domain_stats.items():
            original_count = len(data["entities"])
            data["entities"] = selected[domain]
            if len(data["entities"]) < original_count:
                data["truncated"] = True
        return sum(len(entities) for entities in selected.values())

    def _paginate_overview_entities(
        self,
        formatted_domain_stats: dict[str, dict[str, Any]],
        limit: int | None,
        offset: int,
        detail_level: str,
    ) -> dict[str, Any] | None:
        """Apply global entity pagination across domains; returns metadata or None.

        Default limit: None for minimal (already capped per-domain), 200 for
        standard/full. Domain counts/states_summary stay complete regardless.
        """
        effective_limit = limit
        if effective_limit is None and detail_level != "minimal":
            effective_limit = 200
        if effective_limit is None:
            return None

        total_entity_count = sum(
            len(ds["entities"]) for ds in formatted_domain_stats.values()
        )
        entities_included = self._allocate_page(
            formatted_domain_stats, effective_limit, offset
        )

        has_more = (offset + entities_included) < total_entity_count
        return {
            "total_entity_results": total_entity_count,
            "offset": offset,
            "limit": effective_limit,
            "entities_returned": entities_included,
            "has_more": has_more,
            "next_offset": offset + effective_limit if has_more else None,
        }

    @staticmethod
    def _assemble_overview_response(
        *,
        system_summary: dict[str, Any],
        formatted_domain_stats: dict[str, dict[str, Any]],
        area_stats: dict[str, dict[str, Any]],
        ai_insights: dict[str, Any],
        detail_level: str,
        pagination_metadata: dict[str, Any] | None,
        partial_warnings: list[str],
        visibility_warnings: list[str],
        device_types: dict[str, int],
        service_stats: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Assemble the final overview response and attach level-specific fields."""
        base_response: dict[str, Any] = {
            "success": True,
            "system_summary": system_summary,
            "domain_stats": formatted_domain_stats,
            "area_analysis": (
                {area: {"count": info["count"]} for area, info in area_stats.items()}
                if detail_level == "minimal"
                else area_stats
            ),
            "ai_insights": ai_insights,
        }

        if pagination_metadata:
            base_response["pagination"] = pagination_metadata

        # Only genuinely incomplete data (e.g. a failed services fetch) marks the
        # response partial; visibility warnings annotate a complete result. Both
        # surface in `warnings`.
        if partial_warnings:
            base_response["partial"] = True
        all_warnings = partial_warnings + visibility_warnings
        if all_warnings:
            base_response["warnings"] = all_warnings

        # Full: add device types and service catalog
        if detail_level == "full":
            base_response["device_types"] = device_types
            base_response["service_availability"] = service_stats

        return base_response
