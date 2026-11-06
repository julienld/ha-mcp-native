"""The component's YAML editor, shared by genuine service and native callers."""

from __future__ import annotations

import fnmatch
import logging
import posixpath
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.config import async_check_ha_config_file
from ruamel.yaml import YAMLError

from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .const import (
    ALLOWED_YAML_CONFIG_FILES,
    PACKAGES_ONLY_YAML_KEYS,
    YAML_KEY_DEFAULT_POST_ACTION,
    YAML_KEY_DENYLIST,
    YAML_KEY_POST_ACTIONS,
)
from .file_access import BeforeWrite, FileAccess, Guard, _current_extra_yaml_keys
from .file_io import FileImage, FileWriteState, _replace_file_sync, read_file_image
from .file_paths import _path_in_package_dir, _resolves_within, _violates_deny_floor
from .package_dirs import _detect_package_dirs
from .yaml_edit_values import (
    _apply_edit_action,
    _confirm_token,
    _count_reindented_lines,
    _parse_and_validate_yaml_path,
    _reindent_warning,
    _unified_diff,
)
from .yaml_rt import apply_seq_indent, detect_seq_indent, make_yaml, yaml_dumps

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__package__)
Commit = Callable[[str], Awaitable[dict[str, Any]]]


async def _run_config_check(hass: HomeAssistant, rel_path: str) -> dict[str, Any]:
    """Validate the whole HA config after a write and return fields to merge
    into the write response.

    Uses ``async_check_ha_config_file``, which returns an error string when the
    resulting config is invalid (and ``None`` when it is valid). The
    ``homeassistant.check_config`` *service* is deliberately not used: it is
    registered with ``SupportsResponse.NONE`` and signals errors only by
    raising, so a response-returning call to it always failed and the check
    never actually ran (#1660).

    Never raises: a check that cannot run surfaces as
    ``config_check: "unavailable"`` rather than failing an already-completed
    write.
    """
    try:
        errors = await async_check_ha_config_file(hass)
    except Exception as check_err:  # noqa: BLE001
        _LOGGER.warning(
            "Config check unavailable after editing %s: %s", rel_path, check_err
        )
        return {"config_check": "unavailable", "config_check_error": str(check_err)}
    if errors:
        _LOGGER.warning(
            "Config check found errors after editing %s: %s", rel_path, errors
        )
        return {"config_check": "errors", "config_check_errors": errors}
    return {"config_check": "ok"}


async def _classify_edit_yaml_target(
    hass: HomeAssistant, rel_path: str, normalized: str
) -> tuple[bool, bool, dict[str, Any] | None]:
    """Validate the edit target path and classify it as package/theme.

    Returns (is_package, is_theme, error). On rejection the bools are False and
    error is the response dict; on success error is None.
    """
    # Validate file path — only configuration.yaml, packages/*.yaml, and themes/*.yaml
    if normalized.startswith("..") or normalized.startswith("/"):
        return (
            False,
            False,
            {
                "success": False,
                "error": "Path traversal is not allowed.",
            },
        )

    is_config_yaml = normalized in ALLOWED_YAML_CONFIG_FILES
    is_theme = fnmatch.fnmatch(normalized, "themes/*.yaml")
    # Package files may live under any folder the user binds via
    # ``homeassistant: packages: !include_dir_named <folder>`` — not just the
    # default ``packages`` (issue #1854). The configured folder(s) are
    # detected at runtime (parsing configuration.yaml), so only do that work
    # when the target isn't already a config-root or theme file. The set
    # always includes ``"packages"`` as a fallback.
    package_dirs: set[str] = set()
    is_package = False
    if not is_config_yaml and not is_theme:
        package_dirs = await _detect_package_dirs(hass)
        is_package = _path_in_package_dir(normalized, package_dirs)
    if not is_config_yaml and not is_package and not is_theme:
        allowed_pkg = ", ".join(f"{folder}/*.yaml" for folder in sorted(package_dirs))
        return (
            False,
            False,
            {
                "success": False,
                "error": (
                    f"File '{rel_path}' is not allowed. "
                    f"Only {', '.join(ALLOWED_YAML_CONFIG_FILES)}, {allowed_pkg}, and themes/*.yaml are supported."
                ),
            },
        )
    return is_package, is_theme, None


def _check_edit_yaml_containment(
    config_dir: Path, rel_path: str, normalized: str, is_theme: bool
) -> dict[str, Any] | None:
    """Reject an edit target that escapes the config dir or is a hidden theme path."""
    # Symlink-safe containment (parity with the read path, issue #1586): the
    # write target is ``config_dir / normalized``, so a package/theme folder
    # that is (or contains) a symlink escaping the config dir — or a path
    # resolving into the deny floor (.storage / secrets.yaml) — must be
    # rejected before writing, exactly as read_file already does.
    if _violates_deny_floor(config_dir, normalized) or not _resolves_within(
        config_dir, rel_path
    ):
        _LOGGER.warning("Rejected YAML edit escaping the config dir: %s", rel_path)
        return {
            "success": False,
            "error": (
                f"Path '{rel_path}' resolves outside the config directory or "
                "into a protected location and cannot be edited."
            ),
        }

    # ``themes/*.yaml`` matches dot-prefixed paths (fnmatch's ``*`` matches a
    # leading ``.`` and spans ``/``), but HA's ``!include_dir_merge_named``
    # loader skips them: ``annotatedyaml``'s ``_find_files`` filters every
    # walked directory AND file basename through ``_is_file_valid`` (which
    # is ``return not name.startswith(".")``), so a dot-prefixed file
    # (``themes/.hidden.yaml``) OR directory (``themes/.foo/bar.yaml``) is
    # pruned and the theme never loads — a phantom ``reload_performed``.
    # Reject if any path segment is dot-prefixed, mirroring the loader,
    # rather than only checking the basename.
    if is_theme and any(seg.startswith(".") for seg in normalized.split("/")):
        return {
            "success": False,
            "error": (
                f"Theme path '{rel_path}' has a dot-prefixed file or "
                "directory segment. Home Assistant's !include_dir_merge_named "
                "skips any path segment whose name starts with '.', so it "
                "would never load. Use a path with no dot-prefixed segment."
            ),
        }
    return None


def _check_packages_key_gate(
    options: dict[str, Any], yaml_path: str, is_package: bool
) -> dict[str, Any] | None:
    """Reject a package-only yaml_path key disabled by the caller's runtime config.

    Returns an error response when the top-level key of a packages/*.yaml write
    is in the caller's disabled set, else None.
    """
    # Caller-provided per-key opt-out for PACKAGES_ONLY_YAML_KEYS.
    # Filter to the recognised set so a caller that types
    # ``automatoin`` doesn't accidentally pass through; only
    # actually-known keys count.
    disabled_packages_keys = {
        key
        for key in options.get("disabled_packages_keys", [])
        if key in PACKAGES_ONLY_YAML_KEYS
    }
    # Per-key gate fires only for packages/*.yaml writes. Writes to
    # configuration.yaml fall through to ``_parse_and_validate_yaml_path``
    # which rejects PACKAGES_ONLY_YAML_KEYS with the storage-mode-
    # tools advisory regardless of this flag.
    top_segment = yaml_path.split(".", 1)[0] if yaml_path else ""
    if is_package and top_segment in disabled_packages_keys:
        return {
            "success": False,
            "error": (
                f"yaml_path key {top_segment!r} is disabled by the "
                f"caller's runtime configuration. Re-enable it in the "
                f"caller (the ha-mcp Server Settings → YAML config "
                f"editing → 'Allow {top_segment} in packages/*.yaml' "
                f"toggle), or use the storage-mode equivalent."
            ),
        }
    return None


async def _apply_replace_file(
    hass: HomeAssistant,
    config_dir: Path,
    rel_path: str,
    normalized: str,
    action: str,
    yaml_path: str,
    content: str | None,
    commit: Commit,
) -> dict[str, Any]:
    """Handle action='replace_file': validate and atomically write the whole file."""
    if not content:
        return {
            "success": False,
            "error": "'content' is required for action 'replace_file'.",
        }
    try:
        whole = await hass.async_add_executor_job(
            lambda: make_yaml().load(StringIO(content))
        )
    except YAMLError as err:
        return {"success": False, "error": f"Invalid YAML content: {err}"}
    if not isinstance(whole, dict):
        return {
            "success": False,
            "error": "Whole-file content must be a YAML mapping at the root.",
        }
    try:
        # Write the backup content verbatim (faithful restore). Bundles
        # mkdir + atomic temp-write+rename + stat into one offload, like
        # _write_file_sync.
        write_meta = await commit(content)
    except PermissionError:
        _LOGGER.error("Permission denied editing: %s", rel_path)
        return {
            "success": False,
            "error": f"Permission denied: {rel_path}",
        }
    except OSError as err:
        _LOGGER.error("Error editing YAML config %s: %s", rel_path, err)
        return {"success": False, "error": str(err)}

    _LOGGER.info("YAML config restored whole-file: %s", rel_path)
    restore_result: dict[str, Any] = {
        "success": True,
        "file": rel_path,
        "action": action,
        "yaml_path": yaml_path,
        # Verbatim whole-file write; shares the write-path response
        # shape so ``written`` stays a reliable discriminator.
        "written": True,
        "size": write_meta["size"],
        "modified": datetime.fromtimestamp(write_meta["mtime"]).isoformat(),
        # A whole-file restore can touch any number of keys; a restart
        # is the always-correct activation path.
        "post_action": "restart_required",
    }
    restore_result.update(await _run_config_check(hass, rel_path))
    return restore_result


async def _parse_edit_content(
    hass: HomeAssistant, action: str, content: str | None, kind: str
) -> tuple[Any, dict[str, Any] | None]:
    """Validate content is valid YAML for add/replace.

    Returns (parsed_content, error). For other actions parsed_content is None.
    """
    parsed_content: Any = None
    if action in ("add", "replace"):
        if not content:
            return None, {
                "success": False,
                "error": f"'content' is required for action '{action}'.",
            }
        try:
            parsed_content = await hass.async_add_executor_job(
                lambda: make_yaml().load(StringIO(content))
            )
        except YAMLError as err:
            return None, {
                "success": False,
                "error": f"Invalid YAML content: {err}",
            }
        if parsed_content is None:
            return None, {
                "success": False,
                "error": "Content parsed as null/empty. Provide non-empty YAML.",
            }
        # Theme content must be a YAML mapping (theme variables)
        if kind == "theme" and not isinstance(parsed_content, dict):
            return None, {
                "success": False,
                "error": "Theme content must be a YAML mapping (theme variables).",
            }
    return parsed_content, None


async def _load_edit_target_data(
    hass: HomeAssistant, image: FileImage, action: str, rel_path: str
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """Read + parse the existing YAML file, or start empty.

    Returns (data, raw_content, error).
    """
    # Read existing file content (or start with empty dict)
    if image.stamp is not None:
        if image.text is None:
            return (
                {},
                "",
                {"success": False, "error": f"Cannot read binary file: {rel_path}"},
            )
        # Match the former text-mode reader for parsing/diff. The detached
        # image itself retains exact newlines for capture, confirmation and CAS.
        raw_content = image.text.replace("\r\n", "\n").replace("\r", "\n")
        try:
            data = await hass.async_add_executor_job(
                lambda: make_yaml().load(StringIO(raw_content)) or {}
            )
        except YAMLError as err:
            return (
                {},
                "",
                {
                    "success": False,
                    "error": f"Cannot parse existing file '{rel_path}': {err}",
                },
            )
        if not isinstance(data, dict):
            return (
                {},
                "",
                {
                    "success": False,
                    "error": f"File '{rel_path}' root is not a YAML mapping.",
                },
            )
    else:
        if action == "remove":
            return (
                {},
                "",
                {
                    "success": False,
                    "error": f"File does not exist: {rel_path}",
                },
            )
        data = {}
        raw_content = ""
    return data, raw_content, None


async def _serialize_and_validate_edit(
    hass: HomeAssistant, data: dict[str, Any], raw_content: str
) -> tuple[str, dict[str, Any] | None]:
    """Serialize ``data`` to YAML and verify it round-trips unchanged.

    Returns (new_content, error); new_content is "" when error is set.
    """
    # Serialize back to YAML, keeping the file's dominant top-level
    # sequence-dash style (#1720: "changed indentation I didn't ask
    # to change"). ruamel supports only ONE sequence style per dump,
    # so in a MIXED-style file the minority-style sequences are
    # normalized to the first-detected style — that collateral is
    # counted below and surfaced as a warning + visible in ``diff``.
    seq_style = detect_seq_indent(raw_content)

    def _dump() -> str:
        ry = make_yaml()
        apply_seq_indent(ry, seq_style)
        return yaml_dumps(ry, data)

    try:
        new_content = await hass.async_add_executor_job(_dump)
    except YAMLError as err:
        return "", {
            "success": False,
            "error": f"Failed to serialize YAML: {err}",
        }

    # Validate the result parses cleanly AND round-trips to the
    # same data we intend to write. The emitter must not alter any
    # parsed value (e.g. by re-wrapping a long line inside a folded
    # scalar, which embeds a literal newline in an untouched string
    # — #1720); if it would, refuse to write rather than corrupt
    # the file silently.
    try:
        reparsed = await hass.async_add_executor_job(
            lambda: make_yaml().load(StringIO(new_content))
        )
    except YAMLError as err:
        return "", {
            "success": False,
            "error": f"Generated YAML failed validation: {err}",
        }
    if reparsed != data:
        return "", {
            "success": False,
            "error": (
                "Aborted: re-serializing the file would have altered "
                "content outside the requested edit (the YAML "
                "serializer changed at least one value elsewhere in "
                "the file). The file was NOT modified."
            ),
        }
    return new_content, None


def _maybe_build_confirm_preview(
    options: dict[str, Any],
    normalized: str,
    raw_content: str,
    new_content: str,
    rel_path: str,
    action: str,
    yaml_path: str,
    diff_text: str,
    reindent_count: int,
) -> dict[str, Any] | None:
    """Return a preview response when confirmation is required but unconfirmed."""
    require_confirm = bool(options.get("require_confirm", False))
    supplied_token = options.get("confirm_token")
    expected_token = _confirm_token(normalized, raw_content, new_content)
    if not (require_confirm and supplied_token != expected_token):
        return None
    preview: dict[str, Any] = {
        "success": True,
        "preview": True,
        "written": False,
        "file": rel_path,
        "action": action,
        "yaml_path": yaml_path,
        "diff": diff_text,
        "confirm_token": expected_token,
        "message": (
            "Preview only — NOTHING was written. Review the diff "
            "for unintended changes outside the requested edit, "
            "then repeat the exact same call with confirm_token "
            "to apply."
        ),
    }
    if reindent_count:
        preview["warnings"] = [_reindent_warning(reindent_count)]
    if supplied_token is not None:
        preview["confirm_token_mismatch"] = True
        preview["message"] = (
            "confirm_token did not match — the file changed "
            "since the preview (or the token was wrong). NOTHING "
            "was written. Review the fresh diff and retry with "
            "the new confirm_token."
        )
    return preview


async def _resolve_post_action(
    hass: HomeAssistant, kind: str, path_parts: tuple[str, ...], context: Any = None
) -> dict[str, Any]:
    """Return the post-edit activation info (restart/reload) for the edit kind."""
    # Surface the post-edit action required to activate the change
    post_info: dict[str, Any]
    if kind == "lovelace_dashboard":
        post_info = {"post_action": "restart_required"}
    elif kind == "theme":
        # Trigger frontend.reload_themes to load the theme change
        try:
            await hass.services.async_call(
                "frontend",
                "reload_themes",
                {},
                blocking=True,
                context=context,
            )
            post_info = {
                "post_action": "reload_performed",
                "reload_service": "frontend.reload_themes",
            }
        except Exception as reload_err:  # noqa: BLE001
            post_info = {
                "post_action": "reload_available",
                "reload_service": "frontend.reload_themes",
                "reload_error": str(reload_err),
            }
            _LOGGER.warning(
                "frontend.reload_themes failed after theme edit: %s",
                reload_err,
            )
    else:
        post_info = YAML_KEY_POST_ACTIONS.get(
            path_parts[0], YAML_KEY_DEFAULT_POST_ACTION
        )
    return post_info


async def _apply_yaml_key_edit(
    hass: HomeAssistant,
    config_dir: Path,
    options: dict[str, Any],
    rel_path: str,
    normalized: str,
    action: str,
    yaml_path: str,
    kind: str,
    path_parts: tuple[str, ...],
    parsed_content: Any,
    image: FileImage,
    commit: Commit,
    guard: Guard | None,
    context: Any = None,
) -> dict[str, Any]:
    """Read, edit, serialize, and atomically write the requested YAML key change."""

    try:
        data, raw_content, load_err = await _load_edit_target_data(
            hass, image, action, rel_path
        )
        if load_err is not None:
            return load_err

        # Pre-write backups are captured MCP-side by ha-mcp's shared
        # auto-backup layer (#1579): ha_config_set_yaml snapshots the
        # prior key state before calling this service, so the edit is
        # restorable via ha_manage_backup(scope="edits"). The component
        # no longer writes its own .ha_mcp_tools_backups/ copy. (Pre-fix
        # backups already on disk there stay readable; the separate
        # GHSA-g39v-cvjh-8fpf startup migration is unaffected.)

        # Perform the action — branch on kind
        apply_err = _apply_edit_action(
            data, action, kind, path_parts, parsed_content, rel_path
        )
        if apply_err is not None:
            return apply_err

        new_content, ser_err = await _serialize_and_validate_edit(
            hass, data, raw_content
        )
        if ser_err is not None:
            return ser_err

        diff_text = _unified_diff(raw_content, new_content, rel_path)
        reindent_count = _count_reindented_lines(diff_text)

        preview = _maybe_build_confirm_preview(
            options,
            normalized,
            image.text or "",
            new_content,
            rel_path,
            action,
            yaml_path,
            diff_text,
            reindent_count,
        )
        if preview is not None:
            return preview

        meta = await commit(new_content)
        modified_dt = datetime.fromtimestamp(meta["mtime"])

        _LOGGER.info(
            "YAML config edited: %s (action=%s, key=%s)",
            rel_path,
            action,
            yaml_path,
        )

        result: dict[str, Any] = {
            "success": True,
            "file": rel_path,
            "action": action,
            "yaml_path": yaml_path,
            "size": meta["size"],
            "modified": modified_dt.isoformat(),
            "written": True,
            "diff": diff_text,
        }
        if reindent_count:
            result["warnings"] = [_reindent_warning(reindent_count)]

        # Surface the post-edit action required to activate the change
        if guard is not None:
            await guard(kind == "theme")
        result.update(await _resolve_post_action(hass, kind, path_parts, context))
        result.update(await _run_config_check(hass, rel_path))
        return result

    except PermissionError:
        _LOGGER.error("Permission denied editing: %s", rel_path)
        return {
            "success": False,
            "error": f"Permission denied: {rel_path}",
        }
    except OSError as err:
        _LOGGER.error("Error editing YAML config %s: %s", rel_path, err)
        return {
            "success": False,
            "error": str(err),
        }


def _caller_extra_allowed_keys(options: dict[str, Any]) -> frozenset[str]:
    """Return the operator-configured extra write keys for this call (#1887).

    ``YAML_KEY_DENYLIST`` members are dropped here so they can never widen the
    ``allowed`` set that the generic "not in the allowed list" rejection lists
    for some *other* invalid key. A direct write to a denied key does not rely
    on this drop: ``_parse_and_validate_yaml_path`` checks the denylist first
    and returns the categorical floor message before any allow-set is
    consulted. Keys already covered by the built-in sets are harmless
    duplicates and stay.
    """
    return frozenset(
        key
        for key in options.get("extra_allowed_keys", [])
        if key and key not in YAML_KEY_DENYLIST
    )


def _effective_extra_allowed_keys(
    hass: HomeAssistant, options: dict[str, Any]
) -> frozenset[str]:
    """Union the per-call wire keys with the component-stored keys (#1887).

    The ha-mcp server passes its own ``HA_MCP_EXTRA_YAML_KEYS`` on the wire; the
    component's own options flow / settings store contributes
    :func:`_current_extra_yaml_keys`. Both sources are denylist-filtered - the
    wire in :func:`_caller_extra_allowed_keys`, the store on save/load - and
    :func:`_parse_and_validate_yaml_path` re-checks the deny floor regardless,
    so the union can never lift a forbidden key. The store side is filtered
    again here purely as defense in depth.
    """
    stored = frozenset(
        key for key in _current_extra_yaml_keys(hass) if key not in YAML_KEY_DENYLIST
    )
    return _caller_extra_allowed_keys(options) | stored


class YamlEditor:
    def __init__(self, files: FileAccess) -> None:
        self.files = files

    async def edit(
        self,
        options: dict[str, Any],
        *,
        guard: Guard | None = None,
        before_write: BeforeWrite | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        return await self.files.run_owned(
            partial(self._locked_edit, dict(options), before_write, context),
            guard,
            writes=False,
        )

    async def _locked_edit(
        self,
        options: dict[str, Any],
        before_write: BeforeWrite | None,
        context: Any,
        *,
        guard: Guard | None,
        state: FileWriteState,
    ) -> dict[str, Any]:
        normalized = posixpath.normpath(options["file"])
        target = self.files.config_dir / normalized
        resolved = await self.files._io(guard, False, target.resolve)
        async with registry_update_lock("file", str(resolved)):
            return await self._edit(
                options, before_write, guard, state, normalized, resolved, context
            )

    async def _validate(
        self, options: dict[str, Any], normalized: str, guard: Guard | None
    ) -> tuple[str, tuple[str, ...]]:
        hass = self.files.hass
        await self.files._authorize(guard, False)
        is_package, is_theme, error = await _classify_edit_yaml_target(
            hass, options["file"], normalized
        )
        if error is None:
            error = await self.files._io(
                guard,
                False,
                _check_edit_yaml_containment,
                self.files.config_dir,
                options["file"],
                normalized,
                is_theme,
            )
        if error is not None:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                str(error["error"]),
                data=error,
                status="not_started",
            )
        if options["action"] == "replace_file":
            return "whole", ()
        if error := _check_packages_key_gate(options, options["yaml_path"], is_package):
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                str(error["error"]),
                data=error,
                status="not_started",
            )
        kind, parts, path_error = _parse_and_validate_yaml_path(
            options["yaml_path"],
            is_package=is_package,
            is_theme=is_theme,
            extra_allowed_keys=_effective_extra_allowed_keys(hass, options),
        )
        if path_error is not None:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                path_error,
                data={"success": False, "error": path_error},
                status="not_started",
            )
        return kind, parts

    async def _edit(
        self,
        options: dict[str, Any],
        before_write: BeforeWrite | None,
        guard: Guard | None,
        state: FileWriteState,
        normalized: str,
        resolved: Path,
        context: Any,
    ) -> dict[str, Any]:
        files = self.files
        kind, parts = await self._validate(options, normalized, guard)
        image: FileImage = await files._io(
            guard, False, read_file_image, files.config_dir / normalized
        )
        if image.real_path != resolved:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "YAML target changed while waiting for its file lock",
                status="not_started",
            )

        async def commit(content: str) -> dict[str, Any]:
            await files._authorize(guard, True)
            if before_write is not None:
                await before_write(normalized, image)
            # Settings and package-folder definitions may change during backup.
            current_kind, current_parts = await self._validate(
                options, normalized, guard
            )
            if (current_kind, current_parts) != (kind, parts):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "YAML target classification changed after its before-image",
                    status="not_started",
                )
            await files._authorize(guard, True)

            def write() -> dict[str, Any]:
                error = _check_edit_yaml_containment(
                    files.config_dir, options["file"], normalized, kind == "theme"
                )
                if error is not None:
                    raise ToolFailure(
                        "ACCESS_DENIED",
                        str(error["error"]),
                        data=error,
                        status="not_started",
                    )
                return _replace_file_sync(
                    files.config_dir / normalized, content, expected=image, state=state
                )

            meta: dict[str, Any] = await files.hass.async_add_executor_job(write)
            return meta

        if kind == "whole":
            return await _apply_replace_file(
                files.hass,
                files.config_dir,
                options["file"],
                normalized,
                options["action"],
                options["yaml_path"],
                options.get("content"),
                commit,
            )
        parsed, error = await _parse_edit_content(
            files.hass, options["action"], options.get("content"), kind
        )
        if error is not None:
            return error
        return await _apply_yaml_key_edit(
            files.hass,
            files.config_dir,
            options,
            options["file"],
            normalized,
            options["action"],
            options["yaml_path"],
            kind,
            parts,
            parsed,
            image,
            commit,
            guard,
            context,
        )
