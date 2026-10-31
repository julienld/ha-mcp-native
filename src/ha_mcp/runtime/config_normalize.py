"""Existing automation round-trip normalization, independent of transport."""

from typing import Any, cast

_USER_NAMESPACE_KEYS = frozenset({"variables", "trigger_variables"})


def _normalize_automation_config(
    config: Any, is_root: bool = True, *, in_user_namespace: bool = False
) -> Any:
    """
    Recursively normalize automation config field names to HA's canonical form.

    Home Assistant's 2024.10+ canonical form uses the plural root list keys
    ('triggers', 'actions', 'conditions'); the singular forms ('trigger',
    'action', 'condition') remain fully accepted as silent aliases. This tool
    canonicalizes to the plural root forms so the config-API round-trip and the
    downstream validators / best-practice checker all see one stable, modern
    shape (HA accepts whichever we send).

    Only the ROOT list keys are pluralized. The singular keys that act as type
    discriminators or service calls inside trigger/condition/action items
    ('trigger:' = trigger type, 'condition:' = condition type, 'action:' =
    service call) are semantically different and are left untouched, as is the
    singular 'sequence' key inside choose/if options and scripts. The nested
    'conditions' lists required inside choose/if and compound (or/and/not)
    blocks are already plural and pass through unchanged (issue #498: never
    rewrite these deeper keys).

    No mapping applies below a 'variables:' / 'trigger_variables:' key. That
    subtree is a free-form user namespace, so a key named 'sequences' there is
    a variable the user declared, not an alias to canonicalize. Rewriting it
    renamed the variable in what was written to HA (breaking every
    '{{ sequences }}' reference in the automation) and, when a sibling named
    'sequence' also existed, dropped the user's value outright.

    Args:
        config: Automation configuration (dict, list, or primitive)
        is_root: Whether this is the root-level automation config dict. Only the
                 root level gets the singular -> plural list-key normalization.
        in_user_namespace: Whether this value sits inside a variables block.
                 Set on recursion; no caller passes it.

    Returns:
        Normalized configuration with plural list field names at the root level.
    """
    # Handle lists - recursively process each item
    if isinstance(config, list):
        return [
            _normalize_automation_config(
                item, is_root=False, in_user_namespace=in_user_namespace
            )
            for item in config
        ]

    # Handle primitives (strings, numbers, etc.)
    if not isinstance(config, dict):
        return config

    # Process dictionary
    normalized = config.copy()

    # Build field mappings (source alias -> canonical key). Inside a variables
    # block every key is a user-chosen name, so the mapping stays empty there.
    field_mappings: dict[str, str] = {}

    if not in_user_namespace:
        # Pluralize the root list keys to HA's 2024.10+ canonical form. ONLY at
        # the root level: deeper in the tree 'trigger'/'action'/'condition' are
        # type discriminators / service calls, not list keys, and must not be
        # touched (e.g., 'action' inside a delay object -- see issue #498).
        if is_root:
            field_mappings["trigger"] = "triggers"
            field_mappings["action"] = "actions"
            field_mappings["condition"] = "conditions"

        # 'sequences' -> 'sequence': the canonical key is singular at any level.
        field_mappings["sequences"] = "sequence"

    # Apply field mapping to current level, preferring the canonical key.
    for src, dst in field_mappings.items():
        if src in normalized and dst not in normalized:
            normalized[dst] = normalized.pop(src)
        elif src in normalized and dst in normalized:
            # Both present -- prefer the canonical key, drop the alias.
            del normalized[src]

    # Recursively process all values in the dictionary
    for key, value in normalized.items():
        normalized[key] = _normalize_automation_config(
            value,
            is_root=False,
            in_user_namespace=in_user_namespace or key in _USER_NAMESPACE_KEYS,
        )

    return normalized


def _normalize_trigger_keys(triggers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Normalize trigger objects for round-trip compatibility.

    Older Home Assistant configs (and some integrations) still emit triggers
    keyed by the legacy 'platform'. This tool canonicalizes each trigger to the
    modern 'trigger' key (HA 2024.10+) so its pipeline and round-trip output use
    one stable, current shape; HA accepts either form on the SET side.

    Args:
        triggers: List of trigger configuration dicts

    Returns:
        List of triggers with 'trigger' key instead of 'platform' key
    """
    normalized_triggers = []
    for trigger in triggers:
        # Defensive: a malformed (e.g. LLM-generated) item may not be a dict.
        if not isinstance(trigger, dict):
            normalized_triggers.append(trigger)
            continue
        normalized_trigger = trigger.copy()
        # Convert legacy 'platform' to modern 'trigger'. If both are present,
        # drop the legacy alias so HA's strict schema doesn't reject the config
        # with "extra keys not allowed" (mirrors _normalize_automation_config).
        if "platform" in normalized_trigger:
            if "trigger" not in normalized_trigger:
                normalized_trigger["trigger"] = normalized_trigger.pop("platform")
            else:
                del normalized_trigger["platform"]
        normalized_triggers.append(normalized_trigger)
    return normalized_triggers


def _normalize_config_for_roundtrip(config: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize automation config from GET response for direct use in SET.

    This ensures a config retrieved via ha_config_get_automation can be
    directly passed to ha_config_set_automation without modification.

    Transformations:
    1. Field names: canonicalized to plural root keys (triggers/actions/conditions);
       a stray `sequences` key is normalized to `sequence`.
    2. Trigger keys: platform -> trigger (inside each trigger object)

    Args:
        config: Raw automation configuration from HA API

    Returns:
        Normalized configuration compatible with SET API
    """
    # First normalize field names (singular -> plural at the root level)
    normalized = _normalize_automation_config(config)

    # Then normalize trigger keys (legacy 'platform' -> modern 'trigger')
    if "triggers" in normalized and isinstance(normalized["triggers"], list):
        normalized["triggers"] = _normalize_trigger_keys(normalized["triggers"])

    return cast(dict[str, Any], normalized)
