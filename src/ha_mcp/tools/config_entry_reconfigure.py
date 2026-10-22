"""Legacy compatibility for shared integration reconfigure orchestration."""

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime import reconfigure as _shared
from ha_mcp.runtime import reconfigure_preflight as _preflight

logger = _shared.logger
_VERIFICATION_ATTEMPTS = _shared._VERIFICATION_ATTEMPTS
_VERIFICATION_BACKOFF_SECONDS = _shared._VERIFICATION_BACKOFF_SECONDS
PreparedReconfigure = _preflight.PreparedReconfigure
_raise_post_commit_verification_error = legacy_helper_flow(
    _shared._raise_post_commit_verification_error
)
_build_reconfigure_verification = legacy_helper_flow(
    _shared._build_reconfigure_verification
)
_verify_reconfigured_entry = legacy_helper_flow(_shared._verify_reconfigured_entry)
_build_reconfigure_flow_config = legacy_helper_flow(
    _preflight._build_reconfigure_flow_config
)
prepare_reconfigure_request = legacy_helper_flow(_preflight.prepare_reconfigure_request)
_validate_reconfigure_identity_and_duplicates = legacy_helper_flow(
    _preflight._validate_reconfigure_identity_and_duplicates
)
_run_reconfigure_flow = legacy_helper_flow(_shared._run_reconfigure_flow)
_verify_reconfigure_result = legacy_helper_flow(_shared._verify_reconfigure_result)
_reconfigure_response = legacy_helper_flow(_shared._reconfigure_response)
_observed_reload_state = legacy_helper_flow(_shared._observed_reload_state)
reconfigure_config_entry = legacy_helper_flow(_shared.reconfigure_config_entry)
