"""Compatibility exports for the shared entry flow implementation."""

from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.runtime.flows import entry as _shared

OptionsFlowError = _shared.OptionsFlowError
CreationFlowError = _shared.CreationFlowError
_reject_redaction_sentinels = legacy_flow(_shared._reject_redaction_sentinels)
set_config_subentry = legacy_flow(_shared.set_config_subentry)
update_config_entry_options = legacy_flow(_shared.update_config_entry_options)
COMPONENT_DOMAIN = _shared.COMPONENT_DOMAIN
create_config_entry = legacy_flow(_shared.create_config_entry)
create_flow_helper = legacy_flow(_shared.create_flow_helper)


__all__ = [
    "COMPONENT_DOMAIN",
    "CreationFlowError",
    "OptionsFlowError",
    "_reject_redaction_sentinels",
    "create_config_entry",
    "create_flow_helper",
    "set_config_subentry",
    "update_config_entry_options",
]
