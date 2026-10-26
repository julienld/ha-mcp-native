"""App inventories use Core's actual Supervisor session and shared projections."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

from ha_mcp.runtime.app_results import (
    app_info_result,
    available_apps_result,
    installed_apps_result,
)
from ha_mcp.runtime.app_values import _running_addon_slugs
from ha_mcp.tool_api import CallContext, ToolFailure

from ..native_server_policy import setting_value
from .registry_access import registry_failure
from .supervisor_access import SupervisorAccess


@dataclass(slots=True)
class NativeAppReads:
    manager: Any
    actor: CallContext
    supervisor: SupervisorAccess = field(init=False)

    def __post_init__(self) -> None:
        self.supervisor = SupervisorAccess(self.manager, self.actor)

    def _info(self, data: dict[str, Any], slug: str) -> dict[str, Any]:
        return app_info_result(
            data,
            slug,
            redact=self.manager.policy.flag("redact_secrets"),
            option_keys=str(
                setting_value(self.manager.policy.flags, "redact_app_option_keys") or ""
            ),
        )

    async def get_addon(
        self,
        source: Literal["installed", "available"] | None,
        slug: str | None,
        include_stats: bool,
        repository: str | None,
        query: str | None,
    ) -> dict[str, Any]:
        try:
            await self.supervisor.authorize()
            if slug:
                result = self._info(await self.supervisor.app_info(slug), slug)
            elif source == "available":
                data = await self.supervisor.store()
                rows, warnings = self._redact_rows(data.get("addons", []))
                result = available_apps_result(
                    {**data, "addons": rows},
                    repository,
                    query,
                    preserve_native=True,
                )
                if warnings:
                    result["warnings"] = warnings
            else:
                result = await self._installed(include_stats)
            await self.supervisor.authorize()
            return result
        except Exception as error:
            raise registry_failure(error, {"slug": slug, "source": source}) from error

    async def _installed(self, include_stats: bool) -> dict[str, Any]:
        data = await self.supervisor.installed()
        rows = data.get("addons", [])
        stats: dict[str, Any] = {}
        warnings: list[str] = []
        if include_stats:
            semaphore = asyncio.Semaphore(4)

            async def fetch(slug: str) -> None:
                async with semaphore:
                    try:
                        stats[slug] = await self.supervisor.app_stats(slug)
                    except ToolFailure as error:
                        if error.code in {"AUTH_FORBIDDEN", "CONFIG_CONFLICT"}:
                            raise
                        stats[slug] = None
                        warnings.append(
                            f"Statistics unavailable for app {slug!r}: {error.code}"
                        )

            outcomes = await asyncio.gather(
                *(fetch(slug) for slug in _running_addon_slugs(rows)),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
        # A future Supervisor may include options in this inventory. Preserve
        # them only through the same schema-based redaction as the detail path.
        rows, redaction_warnings = self._redact_rows(rows)
        warnings.extend(redaction_warnings)
        return installed_apps_result(
            rows, include_stats, stats, warnings, preserve_native=True
        )

    def _redact_rows(
        self, rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        rendered = [self._info(row, str(row.get("slug", ""))) for row in rows]
        return [result["addon"] for result in rendered], [
            warning for result in rendered for warning in result.get("warnings", [])
        ]
