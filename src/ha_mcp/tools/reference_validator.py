"""Reference validator for automation and script configs.

Walks a config dict and extracts every literal service and entity
reference, then cross-checks them against the live service and entity
registries. Produces soft warnings that flow into the existing response
alongside ``best_practice_warnings``.

Intentional limits (documented, not bugs):

- **Templates** (strings containing ``{{``) are counted and skipped.
  Jinja is not rendered here; template-safe validation would need
  ``POST /api/template`` round-trips and is a later follow-up.
- **Blueprint automations** (``use_blueprint`` at the root) are skipped
  wholesale. Post-substitution config is not exposed by any HA API, so
  the effective refs cannot be ground-truthed.
- **``device_id`` / ``area_id`` / ``label_id``** are NOT checked yet.
  They require separate registry fetches and are planned for a later
  pass; see #940.

Background: #940 (hallucinated ``notify.mobile_app_andrew_phone`` that
``ha_config_set_automation`` accepted silently).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..runtime.config_reference_rules import _ENTITY_KEYS as _ENTITY_KEYS
from ..runtime.config_reference_rules import _SERVICE_KEYS as _SERVICE_KEYS
from ..runtime.config_reference_rules import ExtractedRef as ExtractedRef
from ..runtime.config_reference_rules import ValidationWarning as ValidationWarning
from ..runtime.config_reference_rules import WalkerResult as WalkerResult
from ..runtime.config_reference_rules import (
    _extract_entity_refs as _extract_entity_refs,
)
from ..runtime.config_reference_rules import (
    _extract_service_ref as _extract_service_ref,
)
from ..runtime.config_reference_rules import _is_template as _is_template
from ..runtime.config_reference_rules import build_entity_set as build_entity_set
from ..runtime.config_reference_rules import build_service_index as build_service_index
from ..runtime.config_reference_rules import check_refs as check_refs
from ..runtime.config_reference_rules import extract_refs as extract_refs
from .component_config_reads import fetch_reference_data_via_component

logger = logging.getLogger(__name__)

# Keys whose value (literal string) names a Home Assistant service in
# the form ``<domain>.<service_name>``. Both legacy (``service:``) and
# the new-style (``action:``) keys are recognized — HA accepts either
# inside automation/script action blocks.

# Keys whose value (string or list of strings) names an entity. These
# appear in triggers, conditions, ``target:`` blocks, and service
# ``data:`` blocks — the walker is depth-agnostic so the location
# doesn't matter.


async def validate_config_references(
    client: Any, config: dict[str, Any]
) -> dict[str, Any]:
    """Walk *config*, fetch registries, return validation metadata.

    Errors from the two registry fetches are logged and swallowed so
    validation can never break the happy path of
    ``ha_config_set_automation`` / ``ha_config_set_script``.

    Returns a dict with three keys:

    - ``warnings`` - list of :class:`ValidationWarning`, empty on success
    - ``unvalidated_templates`` - int, templated strings skipped by the
      walker
    - ``blueprint_skipped`` - bool, True iff the root config uses
      ``use_blueprint``
    """
    walker_result = extract_refs(config)

    if walker_result["blueprint_skipped"] or not walker_result["refs"]:
        return {
            "warnings": [],
            "unvalidated_templates": walker_result["unvalidated_templates"],
            "blueprint_skipped": walker_result["blueprint_skipped"],
        }

    try:
        # ONLY the data FETCH lives in the swallow-all try. When the component
        # advertises ``reference_data``, one in-process frame returns the
        # REST-shaped service catalog + the entity-id universe together,
        # replacing the two REST round-trips; ``None`` ⇒ component
        # unavailable/errored → the legacy gather. Both sources normalise to the
        # same ``services_payload`` / ``states_payload`` shapes the reducers
        # consume, so the warnings are identical (pinned by the cross-seam
        # contract test).
        reference_data = await fetch_reference_data_via_component(client)
        if reference_data is not None:
            services_payload = reference_data["services"]
            states_payload = [
                {"entity_id": eid} for eid in reference_data["entity_ids"]
            ]
        else:
            services_payload, states_payload = await asyncio.gather(
                client.get_services(),
                client.get_states(),
            )
    except Exception:
        logger.exception(
            "Reference validator: failed to fetch service/entity registries; "
            "skipping validation for this call"
        )
        return {
            "warnings": [],
            "unvalidated_templates": walker_result["unvalidated_templates"],
            "blueprint_skipped": False,
        }

    # Index-building stays OUTSIDE the fetch try so a builder exception
    # propagates as it did before the component routing was added — the try
    # swallows registry-fetch failures only.
    service_index = build_service_index(services_payload)
    entity_set = build_entity_set(states_payload)
    warnings = check_refs(walker_result["refs"], service_index, entity_set)

    return {
        "warnings": warnings,
        "unvalidated_templates": walker_result["unvalidated_templates"],
        "blueprint_skipped": False,
    }
