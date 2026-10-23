"""
Managed YAML configuration editing tools for Home Assistant MCP Server.

Provides a structured, validated tool for editing YAML configuration files
(configuration.yaml and package files) for Home Assistant features that exist
only in YAML and have no REST/WebSocket API equivalent.

**Dependency:** Requires the ha_mcp_tools custom component to be installed.
The tools will gracefully fail with installation instructions if the component is not available.

Feature Flag: Set ENABLE_YAML_CONFIG_EDITING=true to enable.
"""

import fnmatch
import logging
import os
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.tool_api import ToolFailure

from ..backends.legacy import legacy_errors
from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from ..runtime.yaml_tools import YamlConfigTools as YamlContractTools
from ..runtime.yaml_values import (
    _YAML_PACKAGES_FLAG_BY_KEY,
    _YAML_SKILL_FILES,
    _append_yaml_result_warnings,
    _validate_yaml_action_and_content,
)
from ..strict_bps import BestPracticeKeyParam
from .auto_backup import with_auto_backup
from .config_write_helpers import (
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import write_hints
from .tools_config_dashboards import fetch_dashboards_list
from .tools_filesystem import (
    _assert_mcp_tools_available,
    assert_extra_yaml_keys_supported,
    call_mcp_tools_service,
    effective_extra_yaml_write_keys,
)
from .util_helpers import unwrap_service_response

# YAML packages frequently include template sensors, command_line entities,
# and mqtt templates — exactly where template misuse causes the most
# subtle bugs. template-guidelines.md is the relevant default.

logger = logging.getLogger(__name__)

_LOVELACE_DASHBOARD_PREFIX = "lovelace.dashboards."

# Maps a per-key Settings flag onto the yaml_path top-level segment it
# gates. Keep in lockstep with the custom component's
# PACKAGES_ONLY_YAML_KEYS — every key in that frozenset must appear
# here, otherwise a key would silently be unreachable through the
# wrapper. The parity invariant (keys == PACKAGES_ONLY_YAML_KEYS, and
# every value is a real Settings field) is enforced by
# test_yaml_dashboards.py::test_flag_map_matches_packages_only_keys.

# Keys editable here that ALSO have a storage-mode helper equivalent.
# Deliberately NOT blocked (git-managed YAML configs are legitimate) —
# the response instead carries a reactive routing warning, since this
# exact entry point (utility_meter via raw YAML when the helper tool
# covered it) is how the #1720 corruption reached a production config.


def _is_preview_only_call(kwargs: dict[str, Any]) -> bool:
    """True when this ha_config_set_yaml call can only return a preview.

    With the confirm flow enabled and no ``confirm_token`` supplied, the
    component returns a diff preview and writes nothing — so the mandatory
    pre-write auto-backup would snapshot for an edit that never happens
    (phantom ``scope="edits"`` entries). A settings failure returns False,
    failing toward capturing a backup.
    """
    if kwargs.get("confirm_token") is not None:
        return False
    try:
        return bool(get_global_settings().enable_yaml_edit_confirm)
    except Exception:  # noqa: BLE001
        return False


def _disabled_packages_keys(settings: Any) -> list[str]:
    """Return the sorted list of PACKAGES_ONLY_YAML_KEYS whose Settings
    flag is currently False.

    Sorted so the value is deterministic in service payloads and test
    assertions; the custom component treats it as a set so order is
    irrelevant on the wire.

    Uses ``getattr`` without a default so a future rename that breaks the
    ``_YAML_PACKAGES_FLAG_BY_KEY`` → ``Settings`` mapping raises loudly
    instead of silently treating the key as disabled (a dead toggle).
    """
    return sorted(
        key
        for key, flag in _YAML_PACKAGES_FLAG_BY_KEY.items()
        if not getattr(settings, flag)
    )


def _reject_disabled_packages_key(
    yaml_path: str, file: str, settings: Any
) -> tuple[list[str], str, str]:
    """Raise a ToolError if ``yaml_path`` targets a disabled PACKAGES_ONLY
    key in a packages/*.yaml file; otherwise return the values the caller
    needs for the rest of the request (disabled_keys, top_key,
    normalized_target).

    Split out of ``ha_config_set_yaml`` purely to keep the caller's
    complexity down; behavior and messages are unchanged.
    """
    # Per-key gate: reject before the custom-component round
    # trip when the yaml_path top-level segment matches a
    # disabled PACKAGES_ONLY key AND the target file is under
    # packages/. The keys (automation / script / scene) are
    # only ACCEPTED in packages/*.yaml in the first place, so
    # writes to configuration.yaml must fall through here and
    # let the component-side reject with its own message that
    # lists the storage-mode tools to use instead.
    disabled_keys = _disabled_packages_keys(settings)
    top_key = yaml_path.split(".", 1)[0] if yaml_path else ""
    # Classify the target exactly like the custom component does
    # (os.path.normpath + fnmatch against "packages/*.yaml") so the
    # wrapper's early reject fires for precisely the paths the
    # component treats as a package — e.g. "./packages/x.yaml" and
    # "packages/sub/x.yaml" both normalise/match. Any other target
    # (configuration.yaml, a non-package path) falls through to the
    # component, which rejects these keys with its own storage-mode-
    # tools advisory.
    # os.path.normpath is a pure string transform (no I/O) — same
    # classification logic the component uses on its identical normpath
    # check.
    normalized_target = os.path.normpath(file)
    is_packages_target = fnmatch.fnmatch(normalized_target, "packages/*.yaml")
    if is_packages_target and top_key in disabled_keys:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                (
                    f"yaml_path key {top_key!r} is disabled. Enable "
                    f"'Allow {top_key} in packages/*.yaml' under "
                    f"YAML config editing in Server Settings to use "
                    f"this key, or use the storage-mode tool "
                    f"(ha_config_set_{top_key})."
                ),
                suggestions=[
                    f"Enable 'Allow {top_key} in packages/*.yaml' under "
                    "YAML config editing in the ha-mcp Server Settings "
                    "panel, then retry.",
                    f"Or use the storage-mode tool ha_config_set_{top_key} "
                    "instead of editing packages YAML directly.",
                ],
                context={
                    "yaml_path": yaml_path,
                    "disabled_key": top_key,
                    "file": file,
                },
            )
        )
    return disabled_keys, top_key, normalized_target


async def _check_storage_mode_dashboard_collision(client: Any, yaml_path: str) -> None:
    """Raise a ToolError if a storage-mode dashboard already owns the requested
    url_path; otherwise return without doing anything.

    Only runs for yaml_path values starting with 'lovelace.dashboards.'.
    A WebSocket failure or unexpected response shape warns and skips the check
    (fail-open) so that a transient HA outage doesn't block dashboard creation.
    """
    if not yaml_path.startswith(_LOVELACE_DASHBOARD_PREFIX):
        return
    url_path = yaml_path[len(_LOVELACE_DASHBOARD_PREFIX) :]
    try:
        dashboards = await fetch_dashboards_list(client)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "lovelace/dashboards/list WS query failed (%s); skipping collision check",
            exc,
        )
        return

    for entry in dashboards or []:
        if (
            isinstance(entry, dict)
            and entry.get("url_path") == url_path
            and entry.get("mode") == "storage"
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    (
                        f"A storage-mode dashboard already owns url_path "
                        f"'{url_path}'. Delete it via ha_config_delete_dashboard "
                        "or pick a different url_path before registering a "
                        "YAML-mode dashboard."
                    ),
                    context={"url_path": url_path, "existing_id": entry.get("id")},
                    suggestions=[
                        f"ha_config_delete_dashboard(url_path='{url_path}')",
                        "Pick a different url_path for your YAML-mode dashboard.",
                    ],
                )
            )


class LegacyYamlConfig:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def edit(
        self,
        yaml_path: str,
        action: str,
        content: str | None = None,
        file: str = "configuration.yaml",
        confirm_token: str | None = None,
        MandatoryBPS: bool = True,
        BestPracticeKey: BestPracticeKeyParam = None,
    ) -> dict[str, Any]:
        """Retain the existing component-service route for legacy callers."""
        try:
            _validate_yaml_action_and_content(action, content)

            settings = get_global_settings()
            disabled_keys, top_key, normalized_target = _reject_disabled_packages_key(
                yaml_path, file, settings
            )

            # Storage-mode dashboard collision check (only for lovelace.dashboards.*).
            # Skip on `remove` so users can clean up YAML entries that conflict
            # with a storage-mode dashboard (e.g., during a migration).
            if action in ("add", "replace"):
                await _check_storage_mode_dashboard_collision(self._client, yaml_path)

            # Check if custom component is available
            await _assert_mcp_tools_available(self._client)

            extra_keys = await effective_extra_yaml_write_keys(self._client, settings)
            await assert_extra_yaml_keys_supported(self._client, extra_keys)

            # Build service data
            service_data: dict[str, Any] = {
                "file": file,
                "action": action,
                "yaml_path": yaml_path,
                "disabled_packages_keys": disabled_keys,
                "require_confirm": settings.enable_yaml_edit_confirm,
            }
            # Only sent when the operator actually configured extra keys, so a
            # component that predates the field keeps working unchanged for
            # everyone else (its schema is strict and would reject the call).
            if extra_keys:
                service_data["extra_allowed_keys"] = extra_keys
            if content is not None:
                service_data["content"] = content
            if confirm_token is not None:
                service_data["confirm_token"] = confirm_token

            # Call the custom component service (token injected by helper)
            result = await call_mcp_tools_service(
                self._client,
                "edit_yaml_config",
                service_data,
            )

            if isinstance(result, dict):
                result = unwrap_service_response(result)
                if not result.get("success", True):
                    raise_tool_error(result)
                _append_yaml_result_warnings(
                    result,
                    action=action,
                    top_key=top_key,
                    normalized_target=normalized_target,
                )
                attach_skill_content(
                    result,
                    # The confirm leg (token supplied) skips the bulky skill
                    # payload: possession of a token proves the caller just
                    # received the preview response that carried it. Preview
                    # and single-call writes keep it — pre-write guidance is
                    # the whole point of MandatoryBPS.
                    MandatoryBPS=MandatoryBPS and confirm_token is None,
                    canonical_files=_YAML_SKILL_FILES,
                    referenced_files=None,
                )
                return result

            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from YAML config service",
                    context={"file": file},
                )
            )

        except ToolFailure as failure:
            augment_error_dict_with_skill_content(failure.data, bp_warnings=None)
            raise
        except ToolError as te:
            raise augment_tool_error_with_skill_content(te, bp_warnings=None) from None
        except Exception as e:  # noqa: BLE001
            error = exception_to_structured_error(
                e,
                context={
                    "tool": "ha_config_set_yaml",
                    "file": file,
                    "action": action,
                    "yaml_path": yaml_path,
                },
                raise_error=False,
            )
            augment_error_dict_with_skill_content(error, bp_warnings=None)
            raise_tool_error(error)
            return None  # raise_tool_error always raises; explicit for CodeQL
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


class YamlConfigTools(YamlContractTools, LegacyYamlConfig):
    def __init__(self, client: Any) -> None:
        LegacyYamlConfig.__init__(self, client)
        YamlContractTools.__init__(self, self)

    ha_config_set_yaml = tool(
        name="ha_config_set_yaml",
        tags={"System", "beta"},
        annotations=write_hints(
            "Raw YAML Config Edit", destructive=True, idempotent=False, open_world=False
        ),
    )(
        with_auto_backup(
            domain="yaml",
            id_fn=lambda kw: (
                f"{kw.get('file') or 'configuration.yaml'}::{kw.get('yaml_path') or ''}"
            ),
            mandatory=True,
            skip_fn=_is_preview_only_call,
        )(log_tool_usage(legacy_errors(YamlContractTools.ha_config_set_yaml)))
    )


def register_yaml_config_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register YAML config editing tools with the MCP server.

    Requires ENABLE_YAML_CONFIG_EDITING=true.
    """
    settings = get_global_settings()
    if not settings.enable_yaml_config_editing:
        logger.debug(
            "YAML config tools disabled (set ENABLE_YAML_CONFIG_EDITING=true to enable)"
        )
        return
    logger.info("YAML config editing tools enabled")
    register_tool_methods(mcp, YamlConfigTools(client))


__all__ = [
    "register_yaml_config_tools",
]
