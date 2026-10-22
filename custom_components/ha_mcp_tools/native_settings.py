"""Native settings handlers behind the existing administrator session cookie."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from aiohttp import web

from .const import COMPONENT_VERSION, SERVER_CONFIG_SUBDIR
from .native_server_policy import _read_object, env_tool_states, load_policy

_LOGGER = logging.getLogger(__name__)
_SUPPORTED_FLAGS = (
    "read_only_mode",
    "enable_tool_security_policies",
    "enable_mandatory_bps",
    "enable_strict_mandatory_bps",
    "enable_tool_search",
)
_BACKUP_FLAGS = ("enable_snapshot_actions", "backup_read_only")
_ADVANCED = {
    "enabled_tool_modules": "ENABLED_TOOL_MODULES",
    "custom_instructions": "HAMCP_CUSTOM_INSTRUCTIONS",
}


class NativeSettings:
    """Reuse the settings page and its wire format with instance-owned storage."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.directory = Path(manager.hass.config.path(SERVER_CONFIG_SUBDIR))
        self.worker_id = uuid4().hex

    async def _io(self, function: Any, *args: Any) -> Any:
        return await self.manager.hass.async_add_executor_job(function, *args)

    async def handle(self, request: web.Request, path: str) -> web.Response:
        if not self.manager.is_running:
            return web.json_response({"error": "MCP server is not running"}, status=503)
        routes = {
            "settings": self._page,
            "api/settings/tools": self._tools,
            "api/settings/features": self._features,
            "api/settings/info": self._info,
            "api/settings/theme": self._theme,
            "api/settings/restart": self._restart,
            "api/settings/advanced": self._advanced,
            "api/settings/backup-config": self._backup_config,
            "api/policy/config": self._policy,
            "api/policy/pending": self._pending,
            "api/policy/approve": self._decide,
            "api/policy/deny": self._decide,
            "api/policy/tool-schema": self._tool_schema,
            "api/policy/value-source": self._value_source,
            "api/policy/decision-pin": self._decision_pin,
            "api/visibility/config": self._visibility_config,
        }
        handler = routes.get(path.strip("/"))
        if handler is None:
            return web.json_response(
                {
                    "error": "This settings operation is not available in the native runtime yet"
                },
                status=501,
            )
        try:
            return await handler(request)
        except (ValueError, TypeError) as err:
            return web.json_response({"error": str(err)}, status=400)
        except OSError:
            _LOGGER.exception("Could not read or persist native settings")
            return web.json_response(
                {"error": "Could not read or persist native settings"}, status=500
            )

    async def _page(self, request: web.Request) -> web.Response:
        snapshot = SimpleNamespace(
            cookies=dict(request.cookies),
            headers={key.lower(): value for key, value in request.headers.items()},
            query_params=dict(request.query),
        )
        return web.Response(
            content_type="text/html", text=await self._io(self._render, snapshot)
        )

    def _render(self, request: Any) -> str:
        from ha_mcp.settings_ui import _render_settings_html
        from ha_mcp.settings_ui._theme import _sanitize_theme_prefs

        theme = (
            _sanitize_theme_prefs(_read_object(self.directory / "theme_prefs.json"))
            or {}
        )
        return _render_settings_html(request, theme_prefs=theme)

    async def _tools(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            body = await request.json()
            result = await self._io(self._save_tools, body)
            await self.manager.refresh_policy()
            return web.json_response(result)
        from ha_mcp.llm_exposure import effective_llm_api_exposed
        from ha_mcp.settings_ui._tools_meta import _capability_fields

        await self.manager.refresh_policy()
        tools = await self.manager.mcp.local_provider._list_tools()
        rows = [
            {
                "name": tool.name,
                "title": tool.title,
                "description": tool.description,
                "tags": sorted(tool.tags),
                "primary_tag": next(iter(sorted(tool.tags)), "Home Assistant"),
                **_capability_fields(
                    tool.name,
                    read_only=bool(
                        tool.annotations and tool.annotations.read_only_hint
                    ),
                    destructive=bool(
                        tool.annotations and tool.annotations.destructive_hint
                    ),
                ),
            }
            for tool in tools
        ]
        policy = self.manager.policy
        return web.json_response(
            {
                "tools": rows,
                "states": {
                    **dict.fromkeys(policy.pinned, "pinned"),
                    **policy.tool_states,
                },
                "env_pinned": await self._io(env_tool_states),
                "read_only_exempt": [],
                "llm_api": {
                    tool.name: effective_llm_api_exposed(
                        tool.name, tool.tags, policy.llm_overrides
                    )
                    for tool in tools
                },
                "llm_api_overrides": policy.llm_overrides,
                "llm_api_available": True,
                "bps_locked_tools": ["ha_get_skill_guide"]
                if policy.flag("enable_mandatory_bps")
                and policy.flag("enable_strict_mandatory_bps")
                else [],
                "ignored_disabled_tools": [],
                "tool_config_warnings": [],
            }
        )

    def _save_tools(self, body: Any) -> dict[str, Any]:
        from ha_mcp.settings_ui._handlers_tools import (
            _coerce_llm_overrides,
            _coerce_tool_states,
        )

        if (
            not isinstance(body, dict)
            or not isinstance(body.get("states", {}), dict)
            or not isinstance(body.get("llm_api", {}), dict)
        ):
            raise ValueError("states and llm_api must be JSON objects")
        states = _coerce_tool_states(body.get("states", {}))
        policy = load_policy(self.directory)
        if (
            policy.flag("enable_mandatory_bps")
            and policy.flag("enable_strict_mandatory_bps")
            and states.get("ha_get_skill_guide") == "disabled"
            and policy.tool_states.get("ha_get_skill_guide") != "disabled"
        ):
            raise ValueError(
                "The skill guide is required while strict best-practices mode is enabled"
            )
        pinned = env_tool_states()
        if any(
            name in states and states[name] != value for name, value in pinned.items()
        ):
            raise ValueError("Environment-pinned tools cannot be changed here")
        persisted = {
            name: value for name, value in states.items() if name not in pinned
        }
        llm_api = _coerce_llm_overrides(body.get("llm_api", {}))
        self._merge("tool_config.json", {"tools": persisted, "llm_api": llm_api})
        restart_required = (
            bool(getattr(self.manager, "search_enabled", False))
            and policy.pinned != load_policy(self.directory).pinned
        )
        return {
            "success": True,
            "applied": persisted,
            "llm_api_applied": llm_api,
            "mode": "file",
            "restart_required": restart_required,
        }

    async def _features(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            body = await request.json()
            result = await self._io(self._save_features, body)
            await self.manager.refresh_policy()
            return web.json_response(result)
        await self.manager.refresh_policy()
        policy = self.manager.policy
        flags = {
            name: {
                "value": policy.flag(name),
                "origin": "env" if name.upper() in os.environ else "file",
                "editable": name.upper() not in os.environ,
                "type": "bool",
                "env_var": name.upper(),
            }
            for name in _SUPPORTED_FLAGS
        }
        return web.json_response(
            {"flags": flags, "beta_sub_flags": [], "is_addon": False}
        )

    def _save_features(self, body: Any) -> dict[str, Any]:
        raw = body.get("flags") if isinstance(body, dict) else None
        if not isinstance(raw, dict) or not raw:
            raise ValueError("flags must be a nonempty JSON object")
        for name, value in raw.items():
            if name not in _SUPPORTED_FLAGS or not isinstance(value, bool):
                raise ValueError(f"Unsupported native feature setting: {name}")
            if name.upper() in os.environ:
                raise ValueError(f"Environment-controlled setting: {name}")
        self._validate_bps_flags(raw)
        self._merge("feature_flags.json", raw)
        return {
            "success": True,
            "applied": raw,
            "mode": "file",
            "restart_required": "enable_tool_search" in raw,
        }

    def _validate_bps_flags(self, raw: dict[str, Any]) -> None:
        from dataclasses import replace

        policy = load_policy(self.directory)
        updated = replace(policy, flags={**policy.flags, **raw})
        parent = updated.flag("enable_mandatory_bps")
        strict = updated.flag("enable_strict_mandatory_bps")
        if raw.get("enable_strict_mandatory_bps") is True and not parent:
            raise ValueError("Enable best-practice skills before enabling strict mode")
        if (
            parent
            and strict
            and {"enable_mandatory_bps", "enable_strict_mandatory_bps"}.intersection(
                raw
            )
        ):
            states = _read_object(self.directory / "tool_config.json").get("tools", {})
            if states.get("ha_get_skill_guide") == "disabled":
                raise ValueError(
                    "Re-enable the skill guide before enabling strict mode"
                )

    async def _theme(self, request: web.Request) -> web.Response:
        if request.method == "GET":
            return web.json_response(
                {
                    "prefs": await self._io(
                        _read_object, self.directory / "theme_prefs.json"
                    )
                }
            )
        body = await request.json()
        sanitized = await self._io(self._save_theme, body)
        return web.json_response({"success": True, "applied": sanitized})

    async def _backup_config(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            result = await self._io(self._save_backup_config, await request.json())
            await self.manager.refresh_policy()
            return web.json_response(result)
        await self.manager.refresh_policy()
        policy = self.manager.policy
        fields = [
            {
                "field": name,
                "env_var": name.upper(),
                "value": policy.flag(name),
                "origin": "env" if name.upper() in os.environ else "file",
                "editable": name.upper() not in os.environ,
            }
            for name in _BACKUP_FLAGS
        ]
        return web.json_response({"success": True, "is_addon": False, "fields": fields})

    def _save_backup_config(self, body: Any) -> dict[str, Any]:
        from ha_mcp.settings_ui._handlers_backups import _validate_backup_payload

        clean, error = _validate_backup_payload(body)
        if error is not None:
            raise ValueError(error)
        for name in clean:
            if name not in _BACKUP_FLAGS or name.upper() in os.environ:
                raise ValueError(
                    f"Unsupported or environment-controlled native backup setting: {name}"
                )
        self._merge("backup_settings.json", clean)
        return {
            "success": True,
            "applied": clean,
            "mode": "file",
            "restart_required": False,
        }

    def _save_theme(self, body: Any) -> dict[str, str]:
        from ha_mcp.settings_ui._theme import _sanitize_theme_prefs

        sanitized = _sanitize_theme_prefs(body)
        if not sanitized:
            raise ValueError("No valid theme preferences")
        self._merge("theme_prefs.json", sanitized)
        return sanitized

    def _merge(self, filename: str, updates: dict[str, Any]) -> None:
        from ha_mcp.settings_ui._persistence import _atomic_write_json
        from ha_mcp.utils.config_write_lock import config_file_lock

        self.directory.mkdir(parents=True, exist_ok=True)
        with config_file_lock(self.directory):
            path = self.directory / filename
            existing = _read_object(path)
            _atomic_write_json(path, {**existing, **updates})

    async def _info(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "is_addon": False,
                "is_sidecar": False,
                "deployment_mode": "embedded",
                "runtime": "native",
                "instance_id": self.worker_id,
                "worker_id": self.worker_id,
                "version": COMPONENT_VERSION,
            }
        )

    async def _restart(self, request: web.Request) -> web.Response:
        if request.method != "POST":
            return web.Response(status=405)
        self.manager.hass.loop.call_later(
            0.2,
            self.manager.hass.config_entries.async_schedule_reload,
            self.manager.entry.entry_id,
        )
        return web.json_response({"success": True, "message": "Reload scheduled"})

    async def _advanced(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            result = await self._io(self._save_advanced, await request.json())
            await self.manager.refresh_policy()
            return web.json_response(result)
        await self.manager.refresh_policy()
        values = self.manager.policy.flags
        fields = [
            {
                "field": name,
                "env_var": env,
                "type": "str",
                "section": "tools_surface",
                "value": os.environ.get(
                    env,
                    values.get(name, "all" if name == "enabled_tool_modules" else ""),
                ),
                "origin": "env" if env in os.environ else "file",
                "editable": env not in os.environ,
                "multiline": name == "custom_instructions",
                "restart_required": False,
            }
            for name, env in _ADVANCED.items()
        ]
        return web.json_response(
            {"fields": fields, "is_addon": False, "is_stdio": False}
        )

    def _save_advanced(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or not body:
            raise ValueError("Expected advanced settings as a nonempty object")
        for name, value in body.items():
            if name not in _ADVANCED or not isinstance(value, str):
                raise ValueError(f"Unsupported native advanced setting: {name}")
            if _ADVANCED[name] in os.environ:
                raise ValueError(f"Environment-controlled setting: {name}")
        self._merge("feature_flags.json", body)
        return {
            "success": True,
            "applied": body,
            "mode": "file",
            "restart_required": False,
        }

    async def _policy(self, request: web.Request) -> web.Response:
        from ha_mcp.policy.persistence import load_policy

        if request.method == "GET":
            policy = await self._io(load_policy, self.directory)
            return web.json_response(policy.model_dump(mode="json"))
        if request.method != "PUT":
            return web.Response(status=405)
        result, changed = await self._io(self._save_policy, await request.json())
        if changed:
            self.manager.approval_gate.queue.clear_remember_cache()
        await self.manager.refresh_policy()
        return web.json_response(
            result, status=409 if "current_version" in result else 200
        )

    def _save_policy(self, body: Any) -> tuple[dict[str, Any], bool]:
        from ha_mcp.policy.decision_pin import is_pin_set
        from ha_mcp.policy.model import (
            ALLOW_LIST_OMITTED_MESSAGE,
            Policy,
            drops_allow_list,
            gates_differ,
        )
        from ha_mcp.policy.persistence import load_policy, save_policy
        from ha_mcp.utils.config_write_lock import config_file_lock

        new = Policy.model_validate(body)
        self.directory.mkdir(parents=True, exist_ok=True)
        with config_file_lock(self.directory):
            current = load_policy(self.directory)
            if drops_allow_list(new, current):
                raise ValueError(ALLOW_LIST_OMITTED_MESSAGE)
            if new.event_decisions_enabled and not is_pin_set(self.directory):
                raise ValueError("Set an approval PIN before enabling event decisions")
            if new.version != current.version:
                return {
                    "error": "policy version mismatch",
                    "current_version": current.version,
                    "current_policy": current.model_dump(mode="json"),
                }, False
            save_policy(self.directory, new)
        return {"saved": True, "version": new.version + 1}, gates_differ(current, new)

    async def _pending(self, request: web.Request) -> web.Response:
        pending = self.manager.approval_gate.queue.list_pending()
        return web.json_response(
            {
                "pending": [
                    {
                        "token": item.token,
                        "tool_name": item.tool_name,
                        "args": item.args,
                        "created_at": item.created_at.isoformat(),
                        "expires_at": item.expires_at.isoformat(),
                    }
                    for item in pending
                ]
            }
        )

    async def _decision_pin(self, request: web.Request) -> web.Response:
        from ha_mcp.policy.decision_pin import pin_status

        if request.method == "GET":
            return web.json_response(await self._io(pin_status, self.directory))
        body = await request.json() if request.method == "POST" else None
        result = await self._io(self._change_pin, request.method, body)
        await self.manager.refresh_policy()
        return web.json_response(result)

    def _change_pin(self, method: str, body: Any) -> dict[str, Any]:
        from ha_mcp.policy.decision_pin import (
            clear_pin,
            pin_status,
            set_pin,
            validate_pin,
        )
        from ha_mcp.policy.persistence import load_policy, save_policy
        from ha_mcp.utils.config_write_lock import config_file_lock

        self.directory.mkdir(parents=True, exist_ok=True)
        with config_file_lock(self.directory):
            if method == "POST":
                pin = validate_pin(body.get("pin") if isinstance(body, dict) else None)
                set_pin(self.directory, pin)
            elif method == "DELETE":
                policy = load_policy(self.directory)
                if policy.event_decisions_enabled:
                    save_policy(
                        self.directory,
                        policy.model_copy(update={"event_decisions_enabled": False}),
                    )
                clear_pin(self.directory)
            else:
                raise ValueError("Unsupported PIN operation")
        return {"success": True, **pin_status(self.directory)}

    async def _decide(self, request: web.Request) -> web.Response:
        if request.method != "POST":
            return web.Response(status=405)
        body = await request.json()
        token = body.get("token") if isinstance(body, dict) else None
        queue = self.manager.approval_gate.queue
        if not isinstance(token, str) or queue.get(token) is None:
            return web.json_response({"error": "unknown token"}, status=404)
        decision = "approve" if request.path.endswith("/approve") else "deny"
        outcome = queue.decide_with_outcome(token, decision)
        if outcome.value != "applied":
            return web.json_response({"error": "already decided"}, status=409)
        return web.json_response(
            {"approved" if decision == "approve" else "denied": True}
        )

    async def _tool_schema(self, request: web.Request) -> web.Response:
        from starlette.requests import Request

        from ha_mcp.policy.handlers import _get_tool_schema

        response = await _get_tool_schema(
            self.manager,
            Request({"type": "http", "query_string": request.query_string.encode()}),
        )
        return web.Response(
            body=response.body,
            status=response.status_code,
            content_type="application/json",
        )

    async def _value_source(self, request: web.Request) -> web.Response:
        from ha_mcp.policy.value_sources import project_value_source

        from .ui_panel import _COOKIE_NAME, _session_user_is_admin

        hass = self.manager.hass
        if not await _session_user_is_admin(hass, request.cookies.get(_COOKIE_NAME)):
            return web.Response(status=401)
        source = request.query.get("source", "")
        params = {key: value for key, value in request.query.items() if key != "source"}
        if source in ("ha_domains", "ha_services"):
            data = hass.services.async_services()
        elif source == "ha_entities":
            data = [{"entity_id": state.entity_id} for state in hass.states.async_all()]
        elif source == "ha_config_entries":
            data = [
                {"entry_id": entry.entry_id}
                for entry in hass.config_entries.async_entries()
            ]
        else:
            raise ValueError(f"Unknown value source: {source!r}")
        return web.json_response(
            {"source": source, "values": project_value_source(source, data, params)}
        )

    async def _visibility_config(self, request: web.Request) -> web.Response:
        from .native_visibility import load_native_visibility

        if request.method == "GET":
            try:
                config = await self._io(load_native_visibility, self.directory)
            except ValueError as error:
                return web.json_response(
                    {"error": str(error), "visibility_file_corrupt": True}, status=500
                )
            return web.json_response(config.model_dump(mode="json"))
        if request.method != "PUT":
            return web.Response(status=405)
        result = await self._io(self._save_visibility, await request.json())
        return web.json_response(
            result, status=409 if "current_version" in result else 200
        )

    def _save_visibility(self, body: Any) -> dict[str, Any]:
        from ha_mcp.utils.config_write_lock import config_file_lock
        from ha_mcp.visibility.model import VisibilityConfig
        from ha_mcp.visibility.persistence import save_visibility_config

        from .native_visibility import load_native_visibility

        config = VisibilityConfig.model_validate(body)
        self.directory.mkdir(parents=True, exist_ok=True)
        with config_file_lock(self.directory):
            current = load_native_visibility(self.directory)
            if config.version != current.version:
                return {
                    "error": "Visibility config version mismatch",
                    "current_version": current.version,
                    "current_config": current.model_dump(mode="json"),
                }
            save_visibility_config(self.directory, config)
        return {"saved": True, "version": config.version + 1}
