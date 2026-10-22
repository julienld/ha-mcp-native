"""YAML tool validation and advisories without transport or process settings."""

import fnmatch
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure

_YAML_SKILL_FILES: tuple[str, ...] = ("references/template-guidelines.md",)


_YAML_PACKAGES_FLAG_BY_KEY = {
    "automation": "enable_yaml_packages_automation",
    "script": "enable_yaml_packages_script",
    "scene": "enable_yaml_packages_scene",
}


_HELPER_EQUIVALENT_KEYS: dict[str, str] = {
    "template": "template",
    "utility_meter": "utility_meter",
    "group": "group",
}


def _validate_yaml_action_and_content(action: str, content: str | None) -> None:
    """Raise a ToolError if ``action`` is not a recognized action, or if
    ``content`` is missing for an action that requires it.

    Split out of ``ha_config_set_yaml`` purely to keep the caller's
    complexity down; behavior and messages are unchanged.
    """
    # Validate action
    valid_actions = ("add", "replace", "remove")
    if action not in valid_actions:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}",
                suggestions=[
                    "Use action='add' to insert content under a key",
                    "Use action='replace' to overwrite a key's content",
                    "Use action='remove' to delete a key entirely",
                ],
            )
        )

    # Validate content is provided for add/replace
    if action in ("add", "replace") and not content:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"'content' is required for action '{action}'.",
                suggestions=["Provide valid YAML content to insert or replace."],
            )
        )


def _append_yaml_result_warnings(
    result: dict[str, Any], *, action: str, top_key: str, normalized_target: str
) -> None:
    """Append any post-write advisory warnings to ``result`` in place.

    Split out of ``ha_config_set_yaml`` purely to keep the caller's
    complexity down; behavior and messages are unchanged.
    """
    if "reload_error" in result:
        # Theme edits trigger frontend.reload_themes in the
        # component; the file write succeeded even when that
        # reload failed, so degrade to a warning instead of
        # masking the partial failure behind a bare success.
        result.setdefault("warnings", []).append(
            "The file was updated, but "
            f"{result.get('reload_service', 'the reload service')} "
            f"failed: {result['reload_error']}. Re-run the reload "
            '(e.g. ha_reload_core(target="themes")) or restart '
            "Home Assistant to apply the change."
        )
    # themes/*.yaml guard: there the top_key is a THEME NAME, so
    # a theme that happens to be called 'template'/'group'/
    # 'utility_meter' must not draw the helper-routing warning.
    if (
        action in ("add", "replace")
        and top_key in _HELPER_EQUIVALENT_KEYS
        and not fnmatch.fnmatch(normalized_target, "themes/*.yaml")
    ):
        result.setdefault("warnings", []).append(
            f"Best practice: '{top_key}' has a storage-mode "
            f"equivalent — ha_config_set_helper(helper_type="
            f"'{_HELPER_EQUIVALENT_KEYS[top_key]}') — which avoids "
            "raw YAML file rewrites entirely. Prefer it unless "
            "this configuration is deliberately YAML-managed "
            "(e.g. git-tracked packages)."
        )
