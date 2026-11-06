"""Single public dashboard contract; services own Core or legacy execution."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ha_mcp.tool_api.parameters import BestPracticeKeyParam
from ha_mcp.tools.coercion import JSON_STRING_COERCION
from ha_mcp.utils.python_sandbox import get_security_documentation


class DashboardTools:
    def __init__(self, dashboards: Any) -> None:
        self.dashboards = dashboards

    async def ha_config_get_dashboard(
        self,
        url_path: Annotated[
            str | None,
            Field(
                description="Dashboard URL path (e.g. 'lovelace-home'); 'default' for the "
                "default dashboard."
            ),
        ] = None,
        list_only: Annotated[
            bool,
            Field(
                description="List every dashboard's metadata instead of reading one; "
                "ignored when a search parameter is given.",
            ),
        ] = False,
        force_reload: Annotated[
            bool,
            Field(
                description="Get only: bypass the config cache (searches always read "
                "fresh config)."
            ),
        ] = False,
        entity_id: Annotated[
            str | None,
            Field(
                description="Search: cards whose entity/entities field holds this "
                "entity ID; '*' matches any run of characters (including none) "
                "and the whole ID must match (e.g. 'sensor.temperature_*')."
            ),
        ] = None,
        card_type: Annotated[
            str | None,
            Field(
                description="Search: cards of this type, e.g. 'tile'; 'badge' selects "
                "view badges instead (alone, it lists them all). With "
                "describe=True: the type to describe."
            ),
        ] = None,
        heading: Annotated[
            str | None,
            Field(
                description="Search: cards whose heading/title contains this text "
                "(case-insensitive)."
            ),
        ] = None,
        include_config: Annotated[
            bool,
            Field(
                description="Search: include each matched card's own config. A "
                "container's body includes its nested cards, which are also matches, "
                "so nested stacks multiply the payload. Withheld, with a warning, for "
                "a dashboard not provably storage-mode (a YAML config may carry "
                "resolved !secret values)."
            ),
        ] = False,
        include_screenshot: Annotated[
            bool,
            Field(
                description="Get mode only: also return rendered image(s) of the dashboard for "
                "visual verification. Requires the 'dashboard screenshot' beta feature "
                "+ engine app (add-on)/sidecar. If the feature is disabled the config "
                "is returned with a warning; if the engine is configured but the render"
                " fails, the call errors."
            ),
        ] = False,
        view_path: Annotated[
            str | None,
            Field(
                description="Get mode: return ONLY the view whose Lovelace views[].path matches "
                "(response carries 'view' + 'view_index' instead of the full 'config')."
                " With include_screenshot, also selects the view to render."
            ),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description="Search: text or entity ID contained in any value a card "
                "holds (case-insensitive); the matching values are listed under "
                "`matched`."
            ),
        ] = None,
        describe: Annotated[
            bool,
            Field(description="Return card_type's fields from HA's card editor"),
        ] = False,
    ) -> Any:
        """Get Lovelace dashboards: list them, read one config, or search their cards.

        Not for a render alone: ha_get_dashboard_screenshot (screenshot beta
        feature) returns images without the config. Not for entity discovery:
        use ha_search.

        The parameters pick the mode:
        - list_only=True: storage and YAML dashboards alike.
        - Any of query / entity_id / card_type / heading searches cards and
          header cards, AND-ing the criteria, in url_path or, when it is
          omitted, every storage dashboard. View badges answer entity_id and
          query, and heading excludes them.
          Cards nested at any depth count, including
          custom cards' own keys such as groups[].cards[].card. Nesting is
          found heuristically, under the keys cards, card, custom_fields and
          states; a card under any other key is read only as its parent's
          text, by query. Each match has a
          jq_path and a python_path to append to `config` in
          ha_config_set_dashboard(python_transform=...), e.g.
          f'config{m["python_path"]}["icon"] = "mdi:x"'. Each match carries its
          dashboard's url_path and config_hash; a one-dashboard search also
          returns the config_hash at the top level.
        - Otherwise the full config of url_path (the default dashboard when
          omitted) is returned; with view_path only that view, while config_hash
          still covers the full config.

        Caveats: config_hash stays the same across reads of an unchanged config.
        Searches stop at 200 matches (`truncated`). A search across dashboards
        skips YAML dashboards (HA resolves !secret in them) and, without a
        current ha_mcp_tools component, the default dashboard (with a warning);
        name either by url_path.
        Strategy dashboards have no stored cards to search. An unknown view_path
        errors and lists the available ones.

        EXAMPLES:
        - List all dashboards: ha_config_get_dashboard(list_only=True)
        - Get one view only: ha_config_get_dashboard(url_path="lovelace-mobile", view_path="office")
        - Find cards by entity (wildcards allowed): ha_config_get_dashboard(url_path="my-dash", entity_id="sensor.temperature_*")
        - Find heading: ha_config_get_dashboard(url_path="my-dash", heading="Climate", card_type="heading")
        - Which dashboards use an entity: ha_config_get_dashboard(query="light.bedroom")
        - Fields of a card type before writing one (omit card_type to list types):
          ha_config_get_dashboard(card_type="tile", describe=True)
        """
        return await self.dashboards.get(
            url_path=url_path,
            list_only=list_only,
            force_reload=force_reload,
            entity_id=entity_id,
            card_type=card_type,
            heading=heading,
            include_config=include_config,
            include_screenshot=include_screenshot,
            view_path=view_path,
            query=query,
            describe=describe,
        )

    async def ha_config_set_dashboard(
        self,
        url_path: Annotated[
            str,
            Field(
                description="Dashboard URL path (e.g., 'my-dashboard'). "
                "Use 'default' or 'lovelace' for the default dashboard. "
                "Home Assistant requires a hyphen in new dashboard paths."
            ),
        ],
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description="Dashboard configuration with views and cards. "
                "Omit or set to None to create dashboard without initial config. "
                "Mutually exclusive with python_transform and patch."
            ),
        ] = None,
        python_transform: Annotated[
            str | None,
            Field(
                description="Python expression to transform existing dashboard config. "
                "Mutually exclusive with config and patch. "
                "Requires config_hash for validation. "
                "See PYTHON TRANSFORM SECURITY below for allowed operations. "
                "Examples: "
                "Simple: python_transform=\"config['views'][0]['cards'][0]['icon'] = 'mdi:lamp'\" "
                "Pattern: python_transform=\"for card in config['views'][0]['cards']: if 'light' in card.get('entity', ''): card['icon'] = 'mdi:lightbulb'\" "
                "Multi-op: python_transform=\"config['views'][0]['cards'][0]['icon'] = 'mdi:lamp'; del config['views'][0]['cards'][2]\" "
                "\n\n" + get_security_documentation(),
            ),
        ] = None,
        config_hash: Annotated[
            str | None,
            Field(
                description="Config hash from ha_config_get_dashboard for optimistic locking. "
                "REQUIRED for python_transform and patch (validates dashboard unchanged). "
                "Optional for config (validates before full replacement if provided)."
            ),
        ] = None,
        title: Annotated[
            str | None,
            Field(description="Dashboard display name shown in sidebar"),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="MDI icon name (e.g., 'mdi:home', 'mdi:cellphone'). "
                "Omit to store none (the sidebar then shows mdi:view-dashboard); "
                "'' clears a stored icon."
            ),
        ] = None,
        require_admin: Annotated[
            bool | None,
            Field(
                description="Restrict dashboard to admin users only. "
                "For existing dashboards, only updated when explicitly provided."
            ),
        ] = None,
        show_in_sidebar: Annotated[
            bool | None,
            Field(
                description="Show dashboard in sidebar navigation. "
                "For existing dashboards, only updated when explicitly provided."
            ),
        ] = None,
        MandatoryBPS: Annotated[
            bool,
            Field(default=True),
        ] = True,
        # BestPracticeKey (#1779): consumed by StrictBpsMiddleware, never read
        # here — see strict_bps.py for the declaration contract.
        BestPracticeKey: BestPracticeKeyParam = None,
        return_screenshot: Annotated[
            bool,
            Field(
                description="After writing, also return rendered image(s) of the dashboard. "
                "Requires the 'dashboard screenshot' beta feature + engine app "
                "(add-on)/sidecar; if unavailable, the write result is returned with a "
                "warning."
            ),
        ] = False,
        view_path: Annotated[
            str | None,
            Field(
                description="With return_screenshot: stable Lovelace "
                "views[].path to render."
            ),
        ] = None,
        patch: Annotated[
            list[dict[str, Any]] | None,
            JSON_STRING_COERCION,
            Field(
                description="Structured dashboard edits: up to 100 JSON Patch "
                "add, remove, replace or test operations using RFC 6901 paths. "
                "Use /- to append to an array; escape ~ as ~0 and / as ~1 in keys. "
                "Mutually exclusive with config and python_transform. "
                "Strings in value are preserved literally."
            ),
        ] = None,
    ) -> Any:
        """Create or update a Home Assistant dashboard.

        MUST call ha_get_skill_guide OR refer to your locally installed skills first.
        `dashboard-guide.md` ships under `skill_content` by default. A card's
        fields: ha_config_get_dashboard(card_type=..., describe=True).

        MODES (pick one):
        - patch: edit known paths with literal values using JSON Patch
          add/remove/replace/test and config_hash, e.g.
          patch=[{"op": "replace", "path": "/views/0/title", "value": "Home"}].
          move/copy are unsupported. Full guide:
          https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/dashboard-edits.md
        - python_transform: loops or pattern-based changes across cards and views,
          e.g. 'config["views"][0]["cards"].append({"type": "button", "entity": "light.bedroom"})'.
          After delete/add operations indices shift, so chain multiple ops in ONE
          expression where possible; a later call needs a fresh config_hash from
          ha_config_get_dashboard().
        - config: new dashboards only, or a full restructure. Replaces everything.
          Omit it to create a dashboard without initial config.

        Use ha_config_get_dashboard(entity_id=...) to get the path of any card,
        and ha_search / ha_get_overview to find entity IDs — never guess them.
        For visual re-checks after the write, use ha_get_dashboard_screenshot
        (when available) instead of re-sending config.

        title/icon/require_admin/show_in_sidebar can be updated in a
        metadata-only call or alongside a full config replacement; with
        python_transform or patch, update metadata in a separate call (combining
        it with patch is rejected). Strategy dashboards (config
        {"strategy": {...}}) cannot be converted to custom dashboards here; use
        "Take Control" in the Home Assistant UI.

        EXAMPLE:
        ha_config_set_dashboard(url_path="home-dashboard", title="Home Overview",
            config={"views": [{"title": "Home", "type": "sections", "sections": [{"title": "Climate", "cards": [{"type": "tile", "entity": "climate.living_room"}]}]}]})

        This tool manages storage-mode dashboards only. YAML-mode dashboards (a
        .yaml file registered under ``lovelace: dashboards:`` in
        configuration.yaml) and dashboards inlined under ``lovelace:`` are
        edited in their .yaml file; ``ha_config_set_yaml`` can update the
        ``lovelace:`` registration but not the dashboard body.
        """
        return await self.dashboards.set(
            url_path=url_path,
            config=config,
            python_transform=python_transform,
            config_hash=config_hash,
            title=title,
            icon=icon,
            require_admin=require_admin,
            show_in_sidebar=show_in_sidebar,
            MandatoryBPS=MandatoryBPS,
            BestPracticeKey=BestPracticeKey,
            return_screenshot=return_screenshot,
            view_path=view_path,
            patch=patch,
        )

    async def ha_config_delete_dashboard(
        self,
        url_path: Annotated[
            str,
            Field(
                description="Dashboard URL path or internal ID to delete "
                "(e.g., 'my-dashboard' or 'my_dashboard'). Both forms are accepted."
            ),
        ],
    ) -> dict[str, Any]:
        """
        Delete a storage-mode dashboard completely.

        WARNING: This permanently deletes the dashboard and all its configuration.
        Cannot be undone. YAML-mode dashboards are defined in configuration.yaml
        and cannot be deleted through the API.

        HA internal IDs may differ from url_path (e.g. hyphens → underscores); the tool resolves
        either form to the actual registry ID before deletion. 'default' is an
        alias for 'lovelace'.

        EXAMPLES:
        - Delete dashboard: ha_config_delete_dashboard("mobile-dashboard")
        """
        result: dict[str, Any] = await self.dashboards.delete(url_path=url_path)
        return result
