"""Private adapter to the loaded Core Marketplace, not a stable public API.

Reviewed 2026-10-26 UTC against Core dev 7f6df9f7c69c2158ca7709ce1d3c457b243d7c72:
marketplace/base.py, websocket/decorators.py, repository.py, repositories.py,
repositories/base.py and update.py. Released stable at review: 2026.10.4.
Use the loaded validators/owners; never instantiate a manager or WS connection.
"""

import copy
from importlib import import_module
from typing import Any

from homeassistant.config_entries import ConfigEntryState

from ha_mcp.tool_api import ToolFailure

from .hacs_access import HacsAccess


def store_domain(hass: Any) -> str:
    domains = {"hacs", "marketplace"}.intersection(hass.config.components)
    if len(domains) > 1:
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "Both HACS and Marketplace are loaded; resolve their ownership before using store tools.",
            status="not_started",
        )
    return "marketplace" if "marketplace" in domains else "hacs"


class MarketplaceAccess(HacsAccess):
    domain = "marketplace"
    module_root = "homeassistant.components.marketplace"

    @staticmethod
    def get_owner(hass: Any, *, writes: bool = False) -> Any:
        try:
            return MarketplaceAccess._loaded_owner(hass)
        except (ImportError, AttributeError) as error:
            raise ToolFailure(
                "UNSUPPORTED_OPERATION",
                "The loaded Marketplace private owner contract is unavailable or has changed; this adapter cannot safely perform the operation.",
                status="not_started",
            ) from error

    @staticmethod
    def _loaded_owner(hass: Any) -> Any:
        if store_domain(hass) != "marketplace":
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Marketplace owner is no longer loaded",
                status="not_started",
            )
        entries = hass.config_entries.async_loaded_entries("marketplace")
        if len(entries) != 1:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Expected one loaded Marketplace owner",
                status="not_started",
            )
        entry = entries[0]
        owner = import_module(
            "homeassistant.components.marketplace.base"
        ).async_get_marketplace(hass)
        if (
            owner is None
            or owner is not entry.runtime_data
            or owner.configuration.config_entry is not entry
            or hass.config_entries.async_get_entry(entry.entry_id) is not entry
            or entry.state is not ConfigEntryState.LOADED
            or owner.status.startup
            or owner.system.removed
            or owner.data is None
        ):
            raise ToolFailure(
                "SERVICE_UNAVAILABLE",
                "Marketplace owner is not ready",
                status="not_started",
            )
        return owner

    def guard(self, action: str, repository: Any = None) -> None:
        # These are the guards of the reviewed WS decorators, not inferred consent.
        if action in {"download", "add_repository"} and (
            not self.actor.user_id
            or not self.owner.warning_accepted(self.actor.user_id)
        ):
            raise ToolFailure(
                "AUTH_FORBIDDEN",
                "Marketplace warning_not_accepted: this administrator must accept the warning in the Marketplace UI.",
                status="not_started",
            )
        if action == "add_repository" and not self.owner.github_connected:
            raise ToolFailure(
                "AUTH_FORBIDDEN",
                "Marketplace github_not_connected: connect GitHub in the Marketplace UI before adding custom repositories.",
                status="not_started",
            )
        if repository is not None:
            self.category(repository.data.category)
            if (
                action == "remove"
                and repository.data.category == "integration"
                and repository.data.domain
                and self.hass.config_entries.async_entries(repository.data.domain)
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "Marketplace repository_in_use: remove this integration's config entries before uninstalling its files.",
                    status="not_started",
                )
            if (
                action == "remove"
                and repository.data.category == "theme"
                and not repository.data.file_name
            ):
                raise ToolFailure(
                    "UNSUPPORTED_OPERATION",
                    "The cached theme has no file name. Refresh its information before removal; the adapter cannot safely select its files from this cache.",
                    status="not_started",
                )

    def category(self, category: str | None) -> None:
        if category in {"appdaemon", "python_script"}:
            raise ToolFailure(
                "UNSUPPORTED_OPERATION",
                f"Marketplace no longer supports {category}. Manage that content outside Marketplace; this tool does not move or install it elsewhere.",
                status="not_started",
            )

    def command(self, action: str) -> str:
        command = super().command(action)
        return command.replace("/download", "/install").replace("/remove", "/uninstall")

    def listed(self, repository: Any) -> bool:
        return True

    def project(self, repository: Any) -> dict[str, Any]:
        summary = import_module(
            "homeassistant.components.marketplace.websocket.repositories"
        ).repository_summary
        # Core owns the catalog fields. Info adds only already-cached data; its
        # WS handler fetches GitHub, clears new, and asks about built-in replacement.
        return copy.deepcopy(
            {
                **repository.data.to_json(),
                **summary(self.owner, repository),
                "additional_info": repository.additional_info,
                "readme": repository.additional_info,
                "releases": repository.data.published_tags,
                "ref": repository.ref,
                "version_or_commit": repository.display_version_or_commit,
                "hide_default_branch": repository.repository_manifest.hide_default_branch,
                "pending_restart": repository.pending_restart,
            }
        )

    def cache(self, repository: Any = None) -> dict[str, Any]:
        result = {
            "source": "marketplace_cache",
            "core_version": self.version,
            "refreshed_from_github": False,
            "github_connected": self.owner.github_connected,
            "warning_accepted": bool(
                self.actor.user_id and self.owner.warning_accepted(self.actor.user_id)
            ),
        }
        if repository is not None:
            result.update(
                last_fetched=repository.data.last_fetched.isoformat()
                if repository.data.last_fetched
                else None,
                readme_available=bool(repository.additional_info),
            )
        return result
