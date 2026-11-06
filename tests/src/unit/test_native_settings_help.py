"""A native capability note replaces help that promises unavailable transports."""

from pathlib import Path

from ._js_harness import run_script


def test_native_help_replaces_transport_promise_and_keeps_operational_facts() -> None:
    script = (
        Path(__file__).resolve().parents[3]
        / "src/ha_mcp/settings_ui/settings_js/01_i18n_state.js"
    ).read_text(encoding="utf-8")
    result = run_script(
        script,
        invoke="document.body.textContent = helpWithFacts('Raw REST available', {help_override: 'Native tool calls only', min: 1, max: 20, restart_required: true});",
    )
    assert "Native tool calls only" in result.dom
    assert "Raw REST available" not in result.dom
    assert "Range" in result.dom and "Restart required" in result.dom
