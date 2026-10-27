"""A green functional bench must not hide Core warnings about HA-MCP calls."""

from scripts.native_core_warnings import findings


def test_blocking_call_attributed_to_our_component_is_a_qualification_failure():
    log = (
        "2026-10-23 12:00:00 WARNING (MainThread) [homeassistant.util.loop] "
        "Detected blocking call to open inside the event loop by custom integration "
        "'ha_mcp_tools' at custom_components/ha_mcp_tools/llm_api.py, line 327\n"
        "Traceback with arbitrary arguments that must stay out of the summary\n"
    )
    assert findings(log) == [{"line": 1, "category": "blocking_call"}]


def test_deprecation_attribution_can_follow_the_first_line_in_a_core_record():
    log = (
        "2026-10-23 12:00:00 WARNING (MainThread) [homeassistant.helpers.frame] "
        "Use of deprecated device API\n"
        '  File "/config/custom_components/ha_mcp_tools/radio.py", line 2\n'
        "2026-10-23 12:00:01 INFO (MainThread) [homeassistant.core] Setup done\n"
    )
    assert findings(log) == [{"line": 1, "category": "deprecated_core_api"}]


def test_unrelated_deprecations_and_the_generic_custom_integration_notice_are_not_ours():
    log = (
        "2026-10-23 12:00:00 WARNING (MainThread) [homeassistant.loader] "
        "We found a custom integration ha_mcp_tools which has not been tested by Home Assistant\n"
        "2026-10-23 12:00:01 WARNING (MainThread) [homeassistant.helpers.frame] "
        "Detected that custom integration 'other' calls a deprecated API\n"
        "2026-10-23 12:00:02 INFO (MainThread) [ha_mcp] A deprecated field was refused\n"
    )
    assert findings(log) == []


def test_attribution_does_not_leak_from_an_earlier_core_record():
    log = (
        "2026-10-23 12:00:00 INFO (MainThread) [homeassistant.core] "
        "Setting up custom integration 'ha_mcp_tools'\n"
        "2026-10-23 12:00:01 WARNING (MainThread) [homeassistant.helpers.frame] "
        "Another integration used a deprecated API\n"
    )
    assert findings(log) == []


def test_colored_core_logs_do_not_hide_a_later_blocking_warning():
    log = (
        "\x1b[32m2026-10-23 12:00:00 INFO (MainThread) [homeassistant.core] Setup\x1b[0m\n"
        "\x1b[33m2026-10-23 12:00:01 WARNING (MainThread) [homeassistant.util.loop] "
        "Detected blocking call to open by custom integration 'ha_mcp_tools'\x1b[0m\n"
    )
    assert findings(log) == [{"line": 2, "category": "blocking_call"}]
