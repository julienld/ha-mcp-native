"""YAML editing algorithms shared by the component owner and its legacy services."""

import difflib
import hashlib
from typing import Any

from .const import (
    ALLOWED_YAML_KEYS,
    DASHBOARD_URL_PATH_PATTERN,
    PACKAGES_ONLY_YAML_KEYS,
    RESERVED_DASHBOARD_URL_PATHS,
    YAML_KEY_DENYLIST,
)
from .file_paths import _validate_dashboard_filename


def _unified_diff(before: str, after: str, rel_path: str, max_lines: int = 200) -> str:
    """Unified diff of a prospective write, capped for response size."""
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{rel_path} (before)",
            tofile=f"{rel_path} (after)",
        )
    )
    if len(lines) > max_lines:
        omitted = len(lines) - max_lines
        lines = lines[:max_lines] + [f"... diff truncated ({omitted} more lines)\n"]
    return "".join(lines)


def _confirm_token(normalized: str, raw_content: str, new_content: str) -> str:
    """Bind confirmation to the target, its before-image and the proposed text.

    Re-derived on the confirm call from CURRENT disk state, so any change
    to the file between preview and confirm invalidates the token
    (optimistic locking, same philosophy as utils/config_hash.py).
    """
    digest = hashlib.sha256()
    for value in (normalized, raw_content, new_content):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()[:16]


def _count_reindented_lines(diff_text: str) -> int:
    """Count untouched lines that only changed leading whitespace.

    A ``-``/``+`` pair whose stripped bodies match is a pure re-indent —
    collateral from normalizing a mixed-style file to one sequence style
    (ruamel supports a single style per dump). Multiset matching so
    repeated identical lines don't over-count.
    """
    removed: dict[str, int] = {}
    added: dict[str, int] = {}
    for line in diff_text.splitlines():
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            body = line[1:]
            removed[body.strip()] = removed.get(body.strip(), 0) + 1
        elif line.startswith("+"):
            body = line[1:]
            added[body.strip()] = added.get(body.strip(), 0) + 1
    return sum(min(n, added.get(key, 0)) for key, n in removed.items() if key)


def _reindent_warning(count: int) -> str:
    """Warning text for re-indented untouched lines (see ``diff``)."""
    return (
        f"{count} line(s) outside the requested edit were re-indented: the "
        "file mixes sequence-indent styles and the serializer normalizes to "
        "the first-detected style. Values are unchanged (guarded); review "
        "the diff if the whitespace matters."
    )


def _walk_lovelace_dashboards(
    data: dict[str, Any], action: str, parsed_content: Any
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Validate dashboard content and walk/create lovelace.dashboards in ``data``.

    Returns (lovelace, dashboards, error). On error the mappings are empty dicts.
    """
    # filename validation for add/replace
    if action in ("add", "replace"):
        if not isinstance(parsed_content, dict):
            return (
                {},
                {},
                {
                    "success": False,
                    "error": "lovelace.dashboards.<url_path> content must be a YAML mapping",
                },
            )
        fn_err = _validate_dashboard_filename(parsed_content.get("filename", ""))
        if fn_err is not None:
            return {}, {}, {"success": False, "error": fn_err}

    # Walk/create lovelace.dashboards
    lovelace = data.setdefault("lovelace", {})
    if not isinstance(lovelace, dict):
        return (
            {},
            {},
            {
                "success": False,
                "error": "Existing 'lovelace' key is not a YAML mapping",
            },
        )
    dashboards = lovelace.setdefault("dashboards", {})
    if not isinstance(dashboards, dict):
        return (
            {},
            {},
            {
                "success": False,
                "error": "Existing 'lovelace.dashboards' is not a YAML mapping",
            },
        )
    return lovelace, dashboards, None


def _apply_lovelace_dashboard_action(
    data: dict[str, Any],
    lovelace: dict[str, Any],
    dashboards: dict[str, Any],
    action: str,
    url_path: str,
    parsed_content: Any,
) -> dict[str, Any] | None:
    """Apply add/replace/remove for a single lovelace dashboard in ``data``."""
    if action == "add":
        if url_path in dashboards:
            existing = dashboards[url_path]
            if isinstance(existing, dict) and isinstance(parsed_content, dict):
                existing.update(parsed_content)
            else:
                return {
                    "success": False,
                    "error": (
                        f"Type mismatch for dashboard '{url_path}': use 'replace' "
                        "to overwrite."
                    ),
                }
        else:
            dashboards[url_path] = parsed_content
    elif action == "replace":
        dashboards[url_path] = parsed_content
    elif action == "remove":
        if url_path not in dashboards:
            return {
                "success": False,
                "error": (
                    f"Dashboard '{url_path}' not found under lovelace.dashboards."
                ),
            }
        del dashboards[url_path]
        # Clean up empty parent containers to keep the file tidy
        if not dashboards:
            del lovelace["dashboards"]
        if not lovelace:
            del data["lovelace"]
    return None


def _apply_lovelace_dashboard_edit(
    data: dict[str, Any], action: str, url_path: str, parsed_content: Any
) -> dict[str, Any] | None:
    """Apply an add/replace/remove to lovelace.dashboards.<url_path> in ``data``."""
    lovelace, dashboards, walk_err = _walk_lovelace_dashboards(
        data, action, parsed_content
    )
    if walk_err is not None:
        return walk_err
    return _apply_lovelace_dashboard_action(
        data, lovelace, dashboards, action, url_path, parsed_content
    )


def _apply_single_key_edit(
    data: dict[str, Any],
    action: str,
    yaml_key: str,
    label: str,
    parsed_content: Any,
    rel_path: str,
) -> dict[str, Any] | None:
    """Apply add/replace/remove for a single top-level key (or theme) in ``data``."""
    # Single-key apply logic, shared by plain config keys and
    # theme names (kind == "theme"): a theme file is a mapping of
    # theme-name -> variables, and theme content is pre-validated
    # as a mapping above, so the list-merge arm never fires for it.
    if action == "add":
        if yaml_key in data:
            existing = data[yaml_key]
            # Merge: list extends list, dict merges dict
            if isinstance(existing, list) and isinstance(parsed_content, list):
                data[yaml_key] = existing + parsed_content
            elif isinstance(existing, dict) and isinstance(parsed_content, dict):
                existing.update(parsed_content)
            else:
                return {
                    "success": False,
                    "error": (
                        f"Type mismatch for {label.lower()} '{yaml_key}': "
                        f"existing is {type(existing).__name__}, "
                        f"new content is {type(parsed_content).__name__}. "
                        "Use action='replace' to overwrite."
                    ),
                }
        else:
            data[yaml_key] = parsed_content
    elif action == "replace":
        data[yaml_key] = parsed_content
    elif action == "remove":
        if yaml_key not in data:
            return {
                "success": False,
                "error": f"{label} '{yaml_key}' not found in '{rel_path}'.",
            }
        del data[yaml_key]
    return None


def _apply_edit_action(
    data: dict[str, Any],
    action: str,
    kind: str,
    path_parts: tuple[str, ...],
    parsed_content: Any,
    rel_path: str,
) -> dict[str, Any] | None:
    """Apply the requested edit action to ``data`` in place, returning an error or None."""
    if kind == "lovelace_dashboard":
        return _apply_lovelace_dashboard_edit(
            data, action, path_parts[2], parsed_content
        )
    label = "Theme" if kind == "theme" else "Key"
    return _apply_single_key_edit(
        data, action, path_parts[0], label, parsed_content, rel_path
    )


def _validate_lovelace_dashboard_path(
    yaml_path: str, parts: tuple[str, ...]
) -> tuple[str, tuple[str, ...], str | None]:
    """Validate a 'lovelace.dashboards.<url_path>' yaml_path (shape 2).

    Returns (kind, parts, error); on error kind is '' and parts is ().
    """
    if parts[:2] != ("lovelace", "dashboards") or len(parts) != 3:
        return (
            "",
            (),
            (
                f"Dotted yaml_path '{yaml_path}' is not supported. "
                "The only accepted dotted form is 'lovelace.dashboards.<url_path>'."
            ),
        )

    url_path = parts[2]
    if url_path in RESERVED_DASHBOARD_URL_PATHS:
        return (
            "",
            (),
            f"url_path '{url_path}' is reserved by Home Assistant and cannot be used.",
        )
    if not DASHBOARD_URL_PATH_PATTERN.fullmatch(url_path):
        return (
            "",
            (),
            (
                f"url_path '{url_path}' is invalid. Must be lowercase letters/digits "
                "separated by hyphens (e.g., 'energy-dashboard')."
            ),
        )
    return "lovelace_dashboard", parts, None


def _parse_and_validate_yaml_path(
    yaml_path: str,
    *,
    is_package: bool = False,
    is_theme: bool = False,
    extra_allowed_keys: frozenset[str] = frozenset(),
) -> tuple[str, tuple[str, ...], str | None]:
    """Parse and validate a yaml_path argument.

    Three accepted shapes:
    1. Single segment in ALLOWED_YAML_KEYS -> kind='single'
       When ``is_package=True``, single segments in PACKAGES_ONLY_YAML_KEYS
       (automation, script, scene) are also accepted. ``extra_allowed_keys``
       adds operator-opted-in keys on top of ALLOWED_YAML_KEYS (#1887); it
       overrides neither ``YAML_KEY_DENYLIST`` (filtered out before it gets
       here) nor the packages-only restriction.
    2. Exactly 'lovelace.dashboards.<url_path>' -> kind='lovelace_dashboard'
    3. Single segment theme name (no dots) when ``is_theme=True`` -> kind='theme'

    Returns (kind, parts, error). On error, kind is '' and parts is ().
    """
    if not yaml_path or not isinstance(yaml_path, str):
        return "", (), "yaml_path must be a non-empty string"

    parts = tuple(yaml_path.split("."))

    # Shape 3: theme name (single segment, no dots)
    if is_theme:
        if len(parts) == 1:
            return "theme", parts, None
        return (
            "",
            (),
            (
                f"Theme name '{yaml_path}' cannot contain dots. "
                "Provide a simple theme name (e.g., 'my-theme', not 'my.theme')."
            ),
        )

    # Shape 1: single key
    if len(parts) == 1:
        key = parts[0]
        # The deny floor is checked before every single-key accept branch, so
        # it holds even if a denied key is ever added to one of the allow
        # sets. It deliberately does not cover the theme branch above: under
        # ``is_theme`` the segment names a file in themes/, not a top-level
        # configuration key, so the same word carries no trust-boundary
        # meaning there.
        if key in YAML_KEY_DENYLIST:
            return (
                "",
                (),
                (
                    f"Key '{yaml_path}' can never be edited through this "
                    "service: it redefines Home Assistant's own trust "
                    "boundary (authentication, proxy/CORS handling, or "
                    "frontend module loading). This floor cannot be lifted "
                    "by the extra-write-keys setting. Edit it by hand if "
                    "you really need to change it."
                ),
            )
        if key in ALLOWED_YAML_KEYS:
            return "single", parts, None
        if is_package and key in PACKAGES_ONLY_YAML_KEYS:
            return "single", parts, None
        # Operator extra keys widen ALLOWED_YAML_KEYS only. They deliberately
        # do NOT lift the packages-only restriction: those keys reach
        # packages/*.yaml through the branch above (still governed by their
        # per-key toggle) and stay rejected in configuration.yaml, so the
        # storage-mode/YAML-mode collision guarantee holds however the
        # operator fills the setting.
        if key in extra_allowed_keys and key not in PACKAGES_ONLY_YAML_KEYS:
            return "single", parts, None
        # Reaching here means the key was not accepted. If it is a
        # PACKAGES_ONLY key, we know is_package=False (the packages branch
        # would have returned, and the extra-keys branch excludes them
        # precisely so this guidance still fires) – emit the targeted
        # "move it to a package file" guidance instead of the generic
        # allowlist dump below.
        if key in PACKAGES_ONLY_YAML_KEYS:
            return (
                "",
                (),
                (
                    f"Key '{yaml_path}' is only allowed in packages/*.yaml "
                    "files, not in configuration.yaml. Move the edit to a "
                    "package file (e.g., packages/automations.yaml) or use "
                    "ha_config_set_automation, ha_config_set_script, or "
                    "ha_config_set_scene for storage-mode."
                ),
            )
        allowed = (
            ALLOWED_YAML_KEYS | PACKAGES_ONLY_YAML_KEYS | extra_allowed_keys
            if is_package
            else ALLOWED_YAML_KEYS | extra_allowed_keys
        )
        return (
            "",
            (),
            (
                f"Key '{yaml_path}' is not in the allowed list. "
                f"Allowed keys: {', '.join(sorted(allowed))}. "
                "For YAML-mode dashboards use 'lovelace.dashboards.<url_path>'."
            ),
        )

    # Shape 2: lovelace.dashboards.<url_path>
    return _validate_lovelace_dashboard_path(yaml_path, parts)
