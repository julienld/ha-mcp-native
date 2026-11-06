"""Core's distinction between an unknown dashboard and an unsaved config."""

UNKNOWN_CONFIG_PREFIX = "Unknown config specified"
NO_STORED_CONFIG_MESSAGES = frozenset(
    {"No config found.", "Command failed: No config found."}
)
CONFIG_NOT_FOUND_CODE = "config_not_found"
NO_STORED_CONFIG_NOTE = (
    "Home Assistant has no config for this dashboard (a storage dashboard with "
    "nothing saved yet, a YAML dashboard whose file is missing, or recovery "
    "mode), so the frontend shows its generated default view."
)
