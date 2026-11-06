"""Store search and normal info refresh, with opt-in release reads."""

from typing import Any

from ha_mcp.runtime.hacs_results import info_data, search_data
from ha_mcp.runtime.hacs_values import CATEGORY_MAP, _filter_and_score_repos
from ha_mcp.runtime.progress import report_progress
from ha_mcp.runtime.projection import timezone_metadata
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .hacs_access import HacsAccess, repository_image
from .registry_access import registry_json


class HacsReads:
    manager: Any
    actor: Any

    def wrapped(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            **timezone_metadata(
                registry_json(data), self.manager.hass.config.time_zone
            ),
        }

    async def access(self, *, writes: bool = False) -> HacsAccess:
        from .marketplace_access import MarketplaceAccess, store_domain

        access_type = (
            MarketplaceAccess
            if store_domain(self.manager.hass) == "marketplace"
            else HacsAccess
        )
        return await access_type.open(
            self.manager.hass, self.actor, self.manager.backend.authorize, writes=writes
        )

    def store_metadata(self, access: HacsAccess) -> dict[str, Any]:
        """Project the selected owner's read-only HACS/Marketplace info fields."""
        # Mirrors websocket/__init__.py::hacs_info (HACS 2.0.5) and
        # ::marketplace_info (Core 7f6df9f7), without a synthetic WS connection.
        owner = access.owner
        result = {
            "api": access.domain,
            "categories": list(owner.common.categories),
            "disabled_reason": owner.system.disabled_reason,
            "has_pending_tasks": owner.queue.has_pending_tasks,
            "lovelace_mode": owner.core.lovelace_mode,
            "stage": owner.stage,
            "startup": owner.status.startup,
            "version": owner.version,
        }
        if access.domain == "marketplace":
            result.update(
                github_connected=owner.github_connected,
                warning_accepted=bool(
                    self.actor.user_id and owner.warning_accepted(self.actor.user_id)
                ),
            )
        else:
            result.update(
                country=owner.configuration.country,
                debug=owner.configuration.debug,
                dev=owner.configuration.dev,
            )
        return result

    async def _hacs_search(
        self,
        query: str,
        category: str | None,
        installed_only: bool,
        max_results: int,
        offset: int,
        ctx: Any,
    ) -> dict[str, Any]:
        await report_progress(
            self.actor.progress,
            progress=0,
            total=3,
            message="checking HACS availability",
        )
        access = await self.access()
        access.category(category)
        await report_progress(
            self.actor.progress,
            progress=1,
            total=3,
            message="reading the loaded HACS repository cache",
        )
        categories = [CATEGORY_MAP.get(category, category)] if category else None
        payload, registration = access.validate(
            access.command("search"),
            {"categories": categories} if categories is not None else {},
        )
        await access.observe()
        rows = [
            {
                key: value
                for key, value in access.project(repo).items()
                if key not in {"readme", "additional_info"}
            }
            for repo in access.repositories.list_all
            if repo.data.category
            in payload.get("categories", access.owner.common.categories)
            and access.listed(repo)
            and repo.data.last_fetched
        ]
        await report_progress(
            self.actor.progress,
            progress=2,
            total=3,
            message=f"filtering {len(rows)} cached repositories",
        )
        matches = _filter_and_score_repos(rows, query, installed_only)
        await report_progress(
            self.actor.progress,
            progress=3,
            total=3,
            message=f"matched {len(matches)} repositories",
        )
        await access.observe()
        access.pin_validator(access.command("search"), registration)
        return self.wrapped(
            {
                **search_data(
                    query,
                    category,
                    installed_only,
                    max_results,
                    offset,
                    matches,
                    store=self.store_metadata(access),
                ),
                "cache": access.cache(),
            }
        )

    async def _hacs_info(
        self, repository_id: str, include_releases: bool = False
    ) -> dict[str, Any]:
        access = await self.access()
        repository = access.find(repository_id)
        _payload, registration = access.validate(
            access.command("info"), {"repository_id": str(repository.data.id)}
        )
        from .hacs_info import refresh_info

        async with registry_update_lock("hacs", "repositories"):
            refreshed, warnings = await await_owned_io(
                refresh_info(access, repository, registration)
            )
            before = repository_image(repository)
            data = access.project(repository)
            freshness = access.cache(repository)
            freshness["refreshed_from_github"] = refreshed
        await access.observe(repository, before)
        access.pin_validator(access.command("info"), registration)
        extra: dict[str, Any] = {}
        if include_releases:
            from .hacs_releases import release_details

            extra = await release_details(access, repository, before)
        return self.wrapped(
            {
                **info_data(repository_id, data),
                "cache": freshness,
                "note": "Repository details follow the store info refresh policy; no installation is performed.",
                **(
                    {"warnings": [*warnings, *extra.pop("warnings", [])]}
                    if warnings
                    else {}
                ),
                **extra,
            }
        )
