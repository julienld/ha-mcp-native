"""A preview cannot authorize replacing a subtree that changed in the meantime."""

import importlib
from types import SimpleNamespace

from .test_native_engine_backend import native as native
from .test_native_file_access import files as files


async def test_replacement_confirmation_is_bound_to_the_replaced_before_image(files):
    _module, owner, _target = files
    component = importlib.import_module("custom_components.ha_mcp_tools")
    target = owner.config_dir / "configuration.yaml"
    target.write_text("utility_meter:\n  old:\n    source: sensor.initial\n")
    handler = component._build_edit_yaml_config_handler(owner.hass)
    arguments = {
        component.CALLER_TOKEN_FIELD: "fixture-capability",
        "file": "configuration.yaml",
        "action": "replace",
        "yaml_path": "utility_meter",
        "content": "requested:\n  source: sensor.new\n",
        "require_confirm": True,
    }
    preview = await handler(SimpleNamespace(data=arguments))
    assert preview["preview"] is True and not preview["written"]
    external = "utility_meter:\n  old:\n    source: sensor.external\n"
    target.write_text(external)
    result = await handler(
        SimpleNamespace(data={**arguments, "confirm_token": preview["confirm_token"]})
    )
    assert result.get("preview") is True and result["written"] is False
    assert result["confirm_token_mismatch"] is True
    assert result["confirm_token"] != preview["confirm_token"]
    assert target.read_text() == external
