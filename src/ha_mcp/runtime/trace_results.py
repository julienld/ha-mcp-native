"""Pure trace summaries, section selection and diagnostic wording."""

import json
from typing import Any, Literal

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


def trace_target(automation_id: str) -> tuple[str, str]:
    if automation_id.startswith("automation."):
        domain = "automation"
    elif automation_id.startswith("script."):
        domain = "script"
    else:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid entity_id format: {automation_id}",
                details="Entity ID must start with 'automation.' or 'script.'",
                context={"automation_id": automation_id},
            )
        )
    return domain, automation_id.split(".", 1)[1]


def _diagnostic_suggestion(diagnostics: dict[str, Any], domain: str) -> str:
    """Build a helpful hint explaining why no traces are available."""
    if not diagnostics["automation_enabled"]:
        return (
            f"The {domain} is currently disabled (state: off). "
            "Enable it to start recording traces."
        )
    if diagnostics["last_triggered"] is None:
        return (
            f"The {domain} has never been triggered. "
            "Wait for it to trigger or manually trigger it to generate traces."
        )
    if not diagnostics["trace_storage_enabled"]:
        return (
            "Trace storage is disabled for this automation. "
            "Set 'stored_traces' to a positive number in the automation config."
        )
    return (
        "Traces may have been cleared or expired. "
        "Home Assistant only keeps a limited number of recent traces."
    )


def _format_trace_list(
    automation_id: str,
    traces: list[dict[str, Any]],
    limit: int,
    diagnostics: dict[str, Any] | None = None,
    *,
    offset: int = 0,
    order: Literal["newest", "oldest"] = "newest",
) -> dict[str, Any]:
    """Format trace list for AI consumption.

    Args:
        automation_id: The automation or script entity_id
        traces: List of trace data from Home Assistant (oldest-first)
        limit: Maximum number of traces to include
        diagnostics: Optional diagnostic information when traces are empty
        offset: Number of traces to skip from the start of the requested order
        order: 'newest' (default) returns most-recent first; 'oldest' chronological
    """
    # HA's trace/list returns traces oldest-first. Pick a window from the end
    # for newest-first, or from the start for oldest-first, with offset for
    # pagination through stored traces beyond `limit`.
    if order == "newest":
        end = len(traces) - offset
        start = max(end - limit, 0)
        window = list(reversed(traces[start:end])) if end > 0 else []
    else:
        window = traces[offset : offset + limit]

    formatted_traces = []
    for trace in window:
        # Extract key information from trace
        trace_info: dict[str, Any] = {
            "run_id": trace.get("run_id"),
            "timestamp": trace.get("timestamp"),
            "state": trace.get("state"),
        }

        # Extract trigger description if available
        trigger_str = trace.get("trigger")
        if trigger_str:
            trace_info["trigger"] = trigger_str

        # Check for errors
        error = trace.get("error")
        if error:
            trace_info["error"] = error

        # Add script-specific execution duration
        if "script_execution" in trace:
            trace_info["execution"] = trace.get("script_execution")

        formatted_traces.append(trace_info)

    result: dict[str, Any] = {
        "success": True,
        "automation_id": automation_id,
        "trace_count": len(formatted_traces),
        "total_available": len(traces),
        "offset": offset,
        "order": order,
        "has_more": offset + len(formatted_traces) < len(traces),
        "traces": formatted_traces,
        "hint": "Use run_id with this tool to get detailed trace information",
    }

    # Include diagnostics when traces are empty
    if diagnostics is not None and len(traces) == 0:
        result["diagnostics"] = diagnostics

    return result


def _format_detailed_trace(
    automation_id: str,
    run_id: str,
    trace: dict[str, Any],
    *,
    deduplicate: bool = True,
    detailed: bool = False,
    sections: str | None = None,
) -> dict[str, Any]:
    """Format detailed trace for AI consumption."""
    domain = "automation" if automation_id.startswith("automation.") else "script"
    result: dict[str, Any] = {
        "success": True,
        "automation_id": automation_id,
        "run_id": run_id,
        "timestamp": trace.get("timestamp"),
        "state": trace.get("state"),
    }

    raw_trace = trace.get("trace", {})
    triggers, conditions, actions = _classify_trace_steps(raw_trace, domain)

    _populate_trigger_info(result, triggers, trace)
    _populate_condition_results(result, conditions)
    _populate_action_trace(result, actions, deduplicate)

    # Add context with trigger variables for template debugging
    config = trace.get("config", {})
    if config:
        # Include config summary for context
        result["config_summary"] = {
            "alias": config.get("alias"),
            "mode": config.get("mode", "single"),
        }

    # Check for overall error
    if trace.get("error"):
        result["error"] = trace["error"]

    # Add script execution info if present
    if trace.get("script_execution"):
        result["script_execution"] = trace["script_execution"]

    # In detailed mode, include logbook entries and context metadata
    # that are essential for debugging but omitted by default to save context.
    if detailed:
        if "logbook_entries" in trace:
            result["logbook_entries"] = trace["logbook_entries"]
        if trace.get("context"):
            result["context"] = trace["context"]

    if sections:
        result = _filter_trace_sections(result, sections)

    return result


def _classify_trace_steps(
    raw_trace: dict[str, Any], domain: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Group flat trace path entries into sorted trigger/condition/action lists."""
    buckets: dict[str, list[dict[str, Any]]] = {
        "trigger": [],
        "condition": [],
        "action": [],
    }

    # Home Assistant trace data is stored as a flat dict with path keys
    # e.g. "trigger/0": [...], "action/0": [...], "action/0/1": [...]
    for path, steps in raw_trace.items():
        if not isinstance(steps, list):
            continue

        for step in steps:
            category = _categorize_step_path(path, domain)
            if category is None:
                continue
            # Create a copy to avoid modifying original
            step_info = step.copy()
            step_info["path"] = path
            buckets[category].append(step_info)

    # Sort by timestamp (if available) or path to maintain execution order
    def sort_key(item: dict[str, Any]) -> tuple[str, str]:
        return (item.get("timestamp", ""), item.get("path", ""))

    for steps_list in buckets.values():
        steps_list.sort(key=sort_key)

    return buckets["trigger"], buckets["condition"], buckets["action"]


def _categorize_step_path(path: str, domain: str) -> str | None:
    """Map a trace path key to 'trigger', 'condition', 'action', or None."""
    if path == "trigger" or path.startswith("trigger/"):
        return "trigger"
    if path == "condition" or path.startswith("condition/"):
        return "condition"
    if path == "action" or path.startswith(("action/", "sequence/")):
        return "action"
    if domain == "script" and path.split("/", maxsplit=1)[0].isdigit():
        return "action"
    return None


def _filter_trace_sections(result: dict[str, Any], sections: str) -> dict[str, Any]:
    """Filter a formatted trace down to the requested sections plus metadata."""
    # Maps user-facing section names to result dict keys.
    section_key_map = {
        "trigger": "trigger",
        "conditions": "condition_results",
        "actions": "action_trace",
        "config": "config_summary",
        "error": "error",
        "logbook": "logbook_entries",
        "context": "context",
    }
    requested = {s.strip().lower() for s in sections.split(",")}
    keep_keys = {section_key_map[s] for s in requested if s in section_key_map}
    # Always keep metadata keys
    keep_keys |= {
        "success",
        "automation_id",
        "run_id",
        "timestamp",
        "state",
        "script_execution",
    }
    return {k: v for k, v in result.items() if k in keep_keys}


def _populate_trigger_info(
    result: dict[str, Any],
    triggers: list[dict[str, Any]],
    trace: dict[str, Any],
) -> None:
    """Extract trigger information and add to result dict."""
    if triggers:
        trigger_step = triggers[0]
        trigger_vars = trigger_step.get("changed_variables", {}).get("trigger", {})
        if not trigger_vars:
            trigger_vars = trigger_step.get("variables", {}).get("trigger", {})

        result["trigger"] = {
            "platform": trigger_vars.get("platform"),
            "description": trigger_vars.get("description"),
        }
        if "to_state" in trigger_vars:
            result["trigger"]["to_state"] = (trigger_vars.get("to_state") or {}).get(
                "state"
            )
        if "from_state" in trigger_vars:
            result["trigger"]["from_state"] = (
                trigger_vars.get("from_state") or {}
            ).get("state")
        if "entity_id" in trigger_vars:
            result["trigger"]["entity_id"] = trigger_vars["entity_id"]
        if "error" in trigger_step:
            result["trigger"]["error"] = trigger_step["error"]

    if "trigger" not in result and "trigger" in trace:
        result["trigger"] = {"description": trace["trigger"]}


def _populate_condition_results(
    result: dict[str, Any],
    conditions: list[dict[str, Any]],
) -> None:
    """Extract condition results and add to result dict."""
    if conditions:
        condition_results = []
        for cond in conditions:
            cond_result = {
                "result": cond.get("result", {}).get("result"),
                "path": cond.get("path"),
            }
            if "timestamp" in cond:
                cond_result["timestamp"] = cond["timestamp"]
            # HA 2026.7+ always records template errors on the failing step
            # (core #172917) — dropping this field would hide the reason a
            # condition evaluated to None.
            if "error" in cond:
                cond_result["error"] = cond["error"]
            condition_results.append(cond_result)
        result["condition_results"] = condition_results


def _populate_action_trace(
    result: dict[str, Any],
    actions: list[dict[str, Any]],
    deduplicate: bool,
) -> None:
    """Extract action trace with optional variable deduplication."""
    if not actions:
        return

    action_results = []
    last_vars_fingerprint: str | None = None
    for action in actions:
        action_info: dict[str, Any] = {
            "path": action.get("path"),
        }

        if "timestamp" in action:
            action_info["timestamp"] = action["timestamp"]

        action_result = action.get("result", {})
        if action_result:
            action_info["result"] = action_result

        if "error" in action:
            action_info["error"] = action["error"]

        variables = action.get("variables") or action.get("changed_variables", {})
        useful_vars, last_vars_fingerprint = _select_action_variables(
            variables, deduplicate, last_vars_fingerprint
        )
        if useful_vars is not None:
            action_info["variables"] = useful_vars

        if "child_id" in action:
            action_info["child_id"] = action["child_id"]

        action_results.append(action_info)

    result["action_trace"] = action_results


def _select_action_variables(
    variables: dict[str, Any],
    deduplicate: bool,
    last_fingerprint: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Pick the variables to record for an action step, deduplicating if enabled.

    Returns the variables to include (or None to omit) alongside the fingerprint
    to carry into the next step.
    """
    if not variables or "trigger" in variables:
        return None, last_fingerprint

    useful_vars = {k: v for k, v in variables.items() if v is not None}
    if not useful_vars:
        return None, last_fingerprint

    if not deduplicate:
        return useful_vars, last_fingerprint

    try:
        fingerprint = json.dumps(useful_vars, sort_keys=True, default=str)
    except (TypeError, ValueError):
        fingerprint = str(useful_vars)

    if fingerprint == last_fingerprint:
        return None, last_fingerprint
    return useful_vars, fingerprint
