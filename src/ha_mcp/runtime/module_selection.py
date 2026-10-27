"""Module selection shared by native and remote catalogs."""

MODULE_PRESETS = {
    "automation": [
        "tools_config_automations",
        "tools_config_scripts",
        "tools_traces",
        "tools_blueprints",
        "tools_search",  # Useful for finding entities in automations
    ],
}


MODULE_SPLIT_EXPANSIONS = {
    # ha_get_logs moved out of tools_utility in the #2279 split.
    "tools_utility": {"tools_logs"},
}


def parse_enabled_modules(value: str) -> set[str] | None:
    """Parse enabled_modules config into a set of module names.

    Returns None if all modules should be enabled.
    """
    if value.lower() == "all":
        return None

    # Check for preset names
    if value.lower() in MODULE_PRESETS:
        return set(MODULE_PRESETS[value.lower()])

    # Parse comma-separated list
    modules = {m.strip() for m in value.split(",") if m.strip()}
    for legacy, successors in MODULE_SPLIT_EXPANSIONS.items():
        if legacy in modules:
            modules |= successors
    return modules if modules else None
