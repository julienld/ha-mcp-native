"""Shared report assembly: diagnostics in, reviewable text and URLs out."""

from typing import Any
from urllib.parse import quote_plus

from .projection import project_fields
from .report_templates import (
    REPORT_END_MARKER,
    _build_issue_url,
    _format_client_host_for_template,
    _format_client_info_for_template,
    _format_server_entry_value,
    _format_supervisor_value,
    _format_tools_entry_value,
    _format_version_value,
    _generate_agent_behavior_template,
    _generate_feature_request_template,
    _generate_runtime_bug_template,
    _generate_search_keywords,
    _ReportText,
    _sanitize_log_text,
    _suggested_title,
)

_AGENT_SIDE_TEMPLATES = {
    "agent_behavior": _generate_agent_behavior_template,
    "feature_request": _generate_feature_request_template,
}


_TITLE_PREFIXES = {
    "runtime_bug": "[BUG] ",
    "agent_behavior": "[AGENT] ",
    "feature_request": "[FEATURE] ",
}


_ISSUE_TITLE_MAX_CHARS = 256


MISSING_TOOL_HINT = (
    "MISSING/UNAVAILABLE TOOL? If the problem is that a tool you expected is not "
    "in your tool list (can't find it, can't call it), this is very likely NOT a "
    "bug. MCP clients cache the tool list when they connect and do NOT pick up "
    "newly enabled tools until the connection is refreshed. Server-side logs "
    "saying a tool is 'enabled' do NOT mean the client has loaded it. Ask the "
    "user to refresh their MCP connection first: in Claude Desktop / claude.ai "
    "use the connector's refresh option, or simply disconnect and reconnect it; "
    "in Claude Code run /mcp and reconnect the server. Only file a bug if the "
    "tool is still missing after a refresh."
)


KNOWN_CLIENT_ISSUES_HINT = (
    "KNOWN CLAUDE DESKTOP ISSUES (not ha-mcp bugs; check before filing):\n"
    "1. A write tool (dashboard, automation, script, helper, service call) "
    "hangs and the client reports a 4-minute timeout with no result, while "
    "read tools work. Cause: Claude Desktop's manual-approval dialog for a "
    "local MCP server accepts the click before the call is fully generated "
    "and silently drops the call. The server never receives it. Tracked in "
    "homeassistant-ai/ha-mcp#2367 and anthropics/claude-code#92014. Tell the "
    "user to wait a few seconds before clicking Allow once. Claude Desktop "
    "is only affected when manual approval is enabled, not when auto-approve "
    "or 'Always allow' is on. A dropped call never reached Home "
    "Assistant, but the same timeout can also hide a call that landed and "
    "lost only its result (anthropics/claude-code#80012), so read the target "
    "back before repeating a write that is not idempotent.\n"
    "2. Every tool call fails with 'expected nonoptional, received "
    "undefined' when an optional parameter is omitted. Cause: Claude Desktop "
    "2.110.0 rejects omitted optional MCP parameters. Tracked in "
    "homeassistant-ai/ha-mcp#2472 and anthropics/claude-code#94608 (fixed "
    "upstream). Tell the user to update Claude Desktop to 2.2553.0 or later; "
    "if they cannot update, downgrading to 1.52386.6 (links in #2472) also "
    "works.\n"
    "Only file a bug if the problem persists after the matching workaround."
)


def _format_websockets_dependency_value(diagnostic_info: dict[str, Any]) -> str:
    """Render the websockets probe for the human-pasteable report.

    Mirrors the sibling ``_format_*_value`` helpers: a degraded probe stays
    visibly degraded instead of disappearing — for the #2135/#2146 failure
    class this line IS the diagnosis, so it must survive into the report a
    user pastes.
    """
    state = diagnostic_info.get("websockets_dependency") or {}
    if state.get("not_used"):
        return str(state["not_used"])
    if not state:
        return "not probed"
    if state.get("vendored_import_ok"):
        value = f"vendored {state.get('vendored_version') or 'unknown'}"
        if state.get("vendored_c_speedups") is False:
            value += " (pure-Python)"
    else:
        value = (
            f"vendored BROKEN: {state.get('vendored_import_error', 'import failed')}"
        )
    if (error := state.get("shared_metadata_error")) is not None:
        shared = f"metadata unreadable: {error}"
    elif (version := state.get("shared_metadata_version")) is not None:
        shared = f"{version} per dist metadata"
    else:
        shared = "absent"
    return f"{value} | shared env copy: {shared}"


def _build_formatted_report(
    diagnostic_info: dict[str, Any],
    mcp_transport: str,
    client_info: dict[str, str],
    platform_info: dict[str, str],
    config_toggles: dict[str, Any],
    startup_logs: list[dict[str, Any]],
    startup_log_summary: str,
    recent_logs: list[dict[str, Any]],
    log_summary: str,
    addon_logs: str,
    core_error_log: str,
) -> str:
    report_lines = [
        "=== ha-mcp Bug Report Info ===",
        "",
        f"ha-mcp Version: {_format_version_value(diagnostic_info)}",
        f"Custom Component: {diagnostic_info.get('component_version') or 'not detected (not installed, or probe failed)'}",
        f"File & YAML Tools entry: {_format_tools_entry_value(diagnostic_info)}",
        f"In-process Server entry: {_format_server_entry_value(diagnostic_info)}",
        f"Installation Method: {diagnostic_info['installation_method']}",
        f"MCP Transport: {mcp_transport}",
        f"MCP Client: {_format_client_info_for_template(client_info)}",
        f"MCP Client Host: {_format_client_host_for_template(diagnostic_info)}",
        f"Operating System: {platform_info['os']} {platform_info['os_release']} ({platform_info['architecture']})",
        f"Python Version: {platform_info['python_version']}",
        f"Home Assistant Version: {diagnostic_info['home_assistant_version']}",
        f"Supervisor: {_format_supervisor_value(diagnostic_info)}",
        f"Connection Status: {diagnostic_info['connection_status']}",
        f"Entity Count: {diagnostic_info['entity_count']}",
        f"websockets Dependency: {_format_websockets_dependency_value(diagnostic_info)}",
    ]
    if "location_name" in diagnostic_info:
        report_lines.append(f"Location Name: {diagnostic_info['location_name']}")
    if "time_zone" in diagnostic_info:
        report_lines.append(f"Time Zone: {diagnostic_info['time_zone']}")
    if config_toggles:
        report_lines.extend(["", "=== ha-mcp Config Toggles ==="])
        for key, value in config_toggles.items():
            report_lines.append(f"  {key}: {value}")
    report_lines.append(
        f"Tool Policy: {diagnostic_info.get('tool_policy', 'not probed')}"
    )
    if startup_logs:
        report_lines.extend(
            [
                "",
                f"=== Startup Logs ({len(startup_logs)} entries) ===",
                startup_log_summary,
            ]
        )
    if recent_logs:
        report_lines.extend(
            [
                "",
                f"=== Recent Tool Calls ({len(recent_logs)} entries) ===",
                log_summary,
            ]
        )
    if addon_logs:
        report_lines.extend(["", "=== Add-on Container Logs ===", addon_logs])
    if core_error_log:
        report_lines.extend(["", "=== Home Assistant Error Log ===", core_error_log])
    return "\n".join(report_lines)


def _format_logs_for_report(logs: list[dict[str, Any]]) -> str:
    """Format log entries for inclusion in a bug report."""
    if not logs:
        return "(No recent logs available)"

    lines = []
    for log in logs:
        timestamp = log.get("timestamp", "?")[:19]  # Trim to seconds
        tool_name = log.get("tool_name", "unknown")
        success = "OK" if log.get("success") else "FAIL"
        exec_time = log.get("execution_time_ms", 0)
        error = log.get("error_message", "")

        duration = (
            f"{exec_time:.0f}ms"
            if isinstance(exec_time, (int, float))
            else "timing unavailable"
        )
        line = f"  {timestamp} | {tool_name} | {success} | {duration}"
        if error:
            # Sanitize before truncating so secrets straddling the cut survive redaction.
            error_short = _sanitize_log_text(str(error))[:100]
            line += f" | Error: {error_short}"
        lines.append(line)

    return "\n".join(lines)


def _format_startup_logs(logs: list[dict[str, Any]]) -> str:
    """Format startup log entries for inclusion in a bug report."""
    if not logs:
        return "(No startup logs available)"

    lines = []
    for log in logs:
        elapsed = log.get("elapsed_seconds", 0)
        level = log.get("level", "INFO")
        logger_name = log.get("logger", "")
        message = log.get("message", "")

        # Sanitize before truncating so secrets straddling the cut survive redaction.
        message = _sanitize_log_text(message)
        if len(message) > 200:
            message = message[:200] + "..."

        line = f"  +{elapsed:05.2f}s | {level:5} | {logger_name}: {message}"
        lines.append(line)

    return "\n".join(lines)


def _issue_title(report_type: str, text: _ReportText, suggested_title: str) -> str:
    """Return a one-line issue title carrying the report type's prefix."""
    prefix = _TITLE_PREFIXES[report_type].strip()
    title = " ".join((text.title or "").split())
    if title.upper().startswith(prefix):
        title = title[len(prefix) :].strip()
    # A title that was only the prefix or whitespace says nothing.
    title = f"{prefix} {title or ' '.join(suggested_title.split())}"
    if len(title) > _ISSUE_TITLE_MAX_CHARS:
        title = title[: _ISSUE_TITLE_MAX_CHARS - 3] + "..."
    return title


def _generate_anonymization_guide() -> str:
    """Generate privacy/anonymization instructions."""
    return """## Anonymization Guide

Before submitting your bug report, please review and anonymize:

### MUST ANONYMIZE (security-sensitive):
- API tokens, passwords, secrets -> Replace with "[REDACTED]"
- IP addresses (internal/external) -> Replace with "192.168.x.x" or "[IP]"
- MAC addresses -> Replace with "[MAC]"
- Email addresses -> Replace with "user@example.com"
- Phone numbers -> Replace with "[PHONE]"

### CONSIDER ANONYMIZING (privacy-sensitive):
- Location names (city, address) -> Replace with generic names like "Home" or "[LOCATION]"
- Device names that reveal personal info -> Replace with "Device 1", "Light 1", etc.
- Person names in entity IDs -> Replace with "person.user1"
- Calendar/todo items with personal details -> Summarize without specifics

### KEEP AS-IS (helpful for debugging):
- Entity domains (light, switch, sensor, etc.)
- Device types and capabilities
- Automation/script structure (triggers, conditions, actions)
- Error messages (but check for secrets in them)
- Timestamps and durations
- State values (on/off, numeric values, etc.)
- Home Assistant and ha-mcp versions

### Example anonymization:
BEFORE: "light.juliens_bedroom" with token "eyJhbG..."
AFTER:  "light.bedroom_1" with token "[REDACTED]"

The goal is to preserve enough detail to reproduce and fix the bug
while protecting your personal information and security.
"""


def render_report(
    diagnostic_info: dict[str, Any],
    recent_logs: list[dict[str, Any]],
    startup_logs: list[dict[str, Any]],
    addon_logs: str,
    core_error_log: str,
    report_type: str,
    text: _ReportText,
    parsed_fields: list[str] | None,
) -> dict[str, Any]:
    mcp_transport = diagnostic_info["mcp_transport"]
    client_info = diagnostic_info["mcp_client_info"]
    platform_info = diagnostic_info["platform"]
    config_toggles = diagnostic_info["config_toggles"]
    log_summary = _format_logs_for_report(recent_logs)
    startup_log_summary = _format_startup_logs(startup_logs)

    # Build the formatted report
    formatted_report = _build_formatted_report(
        diagnostic_info,
        mcp_transport,
        client_info,
        platform_info,
        config_toggles,
        startup_logs,
        startup_log_summary,
        recent_logs,
        log_summary,
        addon_logs,
        core_error_log,
    )

    # Without the agent's title, a fallback keeps reports from being filed
    # under a bare prefix: the last error for a bug, a fixed name for a
    # feature request.
    suggested_title = _suggested_title(report_type, diagnostic_info, recent_logs)
    issue_title = _issue_title(report_type, text, suggested_title)

    def build_body(include_logs: bool, text_cap: int | None = None) -> str:
        if report_type == "runtime_bug":
            return _generate_runtime_bug_template(
                diagnostic_info,
                log_summary,
                startup_log_summary,
                recent_logs,
                startup_logs,
                addon_logs=addon_logs,
                core_error_log=core_error_log,
                text=text,
                include_logs=include_logs,
                text_cap=text_cap,
            )
        return _AGENT_SIDE_TEMPLATES[report_type](
            diagnostic_info,
            log_summary,
            text=text,
            include_logs=include_logs,
            text_cap=text_cap,
        )

    issue_body = build_body(include_logs=True)
    issue_url = _build_issue_url(
        issue_title, lambda cap: build_body(include_logs=False, text_cap=cap)
    )

    # Generate search keywords and URLs for duplicate check
    search_keywords = _generate_search_keywords(
        diagnostic_info,
        recent_logs,
        text.title if report_type == "feature_request" else None,
    )
    duplicate_check_urls = [
        f"https://github.com/homeassistant-ai/ha-mcp/issues?q=is%3Aissue+{quote_plus(keyword)}"
        for keyword in search_keywords[:3]  # Limit to top 3 keywords
    ]

    result: dict[str, Any] = {
        "success": True,
        "diagnostic_info": diagnostic_info,
        "recent_logs": recent_logs,
        # Scrub the startup-log *messages* — they aren't sanitized at source,
        # so the connect-URL secret path can otherwise surface here.
        # recent_logs is also returned, but its sensitive *parameters* are
        # key-masked at the logging chokepoint and its error_message is
        # returned as-is to the trusted client by design (see SECURITY.md);
        # formatted_report and addon_logs already route through
        # _sanitize_log_text. This copies entries — the underlying log
        # records stay raw.
        "startup_logs": [
            {**entry, "message": _sanitize_log_text(entry.get("message", ""))}
            for entry in startup_logs
        ],
        "addon_logs": addon_logs,
        # Already sanitized + truncated by _fetch_core_error_log.
        "core_error_log": core_error_log,
        "log_count": len(recent_logs),
        "startup_log_count": len(startup_logs),
        "formatted_report": formatted_report,
        "issue_title": issue_title,
        "issue_body": issue_body,
        "issue_url": issue_url,
        "anonymization_guide": _generate_anonymization_guide(),
        "duplicate_check_urls": duplicate_check_urls,
        "missing_tool_hint": MISSING_TOOL_HINT,
        "known_client_issues_hint": KNOWN_CLIENT_ISSUES_HINT,
        "instructions": (
            "WORKFLOW FOR FILING A REPORT:\n\n"
            "0. **PRE-CHECK — is the problem a missing/unavailable tool?** If "
            "the user's issue is that a tool they expected is missing or "
            "cannot be called, DO NOT file a bug yet. See the "
            "`missing_tool_hint` field: the likely cause is a stale MCP "
            "client tool list (not a server bug), fixed by refreshing or "
            "reconnecting the MCP connection. Only continue with this report "
            "if the tool is still missing after the user refreshes.\n\n"
            "0b. **PRE-CHECK — is it a known Claude Desktop bug?** A write "
            "tool that hangs to a 4-minute timeout, or every call failing "
            "with 'expected nonoptional, received undefined', is a client "
            "bug with an upstream ticket. See the `known_client_issues_hint` "
            "field for the workaround to give the user, and only continue "
            "if the problem persists after it.\n\n"
            "1. **Check for duplicates FIRST**:\n"
            "   - Use the duplicate_check_urls to search for similar issues\n"
            '   - If gh CLI is available: use `gh issue list --search "keyword"`\n'
            "   - Otherwise: inform user to check the duplicate_check_urls\n"
            "   - If duplicates found, ask user if they want to comment on existing issue instead\n\n"
            "2. **Pass the report text** (call ha_report_issue again if this "
            "call had none): report_type, title, description, user_comment, "
            "user_prompt, tool_calls, ai_model and client_app. The server builds "
            "issue_title, issue_body and issue_url from them. Anonymize the "
            "text first (step 3).\n"
            "   - report_type: 'runtime_bug' when ha-mcp errored or behaved "
            "wrongly; 'agent_behavior' when the user says YOU used the wrong "
            "tool, should have done something differently, or worked "
            "inefficiently; 'feature_request' when the user wants ha-mcp "
            "to do something it cannot do yet. For a feature request, "
            "first try to do it with the tools you have: the tool calls "
            "show the feature is missing. If unclear, ASK: 'Are you "
            "reporting a bug in ha-mcp, giving feedback on how I used the "
            "tools, or requesting a feature?'\n"
            "   - user_prompt and tool_calls: the EXACT user message and the "
            "tool call(s) that produced the problem, copied verbatim. This "
            "is the single most useful part for triage. Do not skip it.\n"
            "   - user_comment: ASK the user to say in their own words what "
            "went wrong or what bothered them, and pass it verbatim. Never "
            "write it yourself: maintainers need the user's view, not "
            "yours.\n"
            "   - ai_model: your own identity, as specific as you know it. "
            "Do not invent a version number.\n"
            "   - client_app: the **MCP Client Host:** line in issue_body is "
            'auto-detected. If it says "not detected", "stdio bridge", '
            'or the version is "unknown", ASK the user which app and '
            "version they are using (usually on the app's About screen or "
            "from its --version command) and pass THEIR answer. NEVER "
            "fill it in yourself: you cannot know the app or its version, "
            "and a guessed value sends triage the wrong way. If the user "
            'does not know, pass "unknown (user asked)". Client-side '
            "regressions often depend on the exact app release.\n\n"
            "3. **ANONYMIZE** (CRITICAL), both in the text you pass and in "
            "the logs inside issue_body:\n"
            "   a. Replace person names with generic labels (person.user1, person.user2)\n"
            "   b. Replace location names with generic names (Home, Location1)\n"
            "   c. Replace device names containing personal info (e.g., 'juliens_bedroom') with generic ones (e.g., 'bedroom_1')\n"
            "   d. Verify no tokens, passwords, or IPs are visible\n"
            "   e. Keep entity domains, error messages, and technical details\n"
            "   See anonymization_guide for full details. Apart from these "
            "replacements, pass issue_body on UNCHANGED: do not summarize, "
            "shorten or rewrite it.\n\n"
            "4. **Show the user issue_title and issue_body, then file it "
            "the first way you can.** The issue must contain issue_body "
            "itself, starting with its Auto-Generated heading: a bug "
            "report that carries a summary or your own write-up instead "
            "is closed after 24 hours.\n"
            "   a. You can act on GitHub (gh CLI, a GitHub connector, or a "
            "browser you control): file the issue only after the user says "
            "yes.\n"
            "      - gh: save issue_title and issue_body to files, then run "
            "`gh issue create --repo homeassistant-ai/ha-mcp --title "
            '"$(cat <title-file>)" --body-file <body-file>`. Never put the '
            "title text itself on the command line: the shell would run "
            "backticks or $(...) inside it.\n"
            "      - Browser: open https://github.com/homeassistant-ai/ha-mcp/issues/new "
            "in the user's signed-in browser, set the title and description "
            "fields directly (fill or set the value; do not type it key by "
            "key), and let the user review it and click Create.\n"
            "   b. You can write files: save issue_body as a .md file. Tell "
            "the user to open issue_url, replace the pre-filled description "
            "with the file's content, and click Create.\n"
            "   c. Chat only: give the user issue_url prominently. It opens "
            "a new issue with the title and report filled in, without the "
            "logs. Then show issue_body in a code block fenced with FOUR "
            "backticks (````markdown ... ````) so it stays in one piece, and "
            "ask the user to paste its log sections into the issue. You "
            "cannot edit the link's text, and its error messages come from "
            "the server: if they show personal names, tell the user to "
            "replace them in GitHub's editor. Remind them to check the "
            "pre-filled text for personal information before clicking "
            "Create.\n\n"
            f"5. The last line of issue_body is `{REPORT_END_MARKER}`. A "
            "copy without it was cut short.\n\n"
            "CRITICAL: Always ANONYMIZE before showing or filing the report!"
        ),
    }
    return project_fields(result, parsed_fields)
