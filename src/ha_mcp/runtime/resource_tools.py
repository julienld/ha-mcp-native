"""One public resource contract, bound to a native or legacy domain service."""

from typing import Annotated, Any, Protocol

from pydantic import Field

_RESOURCE_TYPE = str


class Resources(Protocol):
    async def list(
        self, include_content: bool = False, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]: ...
    async def save(
        self,
        content: str | None = None,
        url: str | None = None,
        resource_type: str = "module",
        resource_id: str | None = None,
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
                description='Include full decoded content for inline resources in the "_content" '
                "field. When False, a 150-char preview is shown instead. Rows past the "
                "per-response content budget carry '_content_truncated': True instead "
                "of '_content'; request a smaller page before copying content into an "
                "update."
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

        Home Assistant 2026.6+ exposes resource management in the UI by default;
        API access works regardless of UI availability.
        """
        return await self.resources.list(
            include_content=include_content, limit=limit, offset=offset
        )

    async def ha_config_set_dashboard_resource(
        self,
        content: Annotated[
            str | None,
            Field(description="JavaScript or CSS code to host inline."),
        ] = None,
        url: Annotated[
            str | None,
            Field(description="URL of the resource."),
        ] = None,
        resource_type: Annotated[
            _RESOURCE_TYPE,
            Field(
                description="Resource type, validated by Home Assistant: 'module' for ES6 "
                "modules (modern cards), 'js' for legacy JavaScript (older custom cards), "
                "'css' for stylesheets (themes, global styles), 'html' for HTML imports"
            ),
        ] = "module",
        resource_id: Annotated[
            str | None,
            Field(
                description="Resource ID to update. If omitted, creates a new resource. "
                "Get IDs from ha_config_list_dashboard_resources()"
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create or update a dashboard resource (inline code or external URL).

        Provide exactly one of:
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
        - url: External resource URL — /local/... (files in /config/www/),
          /hacsfiles/... (HACS-installed cards) or https://... (CDN). Supports
          all types: 'module', 'js', 'css', 'html'.

        EXAMPLES:
        - Inline: ha_config_set_dashboard_resource(content="class MyCard extends HTMLElement { ... } customElements.define('my-card', MyCard);", resource_type="module")
        - HACS card (after ha_manage_hacs(action='download')): ha_config_set_dashboard_resource(url="/hacsfiles/lovelace-mushroom/mushroom.js", resource_type="module")
        - Update: ha_config_set_dashboard_resource(url="/local/my-card-v2.js", resource_type="module", resource_id="abc123")

        After adding a resource, a browser cache clear or hard refresh
        (Ctrl+Shift+R) is needed to load it.
        """
        return await self.resources.save(
            content=content,
            url=url,
            resource_type=resource_type,
            resource_id=resource_id,
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
