"""Create a real disposable entry for Core device ownership checks."""

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .compat import validation


class NativeFixtureFlow(ConfigFlow, domain="native_fixture"):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=validation.Schema({validation.Required("name"): str}),
            )
        return self.async_create_entry(title=user_input["name"], data=user_input)
