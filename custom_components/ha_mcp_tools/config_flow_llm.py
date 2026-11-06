"""Core LLM bridge controls shared by setup and options forms."""

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    DEFAULT_LLM_API_EXPOSURE,
    EXPOSURE_BOTH,
    EXPOSURE_FULL,
    EXPOSURE_TOOL_SEARCH,
    OPT_ENABLE_LLM_API,
    OPT_ENABLE_NATIVE_LLM_IMPORT,
    OPT_LLM_API_EXPOSURE,
    OPT_NATIVE_IMPORT_APIS,
)
from .llm_bridge_options import export_enabled, import_enabled


def schema_fields(values: Mapping[str, Any], *, exposure: bool = False) -> dict:
    """Use the same bridge controls in initial setup and subsequent options."""
    fields = {
        vol.Required(OPT_ENABLE_LLM_API, default=export_enabled(values)): bool,
        vol.Required(
            OPT_ENABLE_NATIVE_LLM_IMPORT, default=import_enabled(values)
        ): bool,
        vol.Optional(
            OPT_NATIVE_IMPORT_APIS, default=values.get(OPT_NATIVE_IMPORT_APIS, [])
        ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiple=True)),
    }
    if exposure:
        fields[
            vol.Required(
                OPT_LLM_API_EXPOSURE,
                default=values.get(OPT_LLM_API_EXPOSURE, DEFAULT_LLM_API_EXPOSURE),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=[EXPOSURE_TOOL_SEARCH, EXPOSURE_FULL, EXPOSURE_BOTH],
                translation_key="llm_api_exposure",
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
    return fields
