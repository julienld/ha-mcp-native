"""HACS search and info read only the integration's loaded cache."""

from typing import Any

from ha_mcp.runtime.hacs_results import info_data, search_data
from ha_mcp.runtime.hacs_values import CATEGORY_MAP, _filter_and_score_repos
from ha_mcp.runtime.progress import report_progress
from ha_mcp.runtime.projection import timezone_metadata

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
            access.project(repo)
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
                    query, category, installed_only, max_results, offset, matches
                ),
                "cache": access.cache(),
            }
        )

    async def _hacs_info(self, repository_id: str) -> dict[str, Any]:
        access = await self.access()
        repository = access.find(repository_id)
        _payload, registration = access.validate(
            access.command("info"), {"repository_id": str(repository.data.id)}
        )
        before = repository_image(repository)
        data = access.project(repository)
        freshness = access.cache(repository)
        await access.observe(repository, before)
        access.pin_validator(access.command("info"), registration)
        return self.wrapped(
            {
                **info_data(repository_id, data),
                "cache": freshness,
                "note": "Cached store metadata only. Use ha_manage_hacs(action='update_information') to refresh GitHub metadata explicitly.",
            }
        )
