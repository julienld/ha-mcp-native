"""Qualify actual Frontend theme services without inventing a renderer account."""

from typing import Any


async def exercise_themes(checks: Any) -> None:
    before = await checks.control("themes")
    assert before["blue_palette"] and before["night_palette"], before
    assert "primary-color" in before["blue_palette"], before
    listed = await checks.call("ha_manage_theme", {"action": "list"})
    assert listed["success"] and listed["data"]["themes"] == before["installed"], listed
    assert listed["data"]["default_theme"] == before["default_light"], listed
    assert listed["data"]["default_dark_theme"] == before["default_dark"], listed
    checks.passed("native_themes_list_the_real_Core_frontend_library")

    changed = await checks.call(
        "ha_manage_theme", {"action": "set", "theme_name": "native_fixture_blue"}
    )
    probe = await checks.control("themes")
    assert changed["success"] and probe["default_light"] == "native_fixture_blue", (
        changed
    )
    assert changed["data"]["default_theme"] == probe["default_light"], changed
    dark = await checks.call(
        "ha_manage_theme",
        {"action": "set", "theme_name": "native_fixture_night", "mode": "dark"},
    )
    probe = await checks.control("themes")
    assert dark["success"] and probe["default_dark"] == "native_fixture_night", dark
    assert dark["data"]["default_dark_theme"] == probe["default_dark"], dark
    reset = await checks.call(
        "ha_manage_theme", {"action": "set", "theme_name": "none", "mode": "dark"}
    )
    assert (
        reset["success"] and (await checks.control("themes"))["default_dark"] is None
    ), reset
    checks.passed("native_themes_set_and_reset_defaults_via_Core_frontend_service")

    invalid = await checks.rpc(
        "tools/call",
        {
            "name": "ha_manage_theme",
            "arguments": {"action": "set", "theme_name": "native_missing_theme"},
        },
    )
    assert invalid["result"]["isError"], invalid
    unchanged = await checks.control("themes")
    assert (
        unchanged["default_light"] == probe["default_light"]
        and unchanged["default_dark"] is None
    ), unchanged
    for action in ("get_engine_theme", "set_engine_theme"):
        response = await checks.rpc(
            "tools/call",
            {
                "name": "ha_manage_theme",
                "arguments": {"action": action, "value": {}, "force": True},
            },
        )
        assert response["result"]["isError"], response
    checks.passed("native_themes_Core_validator_and_absent_renderer_refusal")

    await checks.call(
        "ha_manage_theme", {"action": "set", "theme_name": before["default_light"]}
    )
    await checks.call(
        "ha_manage_theme",
        {
            "action": "set",
            "theme_name": before["default_dark"] or "none",
            "mode": "dark",
        },
    )
