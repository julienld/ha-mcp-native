"""HA MCP Tools - Custom component for ha-mcp server.

Provides services that are not available through standard Home Assistant APIs,
enabling AI assistants to perform advanced operations like file management.
"""

from __future__ import annotations

import difflib
import errno
import fnmatch
import hashlib
import logging
import os
import posixpath
import re
import secrets
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import Any

import voluptuous as vol
import yaml  # type: ignore[import-untyped]
from homeassistant.components import persistent_notification
from homeassistant.config import async_check_ha_config_file
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration
from ruamel.yaml import YAMLError

from ha_mcp.tool_api import ToolFailure

from .const import (
    ALLOWED_READ_DIRS,
    ALLOWED_VOLUME_ROOTS,
    ALLOWED_WRITE_DIRS,
    ALLOWED_YAML_CONFIG_FILES,
    ALLOWED_YAML_KEYS,
    COMPONENT_VERSION,
    CONF_ENTRY_TYPE,
    DASHBOARD_URL_PATH_PATTERN,
    DENY_PATH_SEGMENTS,
    DENY_READ_BASENAMES,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ENTRY_TYPE_TOOLS,
    PACKAGES_ONLY_YAML_KEYS,
    RESERVED_DASHBOARD_URL_PATHS,
    TOOLS_ENTRY_LEGACY_TITLE,
    TOOLS_ENTRY_TITLE,
    YAML_KEY_DEFAULT_POST_ACTION,
    YAML_KEY_DENYLIST,
    YAML_KEY_POST_ACTIONS,
)
from .entry_device import async_register_entry_device
from .entry_migration import async_migrate_entry as async_migrate_entry
from .file_access import (
    _HASS_DATA_ALLOWED_PATHS_KEY as _HASS_DATA_ALLOWED_PATHS_KEY,
)
from .file_access import (
    _HASS_DATA_EXTRA_YAML_KEYS_KEY as _HASS_DATA_EXTRA_YAML_KEYS_KEY,
)
from .file_access import (
    DATA_FILE_ACCESS,
    FileAccess,
    service_file_access,
)
from .file_access import (
    _current_extra_dirs as _current_extra_dirs,
)
from .file_access import (
    _current_extra_yaml_keys as _current_extra_yaml_keys,
)
from .file_io import (
    DEFAULT_LOG_TAIL_LINES as DEFAULT_LOG_TAIL_LINES,
)
from .file_io import (
    TAILED_LOG_FILES as TAILED_LOG_FILES,
)
from .file_io import (
    _delete_file_sync as _delete_file_sync,
)
from .file_io import (
    _list_files_sync as _list_files_sync,
)
from .file_io import (
    _read_file_sync as _read_file_sync,
)
from .file_io import (
    _read_log_tail_sync as _read_log_tail_sync,
)
from .file_io import (
    _replace_file_sync as _replace_file_sync,
)
from .file_io import (
    _write_file_sync as _write_file_sync,
)
from .file_paths import (
    ALLOWED_READ_FILES as ALLOWED_READ_FILES,
)
from .file_paths import (
    _dir_in_package_dir as _dir_in_package_dir,
)
from .file_paths import (
    _is_path_allowed_for_dir as _is_path_allowed_for_dir,
)
from .file_paths import (
    _is_path_allowed_for_read as _is_path_allowed_for_read,
)
from .file_paths import (
    _is_volume_path_allowed as _is_volume_path_allowed,
)
from .file_paths import (
    _is_within_config_dir as _is_within_config_dir,
)
from .file_paths import (
    _matches_extra_dir as _matches_extra_dir,
)
from .file_paths import (
    _normalize_extra_dir as _normalize_extra_dir,
)
from .file_paths import (
    _normalize_volume_dir as _normalize_volume_dir,
)
from .file_paths import (
    _path_in_package_dir as _path_in_package_dir,
)
from .file_paths import (
    _resolves_within as _resolves_within,
)
from .file_paths import (
    _validate_dashboard_filename as _validate_dashboard_filename,
)
from .file_paths import (
    _violates_deny_floor as _violates_deny_floor,
)
from .file_paths import (
    _violates_volume_deny_floor as _violates_volume_deny_floor,
)
from .file_paths import (
    _volume_root_for as _volume_root_for,
)
from .file_views import (
    _extract_yaml_subtree as _extract_yaml_subtree,
)
from .file_views import (
    _extract_yaml_views as _extract_yaml_views,
)
from .file_views import (
    _mask_secrets_content as _mask_secrets_content,
)
from .file_views import (
    _shape_read_file_response as _shape_read_file_response,
)
from .legacy_backup_files import (
    _LEGACY_BACKUP_DIRNAME as _LEGACY_BACKUP_DIRNAME,
)
from .legacy_backup_files import (
    _LEGACY_BACKUP_RE as _LEGACY_BACKUP_RE,
)
from .legacy_backup_files import (
    _decode_legacy_backup_name as _decode_legacy_backup_name,
)
from .legacy_backup_files import (
    _list_legacy_backups_sync as _list_legacy_backups_sync,
)
from .legacy_backup_files import (
    _read_legacy_backup_sync as _read_legacy_backup_sync,
)
from .legacy_backup_files import (
    read_legacy_backup,
)
from .package_dirs import (
    _PACKAGE_DIR_CACHE as _PACKAGE_DIR_CACHE,
)
from .package_dirs import (
    _capture_packages_dir as _capture_packages_dir,
)
from .package_dirs import (
    _detect_package_dirs as _detect_package_dirs,
)
from .package_dirs import (
    _extract_package_dir_markers as _extract_package_dir_markers,
)
from .package_dirs import (
    _follow_include as _follow_include,
)
from .package_dirs import (
    _ignore_unknown_tag as _ignore_unknown_tag,
)
from .package_dirs import (
    _load_package_dir_markers as _load_package_dir_markers,
)
from .package_dirs import (
    _load_package_dir_markers_tracked as _load_package_dir_markers_tracked,
)
from .package_dirs import (
    _mtime_sig as _mtime_sig,
)
from .package_dirs import (
    _package_dir_markers_cached as _package_dir_markers_cached,
)
from .package_dirs import (
    _package_folder_relative_to_config as _package_folder_relative_to_config,
)
from .package_dirs import (
    _PackagesDir as _PackagesDir,
)
from .package_dirs import (
    _PackagesDirLoader as _PackagesDirLoader,
)
from .websocket_api import async_register_commands
from .yaml_edit_values import (
    _apply_edit_action as _apply_edit_action,
)
from .yaml_edit_values import (
    _apply_lovelace_dashboard_action as _apply_lovelace_dashboard_action,
)
from .yaml_edit_values import (
    _apply_lovelace_dashboard_edit as _apply_lovelace_dashboard_edit,
)
from .yaml_edit_values import (
    _apply_single_key_edit as _apply_single_key_edit,
)
from .yaml_edit_values import (
    _confirm_token as _confirm_token,
)
from .yaml_edit_values import (
    _count_reindented_lines as _count_reindented_lines,
)
from .yaml_edit_values import (
    _parse_and_validate_yaml_path as _parse_and_validate_yaml_path,
)
from .yaml_edit_values import (
    _reindent_warning as _reindent_warning,
)
from .yaml_edit_values import (
    _unified_diff as _unified_diff,
)
from .yaml_edit_values import (
    _validate_lovelace_dashboard_path as _validate_lovelace_dashboard_path,
)
from .yaml_edit_values import (
    _walk_lovelace_dashboards as _walk_lovelace_dashboards,
)
from .yaml_editing import (
    YamlEditor,
)
from .yaml_editing import (
    _apply_replace_file as _apply_replace_file,
)
from .yaml_editing import (
    _apply_yaml_key_edit as _apply_yaml_key_edit,
)
from .yaml_editing import (
    _caller_extra_allowed_keys as _caller_extra_allowed_keys,
)
from .yaml_editing import (
    _check_edit_yaml_containment as _check_edit_yaml_containment,
)
from .yaml_editing import (
    _check_packages_key_gate as _check_packages_key_gate,
)
from .yaml_editing import (
    _classify_edit_yaml_target as _classify_edit_yaml_target,
)
from .yaml_editing import (
    _effective_extra_allowed_keys as _effective_extra_allowed_keys,
)
from .yaml_editing import (
    _load_edit_target_data as _load_edit_target_data,
)
from .yaml_editing import (
    _maybe_build_confirm_preview as _maybe_build_confirm_preview,
)
from .yaml_editing import (
    _parse_edit_content as _parse_edit_content,
)
from .yaml_editing import (
    _resolve_post_action as _resolve_post_action,
)
from .yaml_editing import (
    _run_config_check as _run_config_check,
)
from .yaml_editing import (
    _serialize_and_validate_edit as _serialize_and_validate_edit,
)
from .yaml_rt import (
    apply_seq_indent,
    detect_seq_indent,
    make_yaml,
    yaml_dumps,
    yaml_jsonify,
)

_LOGGER = logging.getLogger(__name__)

# Service names
SERVICE_LIST_FILES = "list_files"
SERVICE_READ_FILE = "read_file"
SERVICE_WRITE_FILE = "write_file"
SERVICE_DELETE_FILE = "delete_file"
SERVICE_EDIT_YAML_CONFIG = "edit_yaml_config"
SERVICE_GET_CALLER_TOKEN = "get_caller_token"
SERVICE_GET_ALLOWED_PATHS = "get_allowed_paths"
SERVICE_SET_ALLOWED_PATHS = "set_allowed_paths"
SERVICE_GET_EXTRA_YAML_KEYS = "get_extra_yaml_keys"
SERVICE_SET_EXTRA_YAML_KEYS = "set_extra_yaml_keys"
# Read-only access to pre-#1579 YAML backups in .ha_mcp_tools_backups/, so the
# shared edits-backup interface can list/view/diff/restore them (#1579). These
# historical artifacts predate the fold into the shared store; new writes no
# longer land here.
SERVICE_LIST_LEGACY_BACKUPS = "list_legacy_backups"
SERVICE_READ_LEGACY_BACKUP = "read_legacy_backup"

# Caller-token auth (PR: restrict ha_mcp_tools.* to ha-mcp callers).
# ha-mcp injects this field in every service-call payload; non-ha-mcp callers
# (HA UI, automations, other integrations, the ha_call_service LLM bypass)
# omit it and are rejected with a structured unauthorized response.
CALLER_TOKEN_FIELD = "_ha_mcp_token"
_TOKEN_STORAGE_KEY = f"{DOMAIN}_auth"
_TOKEN_STORAGE_VERSION = 1
_HASS_DATA_TOKEN_KEY = "caller_token"

# User-configurable extra read/write directories (issue #1567). Persisted in a
# SEPARATE Store from the caller token so a corrupt/edited allowlist can never
# affect token bootstrap, and the security credential is never mixed with user
# config. Loaded into hass.data at setup and updated in place by
# set_allowed_paths so enforcement picks up changes with no HA restart.
_ALLOWED_PATHS_STORAGE_KEY = f"{DOMAIN}_allowed_paths"
_ALLOWED_PATHS_STORAGE_VERSION = 1

# User-configurable extra YAML write keys (#1887). Same store-per-concern
# rationale as the allowed paths above: a separate Store, loaded into hass.data
# at setup and updated in place by set_extra_yaml_keys so enforcement picks up
# changes with no HA restart. This is the component-owned half of the setting;
# the ha-mcp server also carries its own HA_MCP_EXTRA_YAML_KEYS, and the
# effective write allowlist is the union of the two (the server reads this store
# via get_extra_yaml_keys). YAML_KEY_DENYLIST members are stripped on the way in
# and re-checked at enforcement, so the store can never widen the deny floor.
_EXTRA_YAML_KEYS_STORAGE_KEY = f"{DOMAIN}_extra_yaml_keys"
_EXTRA_YAML_KEYS_STORAGE_VERSION = 1

# Service schemas
SERVICE_EDIT_YAML_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("file"): cv.string,
        vol.Required("action"): vol.In(["add", "replace", "remove", "replace_file"]),
        vol.Required("yaml_path"): cv.string,
        vol.Optional("content"): cv.string,
        # Back-compat shim: the component reaches users via HACS ahead of the
        # server, so a 0.10.0 component runs against the prior stable server
        # until that server's next release. Pre-7.9.0 servers still send
        # "backup": true on every edit_yaml_config call; this strict
        # (PREVENT_EXTRA) schema would reject it ("extra keys not allowed")
        # and break ha_config_set_yaml for everyone in that window. Accept and
        # ignore it. Removable once the minimum supported server is >= 7.9.0.
        vol.Optional("backup"): cv.boolean,
        # Caller-provided list of PACKAGES_ONLY_YAML_KEYS that the
        # caller wants the component to reject. Empty list (the
        # default) means no extra restrictions on top of the
        # component's existing allowlist. ha-mcp populates this from
        # its per-key Settings flags so the wrapper and the component
        # stay symmetric — if ha-mcp's flag is off, the component also
        # refuses, defending against bypass attempts.
        vol.Optional("disabled_packages_keys", default=list): vol.All(
            cv.ensure_list, [cv.string]
        ),
        # Caller-provided extra top-level keys the operator has opted into
        # on top of ALLOWED_YAML_KEYS (#1887). Additive only: the handler
        # filters YAML_KEY_DENYLIST out before use, so a caller cannot
        # unlock a trust-boundary key by sending it here. Empty list (the
        # default) means the built-in allowlist applies unchanged, which
        # is also what a caller that predates this field produces.
        vol.Optional("extra_allowed_keys", default=list): vol.All(
            cv.ensure_list, [cv.string]
        ),
        # Two-step preview/confirm flow (#1720). Both optional with
        # old-behavior defaults so a pre-confirm-flow server (which never
        # sends them) still gets an immediate write.
        vol.Optional("require_confirm", default=False): cv.boolean,
        vol.Optional("confirm_token"): cv.string,
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_LIST_FILES_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional("pattern"): cv.string,
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_READ_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional("tail_lines"): vol.Coerce(int),
        # When set, also return the round-trip text of the YAML subtree at this
        # dotted path under ``subtree`` (used by ha-mcp's per-edit auto-backup
        # to snapshot the prior value before ha_config_set_yaml edits it, #1579).
        vol.Optional("yaml_path"): cv.string,
        # With ``yaml_path``, also return that subtree as JSON-safe parsed data
        # under ``parsed`` (ha_config_get_yaml's include_parsed, #1788). HA tags
        # are rendered to source form, never resolved.
        vol.Optional("include_parsed", default=False): cv.boolean,
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_WRITE_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Required("content"): cv.string,
        vol.Optional("overwrite", default=False): cv.boolean,
        vol.Optional("create_dirs", default=True): cv.boolean,
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_DELETE_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

# get_caller_token is the bootstrap surface: ha-mcp does not know the token
# yet on first run, so this service intentionally does NOT require the token
# itself. HA's default admin-auth still applies to the service call.
SERVICE_GET_CALLER_TOKEN_SCHEMA = vol.Schema(
    {
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

# get_allowed_paths / set_allowed_paths back the ha-mcp settings UI's custom
# filesystem-directory editor (issues #1567, #1586). Both are caller-token +
# admin gated. set_allowed_paths receives the FULL replacement list (mirrors how
# disabled_packages_keys sends the whole set each call); the handler validates
# and drops any entry that hits the deny floor, or that escapes the config dir
# without being one of the fixed HAOS sibling-volume roots (#1586).
SERVICE_GET_ALLOWED_PATHS_SCHEMA = vol.Schema(
    {
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_SET_ALLOWED_PATHS_SCHEMA = vol.Schema(
    {
        vol.Optional("paths", default=list): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

# get_extra_yaml_keys / set_extra_yaml_keys back the component's own options
# flow and the ha-mcp settings UI (#1887). Both are caller-token + admin gated,
# matching the allowed-paths pair. set_extra_yaml_keys receives the FULL
# replacement list; the handler strips whitespace/empties and drops any
# YAML_KEY_DENYLIST member, reporting drops in ``rejected``.
SERVICE_GET_EXTRA_YAML_KEYS_SCHEMA = vol.Schema(
    {
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_SET_EXTRA_YAML_KEYS_SCHEMA = vol.Schema(
    {
        vol.Optional("keys", default=list): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_LIST_LEGACY_BACKUPS_SCHEMA = vol.Schema(
    {
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_READ_LEGACY_BACKUP_SCHEMA = vol.Schema(
    {
        vol.Required("filename"): cv.string,
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

# Files that are allowed to be read (even if not in ALLOWED_READ_DIRS)

# Root-level log files that the read service tails by default.

# Default tail lines for log files


async def _load_or_create_caller_token(hass: HomeAssistant) -> str:
    """Return the persisted caller token, generating + saving one on first use.

    The token authorizes a caller as ha-mcp. It's stored under
    ``.storage/ha_mcp_tools_auth`` and remains stable across restarts so the
    ha-mcp server can re-bootstrap without user intervention.

    A corrupt/unreadable store must NOT propagate out of async_setup_entry and
    take down the integration: on a load failure we log and fall through to
    generating a fresh token (same path as first run), overwriting the bad
    blob. ha-mcp transparently re-bootstraps the new token via its
    unauthorized-retry, so the only cost is a one-time token rotation.
    """
    store: Store = Store(hass, _TOKEN_STORAGE_VERSION, _TOKEN_STORAGE_KEY)
    try:
        data = await store.async_load()
    except Exception:
        _LOGGER.warning(
            "ha_mcp_tools: could not load the caller-token store; generating a "
            "fresh token and overwriting it.",
            exc_info=True,
        )
        data = None
    if isinstance(data, dict):
        existing = data.get("token")
        if isinstance(existing, str) and existing:
            return existing
    token = secrets.token_urlsafe(32)
    await store.async_save({"token": token})
    return token


async def _load_allowed_paths(hass: HomeAssistant) -> list[str]:
    """Return the persisted user-configurable extra directories.

    Each stored entry is re-validated through :func:`_normalize_extra_dir`, so a
    hand-edited or corrupted store can never load a traversal / deny-floor /
    out-of-config entry into ``hass.data`` (defense in depth — the deny floor is
    also re-checked at enforcement time). Anything dropped is logged so a
    "my custom directories disappeared" case is one grep away. Empty list on
    first run or a malformed store — fail safe to "no extra access" rather than
    raising, mirroring :func:`_load_or_create_caller_token`.
    """
    store: Store = Store(
        hass, _ALLOWED_PATHS_STORAGE_VERSION, _ALLOWED_PATHS_STORAGE_KEY
    )
    try:
        data = await store.async_load()
    except Exception:
        # Honour the documented fail-safe contract: a corrupt/unreadable
        # allowed-paths blob must NOT propagate out of async_setup_entry and
        # take down the integration. Log loudly and fall back to no extra
        # access.
        _LOGGER.warning(
            "ha_mcp_tools: could not load the allowed-paths store; ignoring it "
            "and granting no extra directories.",
            exc_info=True,
        )
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("paths")
    if not isinstance(raw, list):
        if raw is not None:
            _LOGGER.warning(
                "ha_mcp_tools allowed-paths store is malformed (paths is %s, "
                "expected list); ignoring it.",
                type(raw).__name__,
            )
        return []
    config_dir = Path(hass.config.config_dir)
    normalized: list[str] = []
    dropped: list[Any] = []
    for entry in raw:
        norm = (
            _normalize_extra_dir(entry, config_dir) if isinstance(entry, str) else None
        )
        if norm is None:
            dropped.append(entry)
        elif norm not in normalized:
            normalized.append(norm)
    if dropped:
        _LOGGER.warning(
            "ha_mcp_tools: dropped %d invalid entr%s from the persisted "
            "allowed-paths store: %r",
            len(dropped),
            "y" if len(dropped) == 1 else "ies",
            dropped,
        )
    return normalized


async def _save_allowed_paths(hass: HomeAssistant, paths: list[str]) -> None:
    """Persist the user-configurable extra directories to .storage."""
    store: Store = Store(
        hass, _ALLOWED_PATHS_STORAGE_VERSION, _ALLOWED_PATHS_STORAGE_KEY
    )
    await store.async_save({"paths": paths})


def _normalize_extra_yaml_keys(raw: Any) -> tuple[list[str], list[Any]]:
    """Clean a candidate extra-YAML-keys list into ``(kept, dropped)`` (#1887).

    Mirrors the server's ``parse_extra_yaml_write_keys`` (strip whitespace, drop
    empties, dedup, sort) and additionally drops any ``YAML_KEY_DENYLIST``
    member, so the component store can never widen the deny floor. Non-string
    and blank entries are dropped too. ``dropped`` collects every rejected
    entry for reporting/logging. Case is preserved: HA's own top-level domain
    lookup is exact-case, so a mis-cased key never reaches a real integration.
    """
    kept: list[str] = []
    dropped: list[Any] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, str):
            dropped.append(entry)
            continue
        key = entry.strip()
        if not key or key in YAML_KEY_DENYLIST:
            dropped.append(entry)
            continue
        if key not in seen:
            seen.add(key)
            kept.append(key)
    return sorted(kept), dropped


async def _load_extra_yaml_keys(hass: HomeAssistant) -> list[str]:
    """Return the persisted user-configurable extra YAML write keys (#1887).

    Fail-safe like :func:`_load_allowed_paths`: a corrupt/unreadable or
    hand-edited store never propagates out of ``async_setup_entry``. Every entry
    is re-validated through :func:`_normalize_extra_yaml_keys`, so a denylisted
    or malformed key can never load into ``hass.data`` (defense in depth - the
    deny floor is also re-checked at enforcement). Empty list on first run or a
    malformed store.
    """
    store: Store = Store(
        hass, _EXTRA_YAML_KEYS_STORAGE_VERSION, _EXTRA_YAML_KEYS_STORAGE_KEY
    )
    try:
        data = await store.async_load()
    except Exception:
        _LOGGER.warning(
            "ha_mcp_tools: could not load the extra-YAML-keys store; ignoring "
            "it and granting no extra write keys.",
            exc_info=True,
        )
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("keys")
    if raw is not None and not isinstance(raw, list):
        _LOGGER.warning(
            "ha_mcp_tools extra-YAML-keys store is malformed (keys is %s, "
            "expected list); ignoring it.",
            type(raw).__name__,
        )
        return []
    kept, dropped = _normalize_extra_yaml_keys(raw)
    if dropped:
        _LOGGER.warning(
            "ha_mcp_tools: dropped %d invalid entr%s from the persisted "
            "extra-YAML-keys store: %r",
            len(dropped),
            "y" if len(dropped) == 1 else "ies",
            dropped,
        )
    return kept


async def _save_extra_yaml_keys(hass: HomeAssistant, keys: list[str]) -> None:
    """Persist the user-configurable extra YAML write keys to .storage."""
    store: Store = Store(
        hass, _EXTRA_YAML_KEYS_STORAGE_VERSION, _EXTRA_YAML_KEYS_STORAGE_KEY
    )
    await store.async_save({"keys": keys})


async def _apply_allowed_paths(
    hass: HomeAssistant,
    raw_paths: Any,
    *,
    guard: Callable[[], Awaitable[None]] | None = None,
) -> tuple[list[str], list[Any]]:
    """Normalize, persist, and hot-swap the extra directories (#1567, #1887).

    Shared by the ``set_allowed_paths`` service and the tools-entry options flow
    so both edit the store through one validated path. Each entry runs through
    :func:`_normalize_extra_dir`; traversal / out-of-config / deny-floor entries
    are dropped into ``rejected``. Persists to .storage and updates hass.data so
    enforcement applies live. Returns ``(kept, rejected)``.
    """
    from .file_paths import normalize_allowed_paths

    normalized, rejected = await hass.async_add_executor_job(
        normalize_allowed_paths, Path(hass.config.config_dir), raw_paths
    )
    if guard is not None:
        await guard()
    await _save_allowed_paths(hass, normalized)
    hass.data.setdefault(DOMAIN, {})[_HASS_DATA_ALLOWED_PATHS_KEY] = normalized
    return normalized, rejected


async def _apply_extra_yaml_keys(
    hass: HomeAssistant, raw_keys: Any
) -> tuple[list[str], list[Any]]:
    """Normalize, persist, and hot-swap the extra YAML write keys (#1887).

    Shared by the ``set_extra_yaml_keys`` service and the tools-entry options
    flow. Delegates validation to :func:`_normalize_extra_yaml_keys` (strip,
    dedup, sort, drop denylist). Persists to .storage and updates hass.data so
    enforcement applies live. Returns ``(kept, rejected)``.
    """
    normalized, rejected = _normalize_extra_yaml_keys(raw_keys)
    await _save_extra_yaml_keys(hass, normalized)
    hass.data.setdefault(DOMAIN, {})[_HASS_DATA_EXTRA_YAML_KEYS_KEY] = normalized
    return normalized, rejected


def _unauthorized_response(service_name: str, **extra: Any) -> dict[str, Any]:
    """Structured 'unauthorized' response.

    ha-mcp clients detect this via ``error_code == "unauthorized"`` and
    re-fetch the token before retrying.
    """
    return {
        "success": False,
        "error": (
            f"Unauthorized: caller token missing or invalid for "
            f"{DOMAIN}.{service_name}. This service is restricted to the "
            "ha-mcp server; other callers should not invoke it directly."
        ),
        "error_code": "unauthorized",
        **extra,
    }


def _caller_token_ok(hass: HomeAssistant, call: ServiceCall) -> bool:
    """Return True if the caller presented the configured token."""
    domain_data = hass.data.get(DOMAIN)
    expected = (
        domain_data.get(_HASS_DATA_TOKEN_KEY) if isinstance(domain_data, dict) else None
    )
    presented = call.data.get(CALLER_TOKEN_FIELD)
    # token_urlsafe(32) is 256-bit; the timing-side-channel risk is already
    # negligible, but secrets.compare_digest is the right reflex regardless.
    if not isinstance(expected, str) or not isinstance(presented, str):
        return False
    return secrets.compare_digest(expected, presented)


async def _caller_is_admin(hass: HomeAssistant, call: ServiceCall) -> bool:
    """Return True if the caller is an admin user (or a no-user-context call).

    HA's service registry has no built-in admin requirement — `Service`
    has no admin flag, `async_call` performs no permission check, and
    WS / REST `call_service` lack `@require_admin`. Gate explicitly here.

    Calls without `context.user_id` (system-internal events) are treated
    as trusted, matching HA's `async_admin_handler_factory` convention.
    The supported deployment shapes all use admin tokens: addon's
    SUPERVISOR_TOKEN maps to HA's `hassio_user`, which HA force-promotes
    into `GROUP_ID_ADMIN` (hassio/__init__.py); standalone Docker/pip
    deployments use a user-supplied admin LLAT.
    """
    if not call.context.user_id:
        return True
    user = await hass.auth.async_get_user(call.context.user_id)
    return user is not None and bool(user.is_admin)


# config_path -> (signature of the files last read, markers found in them).
# Module-level: the folder binding is per config dir, and HA runs one per
# process. Concurrent first-callers may each miss and parse once before the
# entry lands — bounded, self-healing, and cheaper than holding a lock across
# executor threads.


# Pre-#1579 YAML backups live here (config-root dotdir, never under www/ —
# GHSA-g39v-cvjh-8fpf). New writes no longer land here; these are read-only
# historical artifacts surfaced through the shared edits-backup interface.


def _build_edit_yaml_config_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[dict[str, Any]]]:
    """Build and return the async handle_edit_yaml_config handler.

    Extracted to module level so it can be tested without registering
    the full integration.
    """
    editor = YamlEditor(service_file_access(hass))

    async def handle_edit_yaml_config(call: ServiceCall) -> dict[str, Any]:
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_EDIT_YAML_CONFIG)
        try:
            return await editor.edit(
                dict(call.data), context=getattr(call, "context", None)
            )
        except ToolFailure as error:
            return error.data

    return handle_edit_yaml_config


def _migrate_legacy_backup_dir(config_dir: Path) -> tuple[int, int]:
    """Move pre-fix backups out of www/ (GHSA-g39v-cvjh-8fpf).

    Pre-fix versions wrote backups under www/yaml_backups/, which HA serves
    unauthenticated at /local/. Move .bak files into the new
    .ha_mcp_tools_backups/ directory and remove the old directory if empty.
    Returns (moved, failed) — counts of files successfully relocated and
    files that could not be moved (left in legacy_dir, still exposed).
    """
    legacy_dir = config_dir / "www" / "yaml_backups"
    if not legacy_dir.is_dir():
        return 0, 0

    new_dir = config_dir / _LEGACY_BACKUP_DIRNAME
    new_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    failed = 0
    for src in legacy_dir.iterdir():
        # Only migrate regular .bak files. Skip directories, symlinks,
        # sockets/fifos, and any user-deposited stray files.
        if not src.is_file() or src.is_symlink() or src.suffix != ".bak":
            continue

        # Pick a non-colliding destination name. If <name>.bak exists, try
        # <name>.legacy.bak, then .legacy1.bak, .legacy2.bak, etc. Required
        # because Path.rename / shutil.move overwrite the destination
        # silently on POSIX, which would lose data.
        dest = new_dir / src.name
        if dest.exists():
            dest = new_dir / f"{src.stem}.legacy{src.suffix}"
            counter = 1
            while dest.exists():
                dest = new_dir / f"{src.stem}.legacy{counter}{src.suffix}"
                counter += 1

        try:
            # shutil.move falls back to copy+unlink on EXDEV (cross-device),
            # which Path.rename does not handle — required for setups where
            # /config/www is a separate mount (Docker bind, LXC, NFS).
            shutil.move(str(src), str(dest))
            moved += 1
        except OSError as err:
            failed += 1
            _LOGGER.error(
                "Failed to migrate %s out of www/: %s. File remains "
                "exposed via /local/yaml_backups/.",
                src.name,
                err,
            )

    # Remove the legacy dir if we emptied it. Narrow the swallowed errno
    # to ENOTEMPTY (user-dropped non-.bak files block removal — fine);
    # surface anything else (permissions, read-only FS, etc.).
    try:
        legacy_dir.rmdir()
    except OSError as err:
        # ENOTEMPTY on Linux/APFS; EEXIST on some pre-APFS macOS filesystems.
        if err.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            _LOGGER.warning(
                "Could not remove legacy backup dir %s: [%s] %s",
                legacy_dir,
                type(err).__name__,
                err,
            )

    return moved, failed


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry, dispatching on its entry type.

    The integration serves two entry types under one domain: the ``tools``
    services entry (the original component) and the in-process ``server`` entry
    (issue #1527). A missing ``entry_type`` means ``tools`` so pre-existing
    entries keep working across the component update with no migration.
    """
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SERVER:
        from .embedded_entry import async_setup_server_entry

        return await async_setup_server_entry(hass, entry)
    return await _async_setup_tools_entry(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry, dispatching on its entry type."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SERVER:
        from .embedded_entry import async_unload_server_entry

        return await async_unload_server_entry(hass, entry)
    return await _async_unload_tools_entry(hass, entry)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config-entry removal, dispatching on its entry type.

    The server entry releases the credentials an older release provisioned; the
    tools entry has nothing to clean up beyond what unload already did.
    """
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SERVER:
        from .embedded_entry import async_remove_server_entry

        await async_remove_server_entry(hass, entry)


def _build_list_files_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Keep the service credential boundary around the component file owner."""
    owner = service_file_access(hass)

    async def handle_list_files(call: ServiceCall) -> ServiceResponse:
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_LIST_FILES, files=[])
        try:
            return await owner.list_files(
                path=call.data["path"], pattern=call.data.get("pattern")
            )
        except ToolFailure as error:
            return error.data

    return handle_list_files


def _build_read_file_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Keep the service credential boundary around the component file owner."""
    owner = service_file_access(hass)

    async def handle_read_file(call: ServiceCall) -> ServiceResponse:
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_READ_FILE)
        try:
            return await owner.read_file(
                path=call.data["path"],
                tail_lines=call.data.get("tail_lines"),
                yaml_path=call.data.get("yaml_path"),
                include_parsed=bool(call.data.get("include_parsed", False)),
            )
        except ToolFailure as error:
            return error.data

    return handle_read_file


def _build_write_file_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Keep the service credential boundary around the component file owner."""
    owner = service_file_access(hass)

    async def handle_write_file(call: ServiceCall) -> ServiceResponse:
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_WRITE_FILE)
        try:
            return await owner.write_file(
                path=call.data["path"],
                content=call.data["content"],
                overwrite=call.data.get("overwrite", False),
                create_dirs=call.data.get("create_dirs", True),
            )
        except ToolFailure as error:
            return error.data

    return handle_write_file


def _build_delete_file_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Keep the service credential boundary around the component file owner."""
    owner = service_file_access(hass)

    async def handle_delete_file(call: ServiceCall) -> ServiceResponse:
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_DELETE_FILE)
        try:
            return await owner.delete_file(path=call.data["path"])
        except ToolFailure as error:
            return error.data

    return handle_delete_file


def _build_get_caller_token_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Build the handle_get_caller_token service handler."""

    async def handle_get_caller_token(call: ServiceCall) -> ServiceResponse:
        """Return the caller-auth token to the ha-mcp bootstrap caller.

        Admin-gated explicitly (see `_caller_is_admin`): HA's service
        registry has no built-in admin requirement, so the gate prevents
        a non-admin caller from bootstrapping the token. The supported
        deployments (addon supervisor user, standalone admin LLAT) all
        pass this gate.
        """
        if not await _caller_is_admin(hass, call):
            return {
                "success": False,
                "error_code": "unauthorized",
                "error": "ha_mcp_tools.get_caller_token requires admin auth.",
            }
        domain_data = hass.data.get(DOMAIN)
        token = (
            domain_data.get(_HASS_DATA_TOKEN_KEY)
            if isinstance(domain_data, dict)
            else None
        )
        if not isinstance(token, str) or not token:
            return {
                "success": False,
                "error_code": "not_initialized",
                "error": (
                    "Caller token not initialized — integration may not have "
                    "completed setup. Reload the ha_mcp_tools integration."
                ),
            }
        # Report the manifest version so ha-mcp can enforce a minimum
        # compatible component version. The integration loader reads
        # ``manifest.json`` once at startup; ``async_get_integration``
        # is cheap and avoids hard-coding the version twice.
        # Pathological-but-belt-and-suspenders: a corrupted manifest
        # would otherwise surface as HA's generic handler error rather
        # than the actionable structured response shape this service
        # uses everywhere else.
        try:
            integration = await async_get_integration(hass, DOMAIN)
            if integration.version is None:
                # Reads as None rather than raising; an unreadable version and
                # an absent one deserve the same answer.
                raise ValueError("the manifest carries no version")
            version = str(integration.version)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Could not read ha_mcp_tools manifest version for "
                "get_caller_token response: %s",
                exc,
            )
            return {
                "success": False,
                "error_code": "manifest_unreadable",
                "error": (
                    "ha_mcp_tools manifest version could not be read. "
                    "Reinstall the integration via HACS."
                ),
            }
        return {
            "success": True,
            "token": token,
            "version": version,
        }

    return handle_get_caller_token


def _build_get_allowed_paths_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Build the handle_get_allowed_paths service handler."""

    async def handle_get_allowed_paths(call: ServiceCall) -> ServiceResponse:
        """Return the user-configurable extra directories plus the built-in
        allowlists and the non-overridable deny floor (issue #1567).

        Backs the ha-mcp settings UI's custom filesystem-directory editor.
        Caller-token + admin gated (matching get_caller_token): it reveals the
        filesystem layout, so it is not a public read.
        """
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_GET_ALLOWED_PATHS, paths=[])
        if not await _caller_is_admin(hass, call):
            return {
                "success": False,
                "error_code": "unauthorized",
                "error": "ha_mcp_tools.get_allowed_paths requires admin auth.",
                "paths": [],
            }
        return {
            "success": True,
            "paths": _current_extra_dirs(hass),
            "builtin_read_dirs": list(ALLOWED_READ_DIRS),
            "builtin_write_dirs": list(ALLOWED_WRITE_DIRS),
            "deny_floor": sorted(DENY_PATH_SEGMENTS | DENY_READ_BASENAMES),
        }

    return handle_get_allowed_paths


def _build_set_allowed_paths_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Build the handle_set_allowed_paths service handler."""

    async def handle_set_allowed_paths(call: ServiceCall) -> ServiceResponse:
        """Replace the user-configurable extra directories (issues #1567, #1586).

        Receives the FULL replacement list. Each entry is validated and
        normalized; entries that use traversal, escape the config directory, or
        hit the non-overridable deny floor are dropped and reported in
        ``rejected``. Config-relative dirs (#1567) and the fixed HAOS
        sibling-volume roots (#1586 — see ``const.ALLOWED_VOLUME_ROOTS``) are
        accepted; any other absolute path is dropped. Persists to .storage AND
        updates hass.data so enforcement applies live with no HA restart.
        Caller-token + admin gated.
        """
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_SET_ALLOWED_PATHS, paths=[])
        if not await _caller_is_admin(hass, call):
            return {
                "success": False,
                "error_code": "unauthorized",
                "error": "ha_mcp_tools.set_allowed_paths requires admin auth.",
                "paths": [],
            }
        normalized, rejected = await _apply_allowed_paths(
            hass, call.data.get("paths", [])
        )
        _LOGGER.info(
            "Updated ha_mcp_tools custom filesystem directories: %s (%d rejected)",
            normalized,
            len(rejected),
        )
        return {
            "success": True,
            "paths": normalized,
            "rejected": rejected,
        }

    return handle_set_allowed_paths


def _build_get_extra_yaml_keys_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Build the handle_get_extra_yaml_keys service handler (#1887)."""

    async def handle_get_extra_yaml_keys(call: ServiceCall) -> ServiceResponse:
        """Return the component-configured extra YAML write keys plus the
        non-overridable deny floor.

        Backs the component's own options flow and the ha-mcp settings UI, and
        is how the server reads this store to union it with its own
        ``HA_MCP_EXTRA_YAML_KEYS``. Caller-token + admin gated, matching
        get_allowed_paths.
        """
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_GET_EXTRA_YAML_KEYS, keys=[])
        if not await _caller_is_admin(hass, call):
            return {
                "success": False,
                "error_code": "unauthorized",
                "error": "ha_mcp_tools.get_extra_yaml_keys requires admin auth.",
                "keys": [],
            }
        return {
            "success": True,
            "keys": _current_extra_yaml_keys(hass),
            "deny_floor": sorted(YAML_KEY_DENYLIST),
        }

    return handle_get_extra_yaml_keys


def _build_set_extra_yaml_keys_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Build the handle_set_extra_yaml_keys service handler (#1887)."""

    async def handle_set_extra_yaml_keys(call: ServiceCall) -> ServiceResponse:
        """Replace the component-configured extra YAML write keys.

        Receives the FULL replacement list. Each entry is stripped and
        validated; blanks and ``YAML_KEY_DENYLIST`` members are dropped and
        reported in ``rejected``. Persists to .storage AND updates hass.data so
        enforcement applies live with no HA restart. Caller-token + admin gated.
        """
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_SET_EXTRA_YAML_KEYS, keys=[])
        if not await _caller_is_admin(hass, call):
            return {
                "success": False,
                "error_code": "unauthorized",
                "error": "ha_mcp_tools.set_extra_yaml_keys requires admin auth.",
                "keys": [],
            }
        normalized, rejected = await _apply_extra_yaml_keys(
            hass, call.data.get("keys", [])
        )
        _LOGGER.info(
            "Updated ha_mcp_tools extra YAML write keys: %s (%d rejected)",
            normalized,
            len(rejected),
        )
        return {
            "success": True,
            "keys": normalized,
            "rejected": rejected,
        }

    return handle_set_extra_yaml_keys


def _build_list_legacy_backups_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Build the handle_list_legacy_backups service handler."""
    config_dir = Path(hass.config.config_dir)
    legacy_backup_dir = config_dir / _LEGACY_BACKUP_DIRNAME

    async def handle_list_legacy_backups(call: ServiceCall) -> ServiceResponse:
        """List pre-#1579 YAML backups in .ha_mcp_tools_backups/ (read-only)."""
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_LIST_LEGACY_BACKUPS, backups=[])
        try:
            backups = await hass.async_add_executor_job(
                _list_legacy_backups_sync, legacy_backup_dir
            )
        except OSError as err:
            _LOGGER.error("Error listing legacy backups: %s", err)
            return {"success": False, "error": str(err), "backups": []}
        return {"success": True, "backups": backups, "count": len(backups)}

    return handle_list_legacy_backups


def _build_read_legacy_backup_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Build the handle_read_legacy_backup service handler."""
    config_dir = Path(hass.config.config_dir)

    async def handle_read_legacy_backup(call: ServiceCall) -> ServiceResponse:
        """Read a legacy backup through its confined producer."""
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_READ_LEGACY_BACKUP)
        result: ServiceResponse = await hass.async_add_executor_job(
            read_legacy_backup, config_dir, call.data["filename"]
        )
        return result

    return handle_read_legacy_backup


async def _async_setup_tools_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the File & YAML services (tools entry) from a config entry."""
    config_dir = Path(hass.config.config_dir)

    # Bootstrap the caller-auth token. Generated on first setup, persisted
    # to .storage/ha_mcp_tools_auth, cached in hass.data for fast handler
    # access. ha-mcp fetches it via the get_caller_token service.
    caller_token = await _load_or_create_caller_token(hass)
    hass.data.setdefault(DOMAIN, {})[_HASS_DATA_TOKEN_KEY] = caller_token

    # Load the user-configurable extra read/write directories (issue #1567)
    # into hass.data so the file handlers read them with no I/O. set_allowed_paths
    # updates this in place, so changes apply live (no HA restart).
    hass.data.setdefault(DOMAIN, {})[
        _HASS_DATA_ALLOWED_PATHS_KEY
    ] = await _load_allowed_paths(hass)

    # Load the component-configured extra YAML write keys (#1887) into hass.data
    # so enforcement reads them with no I/O. set_extra_yaml_keys updates this in
    # place, so changes apply live (no HA restart).
    hass.data.setdefault(DOMAIN, {})[
        _HASS_DATA_EXTRA_YAML_KEYS_KEY
    ] = await _load_extra_yaml_keys(hass)

    file_access = FileAccess(hass, entry=entry)
    hass.data[DOMAIN][DATA_FILE_ACCESS] = file_access

    async def stop_file_access(_event: Any) -> None:
        await file_access.async_stop()

    entry.async_on_unload(
        hass.bus.async_listen_once("homeassistant_stop", stop_file_access)
    )

    # One-time migration of pre-fix YAML backups out of the publicly-served
    # www/ directory (GHSA-g39v-cvjh-8fpf). Wrapped so a migration failure
    # cannot prevent the integration from loading — the integration's
    # normal value (file ops, edit_yaml_config) is unaffected by this.
    try:
        moved, failed = await hass.async_add_executor_job(
            _migrate_legacy_backup_dir, config_dir
        )
    except Exception as err:  # noqa: BLE001
        # Defensive: a migration failure must not block setup_entry, since
        # the integration's normal value (file ops, edit_yaml_config) is
        # unaffected by whether old backups got relocated.
        _LOGGER.error(
            "GHSA-g39v-cvjh-8fpf migration failed: %s. Pre-fix backups "
            "may still be present in www/yaml_backups/ and reachable "
            "via /local/yaml_backups/ — manual cleanup required.",
            err,
        )
        moved, failed = 0, 0

    if moved or failed:
        if failed:
            heading = (
                f"Migrated {moved} YAML backup file(s); **{failed} could "
                "not be moved** and remain in `www/yaml_backups/`, still "
                "reachable without authentication via `/local/yaml_backups/`. "
                "Move or delete them manually before continuing."
            )
        else:
            heading = (
                f"Moved {moved} YAML backup file(s) from `www/yaml_backups/` "
                "to `.ha_mcp_tools_backups/`. The previous location was "
                "reachable without authentication via `/local/yaml_backups/`."
            )
        message = (
            f"{heading} See "
            "[GHSA-g39v-cvjh-8fpf](https://github.com/homeassistant-ai/ha-mcp/security/advisories/GHSA-g39v-cvjh-8fpf). "
            "**Rotate any secrets** that appeared in those YAML files "
            "(MQTT/REST credentials, webhook IDs, `shell_command` "
            "definitions, geofence coordinates). If you version-control "
            "your Home Assistant config, also add `.ha_mcp_tools_backups/` "
            "to your `.gitignore` so future backups are not committed."
        )
        _LOGGER.error(message)
        try:
            persistent_notification.async_create(
                hass,
                message,
                title="HA MCP Tools — credential exposure (GHSA-g39v-cvjh-8fpf)",
                notification_id="ha_mcp_tools_ghsa_g39v_cvjh_8fpf",
            )
        except Exception as err:  # noqa: BLE001
            # Defensive: log line above is the source of truth; the
            # notification is best-effort UX and must not block setup.
            _LOGGER.warning(
                "Could not create persistent notification for "
                "GHSA-g39v-cvjh-8fpf migration: [%s] %s",
                type(err).__name__,
                err,
            )

    # Register all services with response support
    for service, schema, build_handler in (
        (
            SERVICE_EDIT_YAML_CONFIG,
            SERVICE_EDIT_YAML_CONFIG_SCHEMA,
            _build_edit_yaml_config_handler,
        ),
        (SERVICE_LIST_FILES, SERVICE_LIST_FILES_SCHEMA, _build_list_files_handler),
        (SERVICE_READ_FILE, SERVICE_READ_FILE_SCHEMA, _build_read_file_handler),
        (SERVICE_WRITE_FILE, SERVICE_WRITE_FILE_SCHEMA, _build_write_file_handler),
        (SERVICE_DELETE_FILE, SERVICE_DELETE_FILE_SCHEMA, _build_delete_file_handler),
        (
            SERVICE_GET_CALLER_TOKEN,
            SERVICE_GET_CALLER_TOKEN_SCHEMA,
            _build_get_caller_token_handler,
        ),
        (
            SERVICE_GET_ALLOWED_PATHS,
            SERVICE_GET_ALLOWED_PATHS_SCHEMA,
            _build_get_allowed_paths_handler,
        ),
        (
            SERVICE_SET_ALLOWED_PATHS,
            SERVICE_SET_ALLOWED_PATHS_SCHEMA,
            _build_set_allowed_paths_handler,
        ),
        (
            SERVICE_GET_EXTRA_YAML_KEYS,
            SERVICE_GET_EXTRA_YAML_KEYS_SCHEMA,
            _build_get_extra_yaml_keys_handler,
        ),
        (
            SERVICE_SET_EXTRA_YAML_KEYS,
            SERVICE_SET_EXTRA_YAML_KEYS_SCHEMA,
            _build_set_extra_yaml_keys_handler,
        ),
        (
            SERVICE_LIST_LEGACY_BACKUPS,
            SERVICE_LIST_LEGACY_BACKUPS_SCHEMA,
            _build_list_legacy_backups_handler,
        ),
        (
            SERVICE_READ_LEGACY_BACKUP,
            SERVICE_READ_LEGACY_BACKUP_SCHEMA,
            _build_read_legacy_backup_handler,
        ),
    ):
        hass.services.async_register(
            DOMAIN,
            service,
            build_handler(hass),
            schema=schema,
            supports_response=SupportsResponse.ONLY,
        )

    # Register the in-process ha_mcp_tools/* WebSocket commands (info + search)
    # the server calls behind a capability gate. Idempotent, and independent of
    # the caller-token gate above — HA core authenticates the WS connection and
    # @require_admin gates each command (see websocket_api.py). The server
    # (embedded) entry registers the same surface (#2289), so a server-only
    # install has it as well; on a dual-entry install both entries register it
    # and the second registration simply overwrites the first.
    async_register_commands(hass)

    # Entry-level finalization: pick up the #1853 rename on existing installs and
    # give the tools entry a device. Both are cosmetic to the integration's core
    # value; the device's manifest version read degrades rather than blocking
    # setup.
    # Retitle an entry still carrying the pre-rename default; a user-customized
    # title is left untouched (only the exact old default migrates). The tools
    # entry registers no update listener, so this async_update_entry cannot
    # trigger a reload loop — and the guard is idempotent regardless (the title
    # no longer matches on the next setup).
    if entry.title == TOOLS_ENTRY_LEGACY_TITLE:
        hass.config_entries.async_update_entry(entry, title=TOOLS_ENTRY_TITLE)

    await async_register_entry_device(
        hass, entry, name=TOOLS_ENTRY_TITLE, model="File & YAML editing services"
    )

    _LOGGER.info("HA-MCP File & YAML Tools initialized with file management services")
    return True


async def _async_unload_tools_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the File & YAML services (tools entry) config entry."""
    # Remove all services
    hass.services.async_remove(DOMAIN, SERVICE_EDIT_YAML_CONFIG)
    hass.services.async_remove(DOMAIN, SERVICE_LIST_FILES)
    hass.services.async_remove(DOMAIN, SERVICE_READ_FILE)
    hass.services.async_remove(DOMAIN, SERVICE_WRITE_FILE)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_FILE)
    hass.services.async_remove(DOMAIN, SERVICE_GET_CALLER_TOKEN)
    hass.services.async_remove(DOMAIN, SERVICE_GET_ALLOWED_PATHS)
    hass.services.async_remove(DOMAIN, SERVICE_SET_ALLOWED_PATHS)
    hass.services.async_remove(DOMAIN, SERVICE_GET_EXTRA_YAML_KEYS)
    hass.services.async_remove(DOMAIN, SERVICE_SET_EXTRA_YAML_KEYS)
    hass.services.async_remove(DOMAIN, SERVICE_LIST_LEGACY_BACKUPS)
    hass.services.async_remove(DOMAIN, SERVICE_READ_LEGACY_BACKUP)

    # Drop the cached token + allowlist + extra YAML keys from hass.data so a
    # subsequent setup_entry re-reads from storage (covers the
    # reload-after-rotate path). The WS command surface is deliberately NOT
    # unregistered here — it is shared with the server entry and HA offers no
    # unregister (#2292); see ``websocket_api.async_register_commands``.
    domain_data = hass.data.get(DOMAIN)
    if isinstance(domain_data, dict):
        file_access = domain_data.get(DATA_FILE_ACCESS)
        if isinstance(file_access, FileAccess):
            await file_access.async_stop()
            if domain_data.get(DATA_FILE_ACCESS) is file_access:
                domain_data.pop(DATA_FILE_ACCESS)
        domain_data.pop(_HASS_DATA_TOKEN_KEY, None)
        domain_data.pop(_HASS_DATA_ALLOWED_PATHS_KEY, None)
        domain_data.pop(_HASS_DATA_EXTRA_YAML_KEYS_KEY, None)
    return True
