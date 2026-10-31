"""Snapshot comparison projection shared by MCP, Assist and the settings panel."""

from typing import Any

from ha_mcp.backup_diff import (
    _TEXT_KIND,
    DiffResponse,
    DiffResponseText,
    _build_diff_response,
    _build_text_diff_response,
    _compute_json_patch,
    _summarize_patch_counts,
)

from .snapshot_files import _is_flow_helper_domain


def comparison_values(data: dict[str, Any], current: Any) -> tuple[dict[str, Any], Any]:
    """Ignore fields an older snapshot or an options restore does not write."""
    if (
        isinstance(current, dict)
        and isinstance(data["config"], dict)
        and "registry_icon" not in data["config"]
    ):
        current.pop("registry_icon", None)
    if _is_flow_helper_domain(data["domain"]) and current is not None:
        data["config"] = {key: data["config"][key] for key in ("entry_id", "options")}
        current = {key: current[key] for key in ("entry_id", "options")}
    return data, current


def diff_snapshot_values(
    name: str, data: dict[str, Any], current: Any, *, max_patch_ops: int = 200
) -> DiffResponse | DiffResponseText:
    domain = data["domain"]
    entity_id = data["entity_id"]
    stored = data["config"]
    captured_at = data.get("captured")
    if data.get("kind") == _TEXT_KIND:
        return _build_text_diff_response(
            name, domain, entity_id, captured_at, str(stored), current
        )
    if current is None:
        return _build_diff_response(
            name,
            domain,
            entity_id,
            captured_at,
            entity_missing=True,
            patch=[],
            counts=_summarize_patch_counts([]),
            truncated=False,
        )
    patch: list[dict[str, Any]] = []
    truncated = _compute_json_patch(stored, current, max_patch_ops, patch)
    return _build_diff_response(
        name,
        domain,
        entity_id,
        captured_at,
        entity_missing=False,
        patch=patch,
        counts=_summarize_patch_counts(patch),
        truncated=truncated,
    )
