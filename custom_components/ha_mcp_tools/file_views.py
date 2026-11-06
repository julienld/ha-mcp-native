"""File read projections preserve masking and round-trip YAML source forms."""

from __future__ import annotations

import os
from datetime import datetime
from io import StringIO
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAMLError

from .file_io import DEFAULT_LOG_TAIL_LINES, TAILED_LOG_FILES
from .yaml_rt import apply_seq_indent, make_yaml, yaml_dumps, yaml_jsonify

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _mask_secrets_content(content: str) -> str:
    """Return secrets.yaml content with every secret value masked.

    Parses the document structurally (ruamel — the same YAML stack used
    elsewhere in this component) and emits ``key: "[MASKED]"`` for each
    top-level key. This closes the gap in the previous line-by-line regex, which
    masked only single-line ``key: value`` pairs and leaked multi-line block
    scalars (``|``, ``>``) whose continuation lines have no colon — SSH keys,
    TLS material, and service-account JSON are commonly stored that way.

    The marker is quoted because the masked text is itself valid YAML that gets
    re-parsed: read_file's ``yaml_path``/``include_parsed`` views load it, and
    an unquoted ``[MASKED]`` is flow-sequence syntax, so the parsed view would
    render the mask as the list ``["MASKED"]`` instead of a scalar.

    Fails closed: any content that cannot be parsed and masked as a key-value
    mapping is withheld rather than returned raw, so a failure on this path never
    leaks secrets. The catch is deliberately broad — masking is a security
    boundary, so every failure mode (parse error, a pathological tag constructor,
    a YAML init/threading error) must withhold, not propagate to the caller with
    the raw content still in scope.
    """
    try:
        parsed = make_yaml().load(content)
        if not isinstance(parsed, dict):
            # Empty file (None) or a top-level list/scalar: no top-level keys to
            # mask, so withhold rather than risk returning unmasked content.
            return (
                "# secrets.yaml is empty or not a key-value mapping — content withheld"
            )
        return "\n".join(f'{key}: "[MASKED]"' for key in parsed)
    except YAMLError:
        return "# secrets.yaml could not be parsed — content withheld to avoid leaking secrets"
    except Exception:  # noqa: BLE001
        return "# secrets.yaml could not be masked — content withheld to avoid leaking secrets"


def _extract_yaml_views(
    content: str, yaml_path: str, include_parsed: bool = False
) -> dict[str, Any]:
    """Return the subtree at ``yaml_path`` as round-trip text and, optionally,
    as JSON-safe parsed data — from a SINGLE parse of ``content``.

    Runs here, in the component, because the round-trip parse needs ``ruamel``
    (a component requirement) which the MCP server's runtime does not carry.
    Comments and HA tags (``!secret`` / ``!include``) are preserved in the text
    view and rendered to their source form in the parsed view — never resolved,
    so neither view can surface a secret's plaintext.

    ``subtree`` is None when the root is not a mapping or the key is absent
    (new-key write — nothing to snapshot). Malformed YAML also yields None, but
    additionally sets ``parse_error``, so a caller can tell "this file does not
    define the key" apart from "this file could not be read at all" — without
    it, one broken package silently reads as a key that is simply absent.
    ``parsed`` is present only when ``include_parsed`` and the key resolved.
    """
    views: dict[str, Any] = {"subtree": None}
    try:
        ry = make_yaml()
        # The per-thread cached instance may carry a sequence style applied
        # by a prior edit's dump on this executor thread — reset it so the
        # snapshot never inherits another file's indentation.
        apply_seq_indent(ry, None)
        node: Any = ry.load(StringIO(content))
        for seg in yaml_path.split("."):
            if not isinstance(node, dict) or seg not in node:
                return views
            node = node[seg]
        views["subtree"] = yaml_dumps(ry, node)
        if include_parsed:
            views["parsed"] = yaml_jsonify(node)
    except YAMLError as err:
        # Position only, never the error text: ruamel embeds the offending
        # source line in its message, which would put file content — possibly
        # an inline credential — into a response this path otherwise keeps
        # free of resolved values.
        mark = getattr(err, "problem_mark", None)
        where = (
            f" at line {mark.line + 1}, column {mark.column + 1}"
            if mark is not None
            else ""
        )
        return {"subtree": None, "parse_error": f"not valid YAML{where}"}
    return views


def _extract_yaml_subtree(content: str, yaml_path: str) -> str | None:
    """Return the YAML subtree at the dotted ``yaml_path`` as round-trip text.

    Used by ``read_file``'s optional ``yaml_path`` to let ha-mcp's per-edit
    auto-backup snapshot the prior value of a key before ``edit_yaml_config``
    changes it (#1579).
    """
    subtree: str | None = _extract_yaml_views(content, yaml_path)["subtree"]
    return subtree


async def _shape_read_file_response(
    hass: HomeAssistant,
    rel_path: str,
    result: dict[str, Any],
    *,
    tail_lines: int | None,
    yaml_path: str | None,
    include_parsed: bool,
) -> dict[str, Any]:
    """Shape a successful _read_file_sync result into the read_file response.

    Applies secrets masking, log tailing, optional tail, and yaml_path
    subtree extraction.
    """
    modified_dt = datetime.fromtimestamp(result["mtime"])
    content = result["content"]
    stat_size = result["size"]

    # Apply special handling for specific files
    normalized = os.path.normpath(rel_path)  # noqa: ASYNC240

    # Mask secrets.yaml. Offloaded because the first make_yaml() call on a
    # thread constructs a ruamel YAML instance, whose plugin discovery globs
    # the site-packages tree — blocking work that must stay off the loop.
    if normalized == "secrets.yaml":
        content = await hass.async_add_executor_job(_mask_secrets_content, content)

    # Log files arrive already tailed by ``_read_log_tail_sync``.
    if normalized in TAILED_LOG_FILES:
        limit = tail_lines if tail_lines else DEFAULT_LOG_TAIL_LINES
        total_lines = result["total_lines"]
        return {
            "success": True,
            "path": rel_path,
            "content": content,
            "size": stat_size,
            "modified": modified_dt.isoformat(),
            "lines_returned": min(total_lines, limit),
            "total_lines": total_lines,
            "truncated": result["truncated"],
        }

    # Apply tail for other files if requested
    full_content = content
    if tail_lines:
        lines = content.split("\n")
        if len(lines) > tail_lines:
            content = "\n".join(lines[-tail_lines:])

    response: dict[str, Any] = {
        "success": True,
        "path": rel_path,
        "content": content,
        "size": stat_size,
        "modified": modified_dt.isoformat(),
    }
    if yaml_path:
        # Extract from the UNTAILED text: tailing is a display concern, and
        # the retained tail is both likely to exclude the key and likely to
        # be invalid YAML on its own, which would report the key as absent.
        response.update(
            await hass.async_add_executor_job(
                _extract_yaml_views, full_content, yaml_path, include_parsed
            )
        )
    return response
