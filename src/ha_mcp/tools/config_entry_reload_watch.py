"""Compatibility exports for the shared config-entry reload observation rules."""

from ha_mcp.runtime import entry_reload as _shared

_TRANSIENT_RECONFIGURE_STATES = _shared._TRANSIENT_RECONFIGURE_STATES
_is_transient_reconfigure_state = _shared._is_transient_reconfigure_state
_entry_fragments = _shared._entry_fragments
_observe_reload_settled = _shared._observe_reload_settled


__all__ = [
    "_TRANSIENT_RECONFIGURE_STATES",
    "_entry_fragments",
    "_is_transient_reconfigure_state",
    "_observe_reload_settled",
]
