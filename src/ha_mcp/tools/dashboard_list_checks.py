"""Legacy error projection for the shared dashboard list-shape checks."""

from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime import dashboard_validation as _shared

_STACK_CARD_TYPES = _shared._STACK_CARD_TYPES
_CARD_WRAPPER_TYPES = _shared._CARD_WRAPPER_TYPES
_items = _shared._items
_collect_card_lists = _shared._collect_card_lists
_collect_section_lists = _shared._collect_section_lists
_collect_view_lists = _shared._collect_view_lists
_LIST_ITEM_COLLECTORS = _shared._LIST_ITEM_COLLECTORS
reject_malformed_dashboard_lists = legacy_errors_sync(
    _shared.reject_malformed_dashboard_lists
)
reject_malformed_dashboard_config = legacy_errors_sync(
    _shared.reject_malformed_dashboard_config
)
_PATCH_LIST_PATH = _shared._PATCH_LIST_PATH
_PATCH_INSIDE_CARD_PATH = _shared._PATCH_INSIDE_CARD_PATH
_PATCH_ITEM_PATH = _shared._PATCH_ITEM_PATH
patch_writes_inside_card = _shared.patch_writes_inside_card
reject_malformed_dashboard_patch = legacy_errors_sync(
    _shared.reject_malformed_dashboard_patch
)
