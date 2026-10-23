"""Retained imports translate neutral entity argument errors for standalone tools."""

from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime import entity_parameters as _values

_parse_string_list_field = legacy_errors_sync(_values._parse_string_list_field)
_parse_aliases_param = legacy_errors_sync(_values._parse_aliases_param)
_parse_categories_param = legacy_errors_sync(_values._parse_categories_param)
_parse_options_param = legacy_errors_sync(_values._parse_options_param)
_parse_expose_to_param = legacy_errors_sync(_values._parse_expose_to_param)


__all__ = [
    "_parse_aliases_param",
    "_parse_categories_param",
    "_parse_expose_to_param",
    "_parse_options_param",
    "_parse_string_list_field",
]
