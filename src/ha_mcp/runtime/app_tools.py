"""The unique public app contracts, independent of Supervisor connection details."""

from typing import Annotated, Any, Literal, Protocol

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class AppService(Protocol):
    async def get_addon(
        self,
        source: Literal["installed", "available"] | None,
        slug: str | None,
        include_stats: bool,
        repository: str | None,
        query: str | None,
    ) -> dict[str, Any]: ...

    async def manage_addon(
        self,
        slug: str,
        path: str | None,
        method: str,
        body: dict[str, Any] | str | None,
        debug: bool,
        port: int | None,
        offset: int,
        limit: int | None,
        websocket: bool,
        wait_for_close: bool,
        message_limit: int | None,
        message_offset: int,
        summarize: bool,
        python_transform: str | None,
        options: dict[str, Any] | None,
        network: dict[str, Any] | None,
        boot: str | None,
        auto_update: bool | None,
        watchdog: bool | None,
        array_patch: dict[str, Any] | None,
        request_headers: dict[str, str] | None,
        action: str | None = None,
        repository: str | None = None,
    ) -> dict[str, Any]: ...


class AppTools:
    def __init__(self, apps: AppService) -> None:
        self.apps = apps

    async def ha_get_app(
        self,
        source: Annotated[
            Literal["installed", "available"] | None,
            Field(
                description="App (add-on) source. "
                "With source='available', 'version' is the version you would "
                "get by installing (Supervisor's version_latest) and "
                "'version_installed' is the running one, null when the app is "
                "not installed — so compare the two, not 'version' alone, to "
                "tell whether an installed app is current.",
                default=None,
            ),
        ] = None,
        slug: Annotated[
            str | None,
            Field(
                description="App (add-on) slug (e.g., '<prefix>_nodered'). "
                "Slug prefixes vary by app repository — omit to list all apps "
                "and discover the actual installed slug.",
                default=None,
            ),
        ] = None,
        include_stats: Annotated[
            bool,
            Field(
                description="Include CPU/memory usage statistics (only for source='installed')",
                default=False,
            ),
        ] = False,
        repository: Annotated[
            str | None,
            Field(
                description="Filter by repository slug, e.g., 'core', 'community' (only for source='available')",
                default=None,
            ),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description="App (add-on) name/description filter (only for source='available')",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get installed or available Home Assistant apps (add-ons), or details for one.

        Do not use this tool to change app state or configuration; use
        ``ha_manage_app``. Use ``slug`` for details, ``source="installed"`` for an
        inventory, or ``source="available"`` for store discovery.

        Requires Home Assistant OS or Supervised.
        """
        return await self.apps.get_addon(
            source=source,
            slug=slug,
            include_stats=include_stats,
            repository=repository,
            query=query,
        )

    async def ha_manage_app(
        self,
        slug: Annotated[
            str,
            Field(
                description="App (add-on) slug (e.g., '<prefix>_nodered', '<prefix>_frigate'). Slug"
                " prefixes vary by app repository — call ha_get_app() to discover the "
                "actual installed slug. Required except for action='add_repository' / "
                "'remove_repository' (which take 'repository' instead) and "
                "action='check_updates' (which takes neither).",
                default="",
            ),
        ] = "",
        path: Annotated[
            str | None,
            Field(
                description="Proxy mode: API path relative to the app (add-on) root "
                "(e.g., '/flows', '/api/events', '/api/stats'). "
                "Required for proxy mode. ESPHome 2026.10+: POST JSON-RPC to '/api/mcp' "
                "(initialize, notifications/initialized, tools/list, then tools/call); "
                "check response.error first, then response.result.isError.",
                default=None,
            ),
        ] = None,
        method: Annotated[
            str,
            Field(
                description="Proxy mode only. HTTP method: GET, POST, PUT, DELETE, PATCH.",
                default="GET",
            ),
        ] = "GET",
        body: Annotated[
            dict[str, Any] | str | None,
            Field(
                description="Proxy mode only. Request body for POST/PUT/PATCH — or, with websocket=True, the initial WebSocket message. Pass a JSON object or JSON string.",
                default=None,
            ),
        ] = None,
        debug: Annotated[
            bool,
            Field(
                description="Proxy mode only. Include diagnostic info (request URL, headers sent, "
                "response headers).",
                default=False,
            ),
        ] = False,
        port: Annotated[
            int | None,
            Field(
                description="Proxy mode only. Connect to this port instead of the Ingress port. Use"
                " ha_get_app(slug='...') to find available ports. Some apps, including "
                "Node-RED and ESPHome, reject direct access unless their leave_front_door_open "
                "option is enabled and the app is restarted.",
                default=None,
            ),
        ] = None,
        offset: Annotated[
            int,
            Field(
                description="Proxy mode only. HTTP: skip this many items in a JSON array response.",
                default=0,
            ),
        ] = 0,
        limit: Annotated[
            int | None,
            Field(
                description="Proxy mode only. HTTP: return at most this many items from a JSON array response.",
                default=None,
            ),
        ] = None,
        websocket: Annotated[
            bool,
            Field(
                description="Proxy mode only. Use WebSocket instead of HTTP for an app (add-on) "
                "WebSocket API. Sends 'body' as the initial message and collects "
                "responses; command names and body schemas are app/version-specific.",
                default=False,
            ),
        ] = False,
        wait_for_close: Annotated[
            bool,
            Field(
                description="Proxy mode only. WebSocket: True waits for the server to close a "
                "run-to-completion stream. False returns after the first response "
                "batch; use for one-shot command/response or bounded capture on a "
                "channel that stays open.",
                default=True,
            ),
        ] = True,
        message_limit: Annotated[
            int | None,
            Field(
                description="Proxy mode only. WebSocket: cap on messages collected from the wire, "
                "bounded by an internal safety ceiling. None = collect up to the "
                "ceiling.",
                default=None,
            ),
        ] = None,
        message_offset: Annotated[
            int,
            Field(
                description="Proxy mode only. WebSocket: drop this many messages from the start of "
                "the collected list before returning.",
                default=0,
            ),
        ] = 0,
        summarize: Annotated[
            bool,
            Field(
                description="Proxy mode only. WebSocket: when True, collapse runs of non-signal "
                "messages (typically YAML config dumps) into short elision markers. Set"
                " to False to return the raw stream.",
                default=True,
            ),
        ] = True,
        python_transform: Annotated[
            str | None,
            Field(
                description="Proxy mode only. Sandboxed Python expression that post-processes the "
                "response. Variable `response` is exposed — a list[dict | str] for "
                "WebSocket (parsed JSON or raw text), or dict/list/str for HTTP (parsed"
                " body). Supports in-place mutation (response.append(...)) or "
                "reassignment (response = [...]). Example: response = [m for m in "
                "response if 'ERROR' in str(m)].",
                default=None,
            ),
        ] = None,
        options: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description="Config mode: App (add-on) configuration values (the 'Configuration' tab in the UI).",
                default=None,
            ),
        ] = None,
        network: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description="Config mode: Complete desired host-port override map "
                "(e.g., {'5800/tcp': 8081}). A non-empty map replaces current "
                "overrides, so omitted entries are cleared. Omit 'network' to leave "
                "mappings unchanged. An empty map is ignored and does not by itself "
                "select config mode.",
                default=None,
            ),
        ] = None,
        boot: Annotated[
            str | None,
            Field(
                description="Config mode: Boot strategy — 'auto' (start with HA) or 'manual'.",
                default=None,
            ),
        ] = None,
        auto_update: Annotated[
            bool | None,
            Field(
                description="Config mode: Enable or disable automatic updates for this app (add-on).",
                default=None,
            ),
        ] = None,
        watchdog: Annotated[
            bool | None,
            Field(
                description="Config mode: Enable or disable Supervisor watchdog (auto-restart on crash).",
                default=None,
            ),
        ] = None,
        array_patch: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Array-patch mode: GET a JSON array endpoint, "
                    "apply ordered ops, then POST the mutated array back. "
                    "Requires 'path'; mutually exclusive with body / websocket / "
                    "offset / limit and config params. Use ha_get_skill_guide for "
                    "operation shapes."
                ),
                default=None,
            ),
        ] = None,
        request_headers: Annotated[
            dict[str, str] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Proxy/array-patch mode: extra HTTP headers for the app (add-on) API. "
                    "Useful for app-specific requirements such as Node-RED's "
                    "`Node-RED-Deployment-Type: full`. Ingress routing headers "
                    "override caller values on Ingress routes; direct-port calls "
                    "have no internal routing headers. `Content-Type` is derived "
                    "from the body when supplied. Not valid in config or websocket "
                    "mode."
                ),
                default=None,
            ),
        ] = None,
        action: Annotated[
            str | None,
            Field(
                description="Lifecycle mode: run a Supervisor app (add-on) action. One of "
                "'install', 'uninstall', 'start', 'stop', 'restart', 'rebuild', "
                "'update'. 'install'/'update' require the app's repository to be "
                "registered (it appears in ha_get_app(source='available')); 'rebuild' "
                "is for a local app whose source changed but whose version did not. "
                "Store-repository mode: 'add_repository' / 'remove_repository' register"
                " or unregister a custom app store repository — these use the "
                "'repository' param instead of 'slug'. Store-wide mode: 'check_updates'"
                " reloads the store so Supervisor re-scans its repositories (e.g. after"
                " editing a local app's config.yaml) — it takes neither 'slug' nor "
                "'repository', refreshes available metadata only, and installs nothing;"
                " follow with action='update' for a newer version, or action='rebuild' "
                "for a local app whose source changed without a version change. Returns"
                " 'changed' and 'updates_available', null when the store could not be "
                "read (see 'warnings').",
                default=None,
            ),
        ] = None,
        repository: Annotated[
            str | None,
            Field(
                description="Store-repository mode only (action='add_repository' or "
                "'remove_repository'). For add_repository: the repository URL "
                "(e.g., 'https://github.com/balloob/home-assistant-addons'). For "
                "remove_repository: the repository slug (e.g., '0f1cc410', as shown "
                "in ha_get_app(source='available')). Required for those actions; "
                "rejected otherwise.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage Home Assistant apps (add-ons) or proxy an app API.

        For app inventory, status, and Supervisor metadata, call ``ha_get_app``
        first; use proxy mode here for documented app-specific read APIs, and
        do not infer private app API schemas.

        Use exactly one mode: lifecycle/store action, configuration fields,
        ``path`` proxy, or ``path`` with ``array_patch``.

        In the native Home Assistant component, every mode requires write
        permission, including proxy GET: an app API can have side effects
        regardless of its HTTP method. App management is unavailable while
        hidden-entity enforcement is active. Array editing is a GET followed
        by POST, without an atomic guarantee against other app clients.

        Requires Home Assistant OS or Supervised. When ha-mcp itself runs as an
        app it cannot update its own running slug; update ha-mcp from the Home
        Assistant Apps UI. ``options`` merges top-level keys and one nested
        mapping level; supply complete values for deeper nested mappings
        because they are replaced. Prefer Ingress: direct-port access requires
        a shared container network and may require weakening the target app
        authentication. If a Supervisor lifecycle, configuration, or repository
        write has an unknown outcome, verify durable state with ``ha_get_app``
        before retrying. That cannot prove whether ``restart`` or ``rebuild``
        ran; inspect Supervisor jobs and logs and do not automatically replay
        them. For a proxy or array-patch write, query the target app's own read
        API before retrying.
        """
        return await self.apps.manage_addon(
            slug=slug,
            path=path,
            method=method,
            body=body,
            debug=debug,
            port=port,
            offset=offset,
            limit=limit,
            websocket=websocket,
            wait_for_close=wait_for_close,
            message_limit=message_limit,
            message_offset=message_offset,
            summarize=summarize,
            python_transform=python_transform,
            options=options,
            network=network,
            boot=boot,
            auto_update=auto_update,
            watchdog=watchdog,
            array_patch=array_patch,
            request_headers=request_headers,
            action=action,
            repository=repository,
        )
