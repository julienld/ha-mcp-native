"""Native settings exposed by both the administrator UI and developer tools."""

FEATURE_FLAGS = (
    "read_only_mode",
    "enable_tool_security_policies",
    "enable_security_policy_tool",
    "enable_mandatory_bps",
    "enable_strict_mandatory_bps",
    "enable_tool_search",
    "tool_search_max_results",
    "redact_secrets",
    "enable_beta_features",
    "enable_code_mode",
    "enable_dashboard_screenshot",
    "enable_filesystem_tools",
    "enable_yaml_config_editing",
    "enable_yaml_edit_confirm",
    "enable_yaml_packages_automation",
    "enable_yaml_packages_script",
    "enable_yaml_packages_scene",
)
ADVANCED_SETTINGS = {
    "enable_dev_mode": "HAMCP_ENABLE_DEV_MODE",
    "dev_tools_security_policy_access": "HAMCP_DEV_SECURITY_POLICY_ACCESS",
    "enabled_tool_modules": "ENABLED_TOOL_MODULES",
    "custom_instructions": "HAMCP_CUSTOM_INSTRUCTIONS",
    "code_mode_max_duration": "CODE_MODE_MAX_DURATION",
    "code_mode_max_memory": "CODE_MODE_MAX_MEMORY",
    "code_mode_max_recursion": "CODE_MODE_MAX_RECURSION",
    "code_mode_max_invocations": "CODE_MODE_MAX_INVOCATIONS",
    "code_mode_saved_tools_path": "CODE_MODE_SAVED_TOOLS_PATH",
    "extra_yaml_write_keys": "HA_MCP_EXTRA_YAML_KEYS",
    "dashboard_screenshot_engine_url": "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL",
}
