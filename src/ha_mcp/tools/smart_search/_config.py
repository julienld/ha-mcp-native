"""Shared constants and pure helpers for the smart-search modules.

This module imports nothing from the ``ha_mcp.tools`` package so the
smart-search mixins and the public ``smart_search`` shell can both depend
on it without creating an import cycle.
"""

from ha_mcp.config import get_global_settings

# Default concurrency limit for parallel operations
DEFAULT_CONCURRENCY_LIMIT = 20

# Timeout for the entity-registry WebSocket list used by the scene walk.
ENTITY_REGISTRY_TIMEOUT = 3.0


# Per-id config-fetch tuning knobs. The code calls this pass "Attempt C":
# a proper name left from when two earlier bulk tiers were tried first,
# both since removed as phantoms (#1889, #2258). It is now the only pass. Sourced from the resolved
# Settings (issues #1538 / #1784) so the env var, the web Settings UI
# override file, and the field defaults all flow through one precedence path
# — and so add-on users (who cannot set raw env vars) can tune them from the
# Advanced panel. Read once at import as module-level constants; a change
# takes effect on the next MCP-host restart (advanced settings already
# carry a restart-required notice in the UI).
_settings = get_global_settings()

# Wall-clock budgets for the per-id config fetch (in seconds).
AUTOMATION_CONFIG_TIME_BUDGET = _settings.automation_config_time_budget
SCRIPT_CONFIG_TIME_BUDGET = _settings.script_config_time_budget
SCENE_CONFIG_TIME_BUDGET = _settings.scene_config_time_budget

# Per-request timeout (seconds) and batch size for the parallel individual
# config fetches. On HA servers that serve the per-id config endpoint
# serially, a batch's tail requests queue behind its head and can exceed
# the per-request timeout despite being perfectly healthy — tune batch
# size toward 1 and/or raise the timeout on such instances (issue #1784).
INDIVIDUAL_CONFIG_TIMEOUT = _settings.individual_config_timeout
INDIVIDUAL_FETCH_BATCH_SIZE = _settings.individual_fetch_batch_size
