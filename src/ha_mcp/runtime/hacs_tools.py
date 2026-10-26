"""The HACS public contracts shared by direct owners and the remote adapter."""

from typing import Annotated, Any, Literal, Protocol

from pydantic import Field

from .hacs_values import _reject_foreign_params
from .validation import validate_identifier_not_empty


class HacsService(Protocol):
    async def _hacs_search(
        self,
        query: str,
        category: str | None,
        installed_only: bool,
        max_results: int,
        offset: int,
        ctx: Any,
    ) -> dict[str, Any]: ...
    async def _hacs_info(self, repository_id: str) -> dict[str, Any]: ...
    async def _hacs_download(
        self, repository_id: str | None, version: str | None
    ) -> dict[str, Any]: ...
    async def _hacs_remove(self, repository_id: str | None) -> dict[str, Any]: ...
    async def _hacs_update_information(
        self, repository_id: str | None
    ) -> dict[str, Any]: ...
    async def _hacs_add_repository(
        self, repository: str, category: str
    ) -> dict[str, Any]: ...


class HacsFacade:
    def __init__(self, hacs: HacsService) -> None:
        self.hacs = hacs

    async def ha_get_hacs_info(
        self,
        action: Annotated[
            Literal["search", "info"],
            Field(description="'search' the store, or 'info' for one repository"),
        ],
        query: Annotated[
            str, Field(description="Search keyword (action='search')")
        ] = "",
        category: Annotated[
            Literal["integration", "lovelace", "theme", "appdaemon", "python_script"]
            | None,
            Field(description="Filter by category (action='search')"),
        ] = None,
        installed_only: Annotated[
            bool,
            Field(description="Only return installed repositories (action='search')"),
        ] = False,
        max_results: Annotated[
            int,
            Field(
                ge=1,
                le=100,
                description="Maximum number of results (action='search')",
            ),
        ] = 10,
        offset: Annotated[
            int,
            Field(
                ge=0,
                description="Results to skip for pagination (action='search')",
            ),
        ] = 0,
        repository_id: Annotated[
            str | None,
            Field(description="Numeric HACS ID or 'owner/repo' path (action='info')"),
        ] = None,
        ctx: Any = None,
    ) -> dict[str, Any]:
        """Get cached HACS or Home Assistant Marketplace data — search the store or read repository details.

        Use ``action="search"`` to search/browse/list store repositories, or
        ``action="info"`` for one repository's full details (README, versions, GitHub
        stats). This tool is read-only; to install or add repositories use
        ``ha_manage_hacs``, and for non-HACS entities/config use the domain-specific tools.

        **DASHBOARD TIP:** ``action="search", installed_only=True, category="lovelace"``
        discovers installed custom cards to wire into ``ha_config_set_dashboard()``.

        **Examples:**
        - Search the store: ha_get_hacs_info(action="search", query="mushroom", category="lovelace")
        - List installed: ha_get_hacs_info(action="search", installed_only=True)
        - Repository details: ha_get_hacs_info(action="info", repository_id="441028036")

        **Caveats:** ``info`` reads only the already-loaded store cache,
        including cached README/additional information when available, and reports
        freshness. It never refreshes GitHub or clears HACS's new flag. Use
        ha_manage_hacs(action="update_information") to refresh explicitly. The
        remote path needs the component cache capability for full details; without
        it, cached catalog fields are returned with an explicit completeness note.
        Marketplace no longer supports appdaemon or python_script repositories.
        """
        if action == "search":
            return await self.hacs._hacs_search(
                query, category, installed_only, max_results, offset, ctx
            )

        # action == "info"
        repository_id = validate_identifier_not_empty(
            repository_id,
            "repository_id",
            message="repository_id is required for action='info'",
            suggestions=[
                "Pass repository_id (numeric HACS ID or 'owner/repo')",
                "Use action='search' to find the repository first",
            ],
        )
        return await self.hacs._hacs_info(repository_id)

    async def ha_manage_hacs(
        self,
        action: Annotated[
            Literal["download", "add_repository", "remove", "update_information"],
            Field(
                description=(
                    "'download' to install/update, 'add_repository' to register "
                    "a custom repo, 'remove' to uninstall a downloaded repo, or "
                    "'update_information' to refresh a repository's release data "
                    "from GitHub"
                )
            ),
        ],
        repository_id: Annotated[
            str | None,
            Field(
                description=(
                    "Numeric HACS ID or 'owner/repo' path "
                    "(action='download' / 'remove' / 'update_information')"
                )
            ),
        ] = None,
        version: Annotated[
            str | None,
            Field(description="Specific version to install (action='download')"),
        ] = None,
        repository: Annotated[
            str | None,
            Field(
                description="GitHub repo 'owner/repo' to add (action='add_repository')"
            ),
        ] = None,
        category: Annotated[
            Literal["integration", "lovelace", "theme", "appdaemon", "python_script"]
            | None,
            Field(description="Repository category (action='add_repository')"),
        ] = None,
    ) -> dict[str, Any]:
        """Manage HACS or Home Assistant Marketplace — install/update, remove, add repositories, or refresh metadata.

        This tool performs writes; to search the store or read repository details use
        ``ha_get_hacs_info``. Use ``action="update_information"`` to run the HACS UI's
        "Update information" action — a forced re-fetch of one repository's release data
        from GitHub, so a pending update becomes visible to HACS and its update entity
        immediately.

        **Examples:**
        - Install latest: ha_manage_hacs(action="download", repository_id="441028036")
        - Install a version: ha_manage_hacs(action="download", repository_id="piitaya/lovelace-mushroom", version="v4.0.0")
        - Remove: ha_manage_hacs(action="remove", repository_id="owner/repo")
        - Add a custom repo: ha_manage_hacs(action="add_repository", repository="owner/repo", category="lovelace")
        - Refresh release data: ha_manage_hacs(action="update_information", repository_id="owner/repo")

        **Caveats:** Installing an integration usually needs a Home Assistant restart to
        activate; new Lovelace cards need a browser cache clear. ``add_repository`` requires ``owner/repo``
        format plus a matching ``category``. Removing an integration deletes its files but
        the loaded module persists until the next Home Assistant restart — delete its config
        entries first (``ha_remove_helpers_integrations``). HACS refreshes custom
        repositories on its own only about every 48 hours, so ``update_information`` is the
        way to surface a just-published release. Native self-update/removal changes
        files while the existing runtime remains loaded; the response reports
        pending_restart and never claims that a new runtime is active. No automatic
        restart or config-entry removal is performed by these tools.
        Marketplace requires this caller's warning acceptance in its UI before
        installation, and a connected GitHub account to add custom repositories.
        The tool never accepts the warning on the caller's behalf.
        """
        # Reject parameters that don't belong to the chosen action rather
        # than silently discarding them — ha_manage_hacs(action="remove",
        # version="v4.0.0") plausibly means "uninstall this version",
        # which remove cannot honor, and dropping it would remove the
        # whole repository while reporting plain success.
        _reject_foreign_params(
            action,
            download={"repository": repository, "category": category},
            remove={
                "version": version,
                "repository": repository,
                "category": category,
            },
            add_repository={"repository_id": repository_id, "version": version},
            update_information={
                "version": version,
                "repository": repository,
                "category": category,
            },
        )

        if action == "download":
            return await self.hacs._hacs_download(repository_id, version)

        if action == "remove":
            return await self.hacs._hacs_remove(repository_id)

        if action == "update_information":
            return await self.hacs._hacs_update_information(repository_id)

        # action == "add_repository"
        repository = validate_identifier_not_empty(
            repository,
            "repository",
            suggestions=["Pass repository in 'owner/repo' format"],
        )
        # ``category`` is a Literal param, so bind the validated value to a
        # new ``str`` name rather than reassigning (str is wider than the Literal).
        valid_category = validate_identifier_not_empty(
            category,
            "category",
            suggestions=[
                "Pass category (integration, lovelace, theme, appdaemon, python_script)"
            ],
        )
        return await self.hacs._hacs_add_repository(repository, valid_category)
