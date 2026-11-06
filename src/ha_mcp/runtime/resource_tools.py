"""One public resource contract, bound to a native or legacy domain service."""

from __future__ import annotations

import builtins
from typing import Annotated, Any, Protocol

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION

from .resource_edits import MAX_EDITS, ResourceEdit

_RESOURCE_TYPE = str


class Resources(Protocol):
    async def list(
        self,
        include_content: bool = False,
        limit: int = 100,
        offset: int = 0,
        include_hash: bool = False,
    ) -> dict[str, Any]: ...
    async def save(
        self,
        content: str | None = None,
        url: str | None = None,
        resource_type: str = "module",
        resource_id: str | None = None,
        edits: builtins.list[ResourceEdit] | None = None,
        config_hash: str | None = None,
    ) -> dict[str, Any]: ...
    async def delete(self, resource_id: str) -> dict[str, Any]: ...


class ResourceTools:
    def __init__(self, resources: Resources) -> None:
        self.resources = resources

    async def ha_config_list_dashboard_resources(
        self,
        include_content: Annotated[
            bool,
            Field(
                description="Include full decoded content for inline resources in the \"_content\" field. When False, a 150-char preview is shown instead. Rows past the per-response content budget carry '_content_truncated': True instead of '_content'; request a smaller page before copying content into an update."
            ),
        ] = False,
        include_hash: Annotated[
            bool,
            Field(
                description='Add "_config_hash" (SHA-256 hex digest of the decoded UTF-8 content) to each inline resource, e.g. to verify an edit or to pass as config_hash without fetching the content.'
            ),
        ] = False,
        limit: Annotated[
            int,
            Field(
                default=100,
                ge=1,
                le=500,
                description="Max resources to return per page",
            ),
        ] = 100,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of resources to skip for pagination",
            ),
        ] = 0,
    ) -> dict[str, Any]:
        """List Lovelace dashboard resources (custom cards, themes, CSS/JS).

        Returns one page of registered resources; `total_count` and `has_more`
        report the full set, and `inline_count` and `by_type` summarise every
        resource, not just this page. Each resource has a unique ID for
        update/delete operations. For inline resources (created with
        ha_config_set_dashboard_resource(content=...)), shows a preview of the
        content instead of the full encoded URL to save tokens.
        `resource_mode` is Core's lovelace resource mode; in 'yaml' mode
        resources cannot be created, updated or deleted through the API.

        EXAMPLES:
        - Next page: ha_config_list_dashboard_resources(offset=100)
        - List with full content: ha_config_list_dashboard_resources(include_content=True)
        - Verify an edit by hash: ha_config_list_dashboard_resources(include_hash=True)

        Home Assistant 2026.6+ exposes resource management in the UI by default;
        API access works regardless of UI availability.
        """
        return await self.resources.list(
            include_content=include_content,
            include_hash=include_hash,
            limit=limit,
            offset=offset,
        )

    async def ha_config_set_dashboard_resource(
        self,
        content: Annotated[
            str | None, Field(description="JavaScript or CSS code to host inline.")
        ] = None,
        url: Annotated[str | None, Field(description="URL of the resource.")] = None,
        resource_type: Annotated[
            _RESOURCE_TYPE,
            Field(
                description="Resource type, validated by Home Assistant: 'module' for ES6 modules (modern cards), 'js' for legacy JavaScript (older custom cards), 'css' for stylesheets (themes, global styles), 'html' for HTML imports"
            ),
        ] = "module",
        resource_id: Annotated[
            str | None,
            Field(
                description="Resource ID to update. If omitted, creates a new resource. Get IDs from ha_config_list_dashboard_resources()"
            ),
        ] = None,
        edits: Annotated[
            Annotated[list[ResourceEdit], Field(min_length=1, max_length=MAX_EDITS)]
            | None,
            JSON_STRING_COERCION,
            Field(
                description="Search/replace edits for an existing inline resource, applied in order. Requires resource_id; resource_type is ignored and the stored type is kept."
            ),
        ] = None,
        config_hash: Annotated[
            str | None,
            Field(
                description="With edits: refuse the patch unless the stored content has this hash when read, just before the write (the list's _config_hash with include_hash=True, or a previous write response's config_hash). A preflight check only: a change made by another client after that read is not detected and can be overwritten."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create or update a dashboard resource (inline code or external URL).

        Provide exactly one of:
        - edits: Search/replace edits on an existing inline resource. Any
          failing edit (or a config_hash mismatch) aborts the whole call
          and saves nothing. Prefer this over content= for small changes to a
          large card: only the changed text is sent.
        - content: Inline JavaScript or CSS code (embedded in the resource URL
          as a data: URI — no file storage or external hosting involved).
          Supports 'module' and 'css' types only; URLs are
          deterministic (same content = same URL); up to ~128KB. Content must be
          self-contained: a data: URI has no base URL, so relative imports inside
          a module and relative url() references inside CSS cannot resolve (use
          fully-qualified URLs instead). If Home Assistant is behind a reverse
          proxy that injects a Content-Security-Policy without 'data:' in
          script-src/style-src, the browser blocks these resources: this call
          still succeeds and the card simply never renders. Register the code as
          a file and use url='/local/...' on such a deployment. (HA itself ships
          no CSP.)
        - url: External resource URL — /local/... (/config/www/; Marketplace
          cards: /local/community/...), /hacsfiles/... (HACS cards) or
          https://... (CDN). Supports all types: 'module', 'js', 'css', 'html'.

        EXAMPLES:
        - Inline: ha_config_set_dashboard_resource(content="class MyCard extends HTMLElement { ... } customElements.define('my-card', MyCard);", resource_type="module")
        - Store cards need no call: in storage mode HACS and the Marketplace register the resource on install (adding it again duplicates it); with YAML-mode resources, list it under lovelace: resources: in configuration.yaml instead.
        - Update: ha_config_set_dashboard_resource(url="/local/my-card-v2.js", resource_type="module", resource_id="abc123")
        - Patch: ha_config_set_dashboard_resource(resource_id="abc123", edits=[{"old": "color: red", "new": "color: blue"}], config_hash="<_config_hash from the list>")

        Inline writes return the stored content's config_hash.

        After adding a resource, a browser cache clear or hard refresh
        (Ctrl+Shift+R) is needed to load it.
        """
        return await self.resources.save(
            content=content,
            url=url,
            resource_type=resource_type,
            resource_id=resource_id,
            edits=edits,
            config_hash=config_hash,
        )

    async def ha_config_delete_dashboard_resource(
        self,
        resource_id: Annotated[
            str,
            Field(description="Resource ID to delete."),
        ],
    ) -> dict[str, Any]:
        """
        Delete a dashboard resource.

        Removes a resource from Home Assistant. The resource will no longer
        be loaded on dashboards.

        WARNING: Deleting a resource used by custom cards in your dashboards
        will cause those cards to fail to load.

        EXAMPLES:
        ha_config_delete_dashboard_resource(resource_id="abc123")

        Note: Use ha_config_list_dashboard_resources() to find resource IDs
        before deleting. Ensure no dashboards depend on the resource.
        """
        return await self.resources.delete(resource_id=resource_id)
