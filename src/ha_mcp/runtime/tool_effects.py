"""Argument-specific effects shared by every invocation facade."""

from collections.abc import Callable
from typing import Any, NamedTuple


class ReadOnlyExemption(NamedTuple):
    """One mixed read/write tool that stays enabled in read-only mode.

    ``blocked_write`` inspects the call arguments and returns ``None``
    when the invocation is a read, or a short human-readable description
    of the write operation when it must be blocked. ``allowed`` is a
    one-line summary of what remains available, surfaced in the error so
    the LLM can self-correct.
    """

    blocked_write: Callable[[dict[str, Any]], str | None]
    allowed: str


def _backup_write(args: dict[str, Any]) -> str | None:
    scope = args.get("scope")
    action = args.get("action")
    # Read-only-safe: per-edit backup listing/viewing/diffing, and snapshot listing
    # (issue #1586 — pure ``backup/info`` read, no tarball mutation).
    # ``diff`` (added in #1632) only reads the stored snapshot and fetches
    # the live config to compare; it never mutates (issue #1944).
    if scope == "edits" and action in ("list", "view", "diff"):
        return None
    if scope == "snapshot" and action == "list":
        return None
    return f"scope={scope!r}, action={action!r}"


# Add-on parameters that, when present, mean the call mutates add-on
# configuration (so the read-only middleware must block it). Module-level
# so the schema-drift guard test (test_read_only.py) can pin an
# independent manifest against it — see item 10b / ha_manage_app.
_ADDON_CONFIG_WRITE_PARAMS = ("options", "network", "boot", "auto_update", "watchdog")


def _addon_write(args: dict[str, Any]) -> str | None:
    action = args.get("action")
    if action:
        return f"action={action!r}"
    for param in _ADDON_CONFIG_WRITE_PARAMS:
        if args.get(param) is not None:
            return f"app (add-on) configuration change ({param}=...)"
    if args.get("array_patch") is not None:
        return "array_patch modification"
    if args.get("websocket"):
        # A WebSocket session's initial message can command mutations
        # (e.g. an ESPHome firmware/compile or devices/update_config command),
        # so it is not statically classifiable as a read — fail closed.
        return "WebSocket proxy session"
    method = str(args.get("method") or "GET").strip().upper()
    if method != "GET":
        return f"HTTP {method} proxy request"
    return None


def _energy_write(args: dict[str, Any]) -> str | None:
    mode = args.get("mode")
    if mode == "get":
        return None
    # dry_run=True previews validate/simulate without saving (every
    # write mode short-circuits before energy/save_prefs). Strict
    # ``is True``: the middleware sees RAW pre-validation arguments, so
    # a non-bool truthy value (e.g. the string "false") that schema
    # coercion could turn into False must fail closed here.
    if args.get("dry_run") is True:
        return None
    return f"mode={mode!r}"


def _pipeline_write(args: dict[str, Any]) -> str | None:
    action = args.get("action")
    if action in ("list", "get"):
        return None
    return f"action={action!r}"


def _security_policy_write(args: dict[str, Any]) -> str | None:
    # ``action`` has no schema default, so an absent key is a malformed call
    # rather than an implicit read — fail closed.
    action = args.get("action")
    if action == "get":
        return None
    return f"action={action!r}"


def _custom_tool_write(args: dict[str, Any]) -> str | None:
    if args.get("list_saved") and not args.get("code") and not args.get("run_saved"):
        return None
    # Sandbox execution gets api_post / ws_send bridges that can write
    # to HA directly, so running code (new or saved) is never a read.
    return "sandbox code execution"


def _updates_write(args: dict[str, Any]) -> str | None:
    # ``action`` defaults to "list" at the schema layer, so a missing action
    # executes as a read (the middleware sees RAW pre-validation arguments —
    # an absent key here means the tool itself will run the list branch).
    action = args.get("action")
    if action in (None, "list", "get"):
        return None
    return f"action={action!r}"


def _blueprint_write(args: dict[str, Any]) -> str | None:
    # Reads (allowed): listing installed blueprints and reading one's
    # metadata/inputs/body. ``substitute`` joins them because it is a pure
    # render — core builds the standalone config in memory and sends it back,
    # touching neither the blueprint store nor any automation/script; writing
    # the rendered config anywhere still needs ha_config_set_automation /
    # ha_config_set_script, which read-only mode blocks. ``import`` (writes a
    # blueprint file), ``save`` (overwrites one with caller-supplied YAML) and
    # ``delete`` stay blocked, and a missing/unknown action fails closed.
    action = args.get("action")
    if action in ("list", "get", "substitute"):
        return None
    return f"action={action!r}"


def _theme_write(args: dict[str, Any]) -> str | None:
    # Reads (allowed): listing installed themes and inspecting the screenshot
    # engine account's saved per-user theme. The latter has no pure-read
    # duplicate anywhere, and it is exactly what a caller needs in Read Only
    # Mode: captures still make the engine persist a theme and still warn about
    # it, so the value must stay inspectable even when restoring it is blocked.
    # Both writes -- the backend default ('set') and the engine-account restore
    # ('set_engine_theme') -- stay blocked. A missing action fails closed.
    if args.get("action") in ("list", "get_engine_theme"):
        return None
    return f"action={args.get('action')!r}"


def _radio_write(args: dict[str, Any]) -> str | None:
    action = args.get("action")
    # Reads (allowed): per-node diagnostics, the integration/network summary,
    # the active reachability probe, a single Zigbee cluster-attribute read, and
    # the Thread dataset listing, and Z-Wave configuration-parameter reads
    # (cache or explicit device request). Everything else is a write — commission/add,
    # remove, reinterview, firmware, fabric/credential/channel/network changes,
    # plus the two actions that LOOK read-ish but are not: zigbee network_backup
    # (creates a backup artifact + key material, like ha_manage_backup's blocked
    # snapshot create) and thread discover_routers (kicks off a long-running
    # mDNS scan). A missing action fails closed.
    if action in (
        "diagnostics",
        "network_status",
        "ping",
        "cluster_read",
        "list_datasets",
        "get_config_params",
        "get_config_param",
    ):
        return None
    return f"action={action!r}"


# Mixed read/write tools whose read surface has no pure-read duplicate
# (verified per tool: ha_get_app cannot proxy-read addon-internal
# APIs; energy prefs and assist pipelines are reachable only through
# these tools; edit-backup listing exists nowhere else; the saved-tools
# cache is only listable here; ha_manage_radio's 'ping' probe, 'cluster_read'
# 'list_datasets', 'get_config_params' and 'get_config_param' have no pure-read
# duplicate elsewhere, while its
# 'diagnostics'/'network_status' reads mirror ha_get_device /
# ha_get_system_health but stay reachable here mid-management;
# ha_manage_security_policy's policy read is duplicated only by the
# dev-mode-only ha_dev_manage_settings('get_policy')). Everything
# NOT in this table and not ``readOnlyHint=True`` is hidden and blocked
# outright.
#
# ``MANDATORY_TOOLS`` (settings_ui/__init__.py) intentionally needs no special
# case here: every mandatory tool is either ``readOnlyHint=True`` or
# present in this table (``ha_manage_backup``). The e2e test
# ``test_real_catalog_mandatory_tools_stay_available``
# (tests/src/e2e/policy/test_readonly_mode.py) guards that invariant
# against the real registered catalog at PR time, so the two sets
# cannot drift apart silently.
READ_ONLY_EXEMPT_TOOLS: dict[str, ReadOnlyExemption] = {
    "ha_manage_theme": ReadOnlyExemption(
        _theme_write,
        "listing installed themes (action='list') and reading the screenshot "
        "engine account's saved theme (action='get_engine_theme')",
    ),
    "ha_manage_backup": ReadOnlyExemption(
        _backup_write,
        "listing, viewing, and diffing per-edit backups (scope='edits', "
        "action='list', 'view', or 'diff') and listing snapshots "
        "(scope='snapshot', action='list')",
    ),
    "ha_manage_app": ReadOnlyExemption(
        _addon_write,
        "HTTP GET proxy reads of add-on APIs (slug + path, method='GET')",
    ),
    "ha_manage_energy_prefs": ReadOnlyExemption(
        _energy_write,
        "reading the energy configuration (mode='get') and dry-run "
        "previews (dry_run=true)",
    ),
    "ha_manage_pipeline": ReadOnlyExemption(
        _pipeline_write,
        "listing and inspecting pipelines (action='list' or 'get')",
    ),
    "ha_manage_custom_tool": ReadOnlyExemption(
        _custom_tool_write,
        "listing saved tools (list_saved=True)",
    ),
    "ha_manage_radio": ReadOnlyExemption(
        _radio_write,
        "node diagnostics ('diagnostics'), the integration/network summary "
        "('network_status'), the active reachability probe ('ping'), a Zigbee "
        "cluster-attribute read ('cluster_read'), and the Thread dataset "
        "listing ('list_datasets'), and Z-Wave parameter reads "
        "('get_config_params' / 'get_config_param', including explicit refresh)",
    ),
    # Update listing/details exist only here — ha_get_updates was merged
    # into this tool (issue #1726), so hiding it would remove the read
    # surface entirely.
    "ha_manage_updates": ReadOnlyExemption(
        _updates_write,
        "listing pending updates (action='list', the default) and reading "
        "update details/release notes (action='get')",
    ),
    # Reading the security-policy document has no pure-read duplicate in
    # standard mode: ha_dev_manage_settings('get_policy') is the only other
    # reader and dev mode is off by default. Editing the policy stays
    # blocked — it is exactly the kind of write read-only mode exists for.
    "ha_manage_security_policy": ReadOnlyExemption(
        _security_policy_write,
        "reading the policy document (action='get')",
    ),
    # Blueprint listing and reading exist only here — ha_get_blueprint was
    # merged into this tool (issue #2329), so hiding it would remove the read
    # surface entirely.
    "ha_manage_blueprints": ReadOnlyExemption(
        _blueprint_write,
        "listing and reading blueprints (action='list' or 'get') and "
        "rendering a standalone config (action='substitute')",
    ),
}
