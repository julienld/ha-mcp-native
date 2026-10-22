"""Compatibility exports for the shared config-entry reload observation rules."""

from ha_mcp.backends.remote_entry_watch import (
    _subscribe_entry_changes as _subscribe_entry_changes,
)
from ha_mcp.runtime import entry_reload as _shared

logger = _shared.logger
_TRANSIENT_RECONFIGURE_STATES = _shared._TRANSIENT_RECONFIGURE_STATES
_RELOAD_SETTLE_TIMEOUT = _shared._RELOAD_SETTLE_TIMEOUT
_ENTRY_SUBSCRIBE_TIMEOUT = _shared._ENTRY_SUBSCRIBE_TIMEOUT
_COMMIT_VISIBLE_TIMEOUT = _shared._COMMIT_VISIBLE_TIMEOUT
WS_CONFIG_ENTRIES_SUBSCRIBE = _shared.WS_CONFIG_ENTRIES_SUBSCRIBE
_modified_at = _shared._modified_at
_is_transient_reconfigure_state = _shared._is_transient_reconfigure_state
_entry_fragments = _shared._entry_fragments
_EntryFragmentReader = _shared._EntryFragmentReader
_is_post_commit = _shared._is_post_commit
_observe_reload_settled = _shared._observe_reload_settled
