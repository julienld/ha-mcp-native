"""One app-mode selector; concrete hosts implement only the finite IO operations."""

from typing import Any, ClassVar, cast

from ha_mcp.errors import create_validation_error

from .app_values import _validate_supervisor_slug
from .errors import raise_failure
from .redaction import sentinel_option_keys
from .validation import validate_identifier_not_empty


class AppDispatch:
    _REPOSITORY_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"add_repository", "remove_repository"}
    )
    _STORE_ACTIONS: ClassVar[frozenset[str]] = frozenset({"check_updates"})

    @staticmethod
    def _build_config_payload(
        options: dict[str, Any] | None,
        network: dict[str, Any] | None,
        boot: str | None,
        auto_update: bool | None,
        watchdog: bool | None,
    ) -> dict[str, Any]:
        config_data: dict[str, Any] = {}
        if options:
            config_data["options"] = options
        if network:
            config_data["network"] = network
        if boot is not None:
            config_data["boot"] = boot
        if auto_update is not None:
            config_data["auto_update"] = auto_update
        if watchdog is not None:
            config_data["watchdog"] = watchdog
        return config_data

    @staticmethod
    def _validate_manage_mode(path: str | None, config_data: dict[str, Any]) -> None:
        if path is not None and path == "":
            raise_failure(
                create_validation_error(
                    "'path' must not be empty. Provide a non-empty path for proxy mode "
                    "(e.g., '/api/events') or omit it to use config mode.",
                    parameter="path",
                )
            )
        if path is not None and config_data:
            raise_failure(
                create_validation_error(
                    "Cannot combine 'path' (proxy mode) with config parameters "
                    "(options/network/boot/auto_update/watchdog). Use one mode at a time.",
                    parameter="path",
                )
            )
        if not path and not config_data:
            raise_failure(
                create_validation_error(
                    "Must provide either 'path' for proxy mode or at least one config parameter "
                    "(options/network/boot/auto_update/watchdog) for config mode.",
                    parameter="path",
                )
            )

    @staticmethod
    def _reject_sentinel_options(options: dict[str, Any]) -> None:
        """Reject redaction sentinels before any merge/write (#2157).

        A caller round-tripping a redacted ha_get_app read must not
        overwrite a live credential with the placeholder string. Omitting
        the key keeps the current value (writes merge server-side). Active
        regardless of the redact_secrets toggle: a sentinel captured while
        redaction was on must not overwrite a credential after the operator
        turns it off.
        """
        sentinel_keys = sentinel_option_keys(options)
        if sentinel_keys:
            raise_failure(
                create_validation_error(
                    "options contains redaction placeholder values for: "
                    f"{', '.join(sentinel_keys)}. These came from a "
                    "redacted read, not real values — omit these keys to "
                    "keep the current values, or submit the real value.",
                    parameter="options",
                )
            )

    @staticmethod
    def _validate_array_patch_input(
        array_patch: dict[str, Any],
        websocket: bool,
        body: Any,
        offset: int,
        limit: int | None,
    ) -> tuple[str, list[Any]]:
        """Validate array_patch parameters and return (id_field, operations)."""
        if not isinstance(array_patch, dict):
            raise_failure(
                create_validation_error(
                    "array_patch must be an object", parameter="array_patch"
                )
            )
        if websocket:
            raise_failure(
                create_validation_error(
                    "array_patch is HTTP-only and cannot be combined with websocket=True",
                    parameter="array_patch",
                )
            )
        if body is not None:
            raise_failure(
                create_validation_error(
                    "array_patch builds the POST body itself; remove the explicit 'body' parameter",
                    parameter="array_patch",
                )
            )
        if offset != 0 or limit is not None:
            raise_failure(
                create_validation_error(
                    "array_patch needs the full array; offset/limit are not supported in this mode",
                    parameter="array_patch",
                )
            )
        id_field = array_patch.get("id_field", "id")
        if not isinstance(id_field, str) or not id_field:
            raise_failure(
                create_validation_error(
                    "array_patch.id_field must be a non-empty string",
                    parameter="array_patch.id_field",
                )
            )
        ops = array_patch.get("operations")
        if not isinstance(ops, list) or not ops:
            raise_failure(
                create_validation_error(
                    "array_patch.operations must be a non-empty list",
                    parameter="array_patch.operations",
                )
            )
        return id_field, ops

    @staticmethod
    def _proxy_overrides_basic(
        method: str,
        body: Any,
        debug: bool,
        port: int | None,
        offset: int,
        limit: int | None,
        websocket: bool,
    ) -> list[tuple[str, str]]:
        """Collect (param_name, display) pairs for proxy-mode params that are non-default and invalid when config mode is active."""
        result: list[tuple[str, str]] = []
        if method != "GET":
            result.append(("method", f"method={method!r}"))
        if body is not None:
            result.append(("body", "body"))
        if debug:
            result.append(("debug", "debug=True"))
        if port is not None:
            result.append(("port", f"port={port}"))
        if offset != 0:
            result.append(("offset", f"offset={offset}"))
        if limit is not None:
            result.append(("limit", f"limit={limit}"))
        if websocket:
            result.append(("websocket", "websocket=True"))
        return result

    @staticmethod
    def _proxy_overrides_ws_and_extra(
        wait_for_close: bool,
        message_limit: int | None,
        message_offset: int,
        summarize: bool,
        python_transform: str | None,
        array_patch: dict[str, Any] | None,
        request_headers: dict[str, str] | None,
    ) -> list[tuple[str, str]]:
        """Collect (param_name, display) pairs for WS/transform params that are non-default and invalid when config mode is active."""
        result: list[tuple[str, str]] = []
        if not wait_for_close:
            result.append(("wait_for_close", "wait_for_close=False"))
        if message_limit is not None:
            result.append(("message_limit", f"message_limit={message_limit}"))
        if message_offset != 0:
            result.append(("message_offset", f"message_offset={message_offset}"))
        if not summarize:
            result.append(("summarize", "summarize=False"))
        if python_transform is not None:
            result.append(("python_transform", "python_transform"))
        if array_patch is not None:
            result.append(("array_patch", "array_patch"))
        if request_headers is not None:
            result.append(("request_headers", "request_headers"))
        return result

    async def _dispatch_repository_action(
        self: Any,
        action: str,
        repository: str | None,
        *,
        slug: str,
        path: str | None,
        config_data: dict[str, Any],
        proxy_overrides: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """Validate and run a store-repository action (add/remove).

        Repository actions don't target an installed app (add-on), so a `slug` is
        not required; `repository` (URL for add, slug for remove) is. Reject
        the other operating modes' params so the call has one unambiguous
        intent.
        """
        conflicts = []
        if slug:
            conflicts.append("slug")
        if path is not None:
            conflicts.append("path")
        if config_data:
            conflicts.append("config parameters")
        conflicts.extend(display for _, display in proxy_overrides)
        if conflicts:
            raise_failure(
                create_validation_error(
                    f"action='{action}' (store-repository mode) operates on the "
                    f"store, not an app (add-on), and cannot be combined with "
                    f"{', '.join(conflicts)}. Pass only 'repository'.",
                    parameter="action",
                )
            )
        if not repository or not repository.strip():
            raise_failure(
                create_validation_error(
                    f"action='{action}' requires the 'repository' parameter "
                    "(the repository URL for add_repository, or the repository "
                    "slug for remove_repository).",
                    parameter="repository",
                )
            )
        repository = repository.strip()
        if action.lower().strip() == "remove_repository":
            _validate_supervisor_slug(repository, "repository")
        return cast(
            dict[str, Any], await self._execute_repository_action(action, repository)
        )

    @staticmethod
    def _reject_store_action_conflicts(
        action: str,
        *,
        slug: str | None,
        repository: str | None,
        path: str | None,
        config_data: dict[str, Any],
        proxy_overrides: list[tuple[str, str]],
    ) -> None:
        """Raise if a store-wide action is combined with another mode's params.

        ``check_updates`` reloads the whole store, so naming one app (add-on)
        or one repository would silently suggest a narrower scope than the
        call actually has.
        """
        conflicts = []
        if slug:
            conflicts.append("slug")
        if repository:
            conflicts.append("repository")
        if path is not None:
            conflicts.append("path")
        if config_data:
            conflicts.append("config parameters")
        conflicts.extend(display for _, display in proxy_overrides)
        if not conflicts:
            return
        raise_failure(
            create_validation_error(
                f"action='{action}' (store-wide mode) reloads the whole app "
                f"(add-on) store and cannot be combined with "
                f"{', '.join(conflicts)}. Pass only 'action'.",
                parameter="action",
            )
        )

    @staticmethod
    def _reject_action_mode_conflicts(
        action: str,
        path: str | None,
        config_data: dict[str, Any],
        proxy_overrides: list[tuple[str, str]],
    ) -> None:
        """Raise if lifecycle-action mode is combined with another mode's params."""
        conflicts = []
        if path is not None:
            conflicts.append("path")
        if config_data:
            conflicts.append("config parameters")
        conflicts.extend(display for _, display in proxy_overrides)
        if conflicts:
            raise_failure(
                create_validation_error(
                    f"action='{action}' (lifecycle mode) cannot be combined "
                    f"with {', '.join(conflicts)}. Use one mode at a time.",
                    parameter="action",
                )
            )

    def _reject_config_mode_proxy_params(
        self: Any,
        *,
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
        array_patch: dict[str, Any] | None,
        request_headers: dict[str, str] | None,
    ) -> None:
        """Raise if any proxy-mode-only param is set while config mode is active."""
        proxy_overrides = self._proxy_overrides_basic(
            method, body, debug, port, offset, limit, websocket
        ) + self._proxy_overrides_ws_and_extra(
            wait_for_close,
            message_limit,
            message_offset,
            summarize,
            python_transform,
            array_patch,
            request_headers,
        )
        if proxy_overrides:
            raise_failure(
                create_validation_error(
                    f"Proxy-mode parameters cannot be used in config mode: {', '.join(d for _, d in proxy_overrides)}. "
                    "Remove these parameters or switch to proxy mode by providing 'path'.",
                    parameter=proxy_overrides[0][0],
                )
            )

    async def manage_addon(
        self: Any,
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
    ) -> dict[str, Any]:
        config_data = self._build_config_payload(
            options, network, boot, auto_update, watchdog
        )
        proxy_overrides = self._proxy_overrides_basic(
            method, body, debug, port, offset, limit, websocket
        ) + self._proxy_overrides_ws_and_extra(
            wait_for_close,
            message_limit,
            message_offset,
            summarize,
            python_transform,
            array_patch,
            request_headers,
        )

        # Handled before the slug requirement below, which they never meet.
        if action is not None and action.lower().strip() in self._STORE_ACTIONS:
            self._reject_store_action_conflicts(
                action,
                slug=slug,
                repository=repository,
                path=path,
                config_data=config_data,
                proxy_overrides=proxy_overrides,
            )
            return cast(dict[str, Any], await self._execute_check_updates())

        # Store-repository actions operate on the store, not an app (add-on), so they
        # take `repository` instead of `slug`. Handle them before the slug
        # requirement applies.
        if action is not None and action.lower().strip() in self._REPOSITORY_ACTIONS:
            return cast(
                dict[str, Any],
                await self._dispatch_repository_action(
                    action,
                    repository,
                    slug=slug,
                    path=path,
                    config_data=config_data,
                    proxy_overrides=proxy_overrides,
                ),
            )
        if repository is not None:
            raise_failure(
                create_validation_error(
                    "'repository' is valid only with action='add_repository' or "
                    "action='remove_repository'.",
                    parameter="repository",
                )
            )

        validate_identifier_not_empty(
            slug,
            "slug",
            suggestions=["Use ha_get_app() to discover installed app (add-on) slugs"],
        )
        _validate_supervisor_slug(slug)

        # Lifecycle mode takes precedence and is mutually exclusive with the
        # proxy / config / array-patch modes.
        if action is not None:
            self._reject_action_mode_conflicts(
                action, path, config_data, proxy_overrides
            )
            return cast(dict[str, Any], await self._execute_action_mode(slug, action))

        self._validate_manage_mode(path, config_data)

        if config_data:
            self._reject_config_mode_proxy_params(
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
                array_patch=array_patch,
                request_headers=request_headers,
            )
            return cast(
                dict[str, Any], await self._execute_config_mode(slug, config_data)
            )

        # _call_addon_ws does not accept caller headers — reject the combo rather
        # than silently dropping them (matches the fail-loud-on-misroute pattern
        # used for message_limit / message_offset / summarize on HTTP).
        if request_headers is not None and websocket:
            raise_failure(
                create_validation_error(
                    "request_headers applies only to HTTP and array_patch modes; "
                    "remove it or set websocket=False",
                    parameter="request_headers",
                )
            )

        if path is None:
            raise RuntimeError(
                "path is None — should be unreachable after _validate_manage_mode"
            )

        if array_patch is not None:
            return cast(
                dict[str, Any],
                await self._execute_array_patch(
                    slug,
                    path,
                    array_patch,
                    websocket,
                    body,
                    offset,
                    limit,
                    debug,
                    port,
                    request_headers,
                ),
            )

        if websocket:
            return cast(
                dict[str, Any],
                await self._execute_ws_proxy(
                    slug,
                    path,
                    body,
                    debug,
                    port,
                    wait_for_close,
                    message_limit,
                    message_offset,
                    summarize,
                    python_transform,
                ),
            )

        return cast(
            dict[str, Any],
            await self._execute_http_proxy(
                slug=slug,
                path=path,
                method=method,
                body=body,
                debug=debug,
                port=port,
                offset=offset,
                limit=limit,
                python_transform=python_transform,
                request_headers=request_headers,
                message_limit=message_limit,
                message_offset=message_offset,
                summarize=summarize,
            ),
        )
