"""Legacy facade for shared helper-field descriptions."""

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime import helper_describe as _shared

logger = _shared.logger
MAX_OPTIONS = _shared.MAX_OPTIONS
_SELECTOR_KEYS = _shared._SELECTOR_KEYS
_SERIALIZED_KEYS = _shared._SERIALIZED_KEYS
_option_values = legacy_helper_flow(_shared._option_values)
compact_field = legacy_helper_flow(_shared.compact_field)
compact_schema = legacy_helper_flow(_shared.compact_schema)
_with_static_hints = legacy_helper_flow(_shared._with_static_hints)
_field_help = legacy_helper_flow(_shared._field_help)
_with_help = legacy_helper_flow(_shared._with_help)
_describe_form = legacy_helper_flow(_shared._describe_form)
_MORE_FORMS = _shared._MORE_FORMS
_later_forms = legacy_helper_flow(_shared._later_forms)
_describe_flow = legacy_helper_flow(_shared._describe_flow)
_reject_menu_choice = legacy_helper_flow(_shared._reject_menu_choice)
_stored_item = legacy_helper_flow(_shared._stored_item)
_describe_simple = legacy_helper_flow(_shared._describe_simple)
describe_helper = legacy_helper_flow(_shared.describe_helper)
describe_helper_response = legacy_helper_flow(_shared.describe_helper_response)
