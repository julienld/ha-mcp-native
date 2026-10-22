"""Pure config reference extraction and registry comparison rules."""

from typing import Any, TypedDict


_SERVICE_KEYS: frozenset[str] = frozenset({"service", "action"})


_ENTITY_KEYS: frozenset[str] = frozenset({"entity_id"})


class ExtractedRef(TypedDict):
    """One reference pulled out of the config tree."""

    path: str
    value: str
    kind: str  # "service" | "entity"


class WalkerResult(TypedDict):
    """Return value of :func:`extract_refs`."""

    refs: list[ExtractedRef]
    unvalidated_templates: int
    blueprint_skipped: bool


class ValidationWarning(TypedDict):
    """One warning in the tool response."""

    path: str
    value: str
    kind: str
    reason: str


def _extract_service_ref(
    sub_path: str,
    value: str,
    refs: list[ExtractedRef],
    unvalidated_templates: list[int],
) -> None:
    """Record a service ref at *sub_path*, counting templates instead."""
    if _is_template(value):
        unvalidated_templates[0] += 1
    else:
        refs.append({"path": sub_path, "value": value, "kind": "service"})


def _extract_entity_refs(
    sub_path: str,
    value: Any,
    refs: list[ExtractedRef],
    unvalidated_templates: list[int],
) -> bool:
    """Record entity ref(s) from a str or list *value*; return True if handled.

    Returns False when *value* is neither a str nor a list so the caller can
    keep recursing into it (an ``entity_id`` mapped to a nested structure).
    """
    if isinstance(value, str):
        if _is_template(value):
            unvalidated_templates[0] += 1
        else:
            refs.append({"path": sub_path, "value": value, "kind": "entity"})
        return True
    if isinstance(value, list):
        for i, item in enumerate(value):
            if not isinstance(item, str):
                continue
            item_path = f"{sub_path}[{i}]"
            if _is_template(item):
                unvalidated_templates[0] += 1
            else:
                refs.append(
                    {
                        "path": item_path,
                        "value": item,
                        "kind": "entity",
                    }
                )
        return True
    return False


def extract_refs(config: Any) -> WalkerResult:
    """Pull every literal service/entity reference out of *config*.

    Pure function: no network, no mutation of the input. The caller
    decides what to do with the extracted refs.

    Blueprint configs short-circuit with an empty ref list and
    ``blueprint_skipped=True`` — the effective post-substitution config
    is not reachable from ha-mcp, so it cannot be validated.
    """
    if isinstance(config, dict) and "use_blueprint" in config:
        return {
            "refs": [],
            "unvalidated_templates": 0,
            "blueprint_skipped": True,
        }

    refs: list[ExtractedRef] = []
    # Wrapped in a single-element list so the inner closure can mutate
    # it without a ``nonlocal`` dance.
    unvalidated_templates = [0]

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                sub_path = f"{path}.{key}" if path else key

                if key in _SERVICE_KEYS and isinstance(value, str):
                    _extract_service_ref(sub_path, value, refs, unvalidated_templates)
                    continue

                if key in _ENTITY_KEYS and _extract_entity_refs(
                    sub_path, value, refs, unvalidated_templates
                ):
                    continue

                # Neither a service nor an entity key (or an entity key whose
                # value is neither str nor list): recurse so deeply nested
                # action blocks (choose/if/parallel/repeat) still get walked.
                _walk(value, sub_path)

        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]")
        # Primitives: nothing to extract.

    _walk(config, "")
    return {
        "refs": refs,
        "unvalidated_templates": unvalidated_templates[0],
        "blueprint_skipped": False,
    }


def _is_template(value: str) -> bool:
    """Return True if *value* looks like a Jinja template."""
    return "{{" in value


def build_service_index(services_payload: Any) -> dict[str, set[str]]:
    """Turn ``/api/services`` output into a ``{domain: {services}}`` map.

    HA returns a list of ``{"domain": str, "services": {name: {...}}}``
    objects — one per domain. Any malformed entry is skipped silently.
    """
    index: dict[str, set[str]] = {}
    if not isinstance(services_payload, list):
        return index
    for entry in services_payload:
        if not isinstance(entry, dict):
            continue
        domain = entry.get("domain")
        services = entry.get("services")
        if not isinstance(domain, str) or not isinstance(services, dict):
            continue
        index[domain] = set(services.keys())
    return index


def build_entity_set(states_payload: Any) -> set[str]:
    """Turn ``/api/states`` output into a set of entity_ids."""
    entities: set[str] = set()
    if not isinstance(states_payload, list):
        return entities
    for entry in states_payload:
        if isinstance(entry, dict):
            entity_id = entry.get("entity_id")
            if isinstance(entity_id, str):
                entities.add(entity_id)
    return entities


def check_refs(
    refs: list[ExtractedRef],
    service_index: dict[str, set[str]],
    entity_set: set[str],
) -> list[ValidationWarning]:
    """Return one warning per ref that isn't in the registry."""
    warnings: list[ValidationWarning] = []
    for ref in refs:
        value = ref["value"]
        if ref["kind"] == "service":
            domain, _, service_name = value.partition(".")
            if (
                not service_name
                or domain not in service_index
                or service_name not in service_index[domain]
            ):
                warnings.append(
                    {
                        "path": ref["path"],
                        "value": value,
                        "kind": "service",
                        "reason": "not found in service registry",
                    }
                )
        elif ref["kind"] == "entity":
            if value not in entity_set:
                warnings.append(
                    {
                        "path": ref["path"],
                        "value": value,
                        "kind": "entity",
                        "reason": "not found in entity registry",
                    }
                )
    return warnings

