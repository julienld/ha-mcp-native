"""Automation validation, canonical hashes and blueprint takeover policy."""

import copy
from typing import Any, NoReturn, cast

from ha_mcp.errors import (
    ErrorCode,
    HomeAssistantAPIError,
    create_error_response,
    create_validation_error,
)
from ha_mcp.tools.best_practice_checker import BestPracticeCheckResult
from ha_mcp.tools.coercion import coerce_to_list, parse_json_param
from ha_mcp.utils.config_hash import compute_config_hash

from .blueprint_substitute import TakenControl, take_control_config
from .config_normalize import _normalize_config_for_roundtrip
from .config_reads import _missing_config
from .config_removals import AutomationRemovalTools
from .config_write_context import ConfigWriteContext
from .config_write_errors import reject_enabled_in_config, reject_sequence_misroute
from .errors import raise_failure
from .validation import reject_malformed_list_fields, validate_identifier_not_empty

_ROOT_LIST_KEYS = (
    "triggers",
    "trigger",
    "conditions",
    "condition",
    "actions",
    "action",
)


def _scene_create_in_choose(action: dict[str, Any]) -> bool:
    """True if any ``choose`` option's sequence contains scene.create."""
    opts = action.get("choose")
    if not isinstance(opts, list):
        return False
    for opt in opts:
        if isinstance(opt, dict) and isinstance(opt.get("sequence"), list):
            for sub in opt["sequence"]:
                if _action_contains_scene_create(sub):
                    return True
    return False


def _action_contains_scene_create(action: Any) -> bool:
    """True if the action — or any nested action under HA's wrapper keys —
    invokes ``scene.create``.

    Walks the standard wrappers: ``sequence``, ``parallel``, ``choose``
    (with options' inner ``sequence``), ``default``, and the ``then``/
    ``else`` siblings of ``if``. Returns True on the first hit, so a deep
    misroute is caught before the upsert reaches HA.
    """
    if not isinstance(action, dict):
        return False
    # 'service:' is the legacy key, 'action:' the modern HA service-call
    # key (HA 2024.8+). Both reach scene.create.
    if "scene.create" in (action.get("service"), action.get("action")):
        return True
    # Wrappers whose value is a list of nested actions.
    for nested_key in ("sequence", "parallel", "default", "then", "else"):
        nested = action.get(nested_key)
        if isinstance(nested, list):
            for sub in nested:
                if _action_contains_scene_create(sub):
                    return True
    return _scene_create_in_choose(action)


def _detect_conflicting_root_keys(config: Any) -> list[str]:
    """Warn when a config carries BOTH a singular alias and its canonical plural
    root key with *different* values.

    ``_normalize_automation_config`` keeps the canonical plural and silently drops
    the singular alias, so a caller that set, say, ``config['trigger']`` on a
    config that already has ``triggers`` would have that change discarded. The
    config is malformed (a caller should send one form), but surfacing the
    conflict beats dropping data silently.
    """
    if not isinstance(config, dict):
        return []
    warnings: list[str] = []
    for singular, plural in (
        ("trigger", "triggers"),
        ("action", "actions"),
        ("condition", "conditions"),
    ):
        if (
            singular in config
            and plural in config
            and config[singular] != config[plural]
        ):
            warnings.append(
                f"Config contains both '{singular}' and '{plural}' with different "
                f"values; using the canonical '{plural}' and ignoring '{singular}'."
            )
    return warnings


def _validate_automation_identifier(identifier: str | None) -> None:
    """Validate an optional automation identifier before dispatch."""
    if identifier is not None:
        validate_identifier_not_empty(
            identifier,
            "identifier",
            suggestions=[
                "Omit identifier to create a new automation",
                "Or pass a valid automation entity_id / unique_id to update",
            ],
            context={"action": "set"},
        )


def _strip_redundant_identifier_echo(
    result: dict[str, Any],
    *,
    extra_excludes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Strip the redundant ``identifier`` echo from an upsert/delete response.

    The canonical ``automation_id`` key (resolved entity_id, falling back to
    input identifier or ``unique_id``) makes re-echoing the raw ``identifier``
    redundant noise.

    ``unique_id`` is intentionally retained — it's HA's internal identifier,
    distinct from ``entity_id``/``automation_id``, and callers track it for
    cleanup. Do not extend ``extra_excludes`` to ``"unique_id"``: that
    regression broke E2E ``test_duplicate_automation_prevention`` at 5fe5338.

    ``extra_excludes`` lets a call site drop additional internal keys the
    spread shouldn't surface (e.g. ``"success"`` on the python_transform
    branch, where the caller manages that key directly).
    """
    excluded = {"identifier", *extra_excludes}
    return {k: v for k, v in result.items() if k not in excluded}


class AutomationConfigMethods(AutomationRemovalTools, ConfigWriteContext):
    async def _read_automation_config(self, identifier: str) -> dict[str, Any]:
        result = await self.backend.execute(
            "config.automation.read", {"identifier": identifier}, self.context
        )
        if not result["found"]:
            raise HomeAssistantAPIError(
                f"Automation not found: {identifier}", status_code=404
            )
        value: dict[str, Any] = result["config"]
        return value

    async def _raise_automation_not_found(self, identifier: str) -> NoReturn:
        result = await self.backend.execute(
            "config.automation.read", {"identifier": identifier}, self.context
        )
        _missing_config("automation", identifier, result.get("available_ids", []))

    @staticmethod
    def _build_set_automation_suggestions(
        e: Exception, bp_warnings: BestPracticeCheckResult
    ) -> list[str]:
        """Build the ordered suggestion list for a failed config-replacement set.

        Extracted verbatim from ``ha_config_set_automation``'s exception
        handler: HA-API messages get a targeted lead suggestion inserted, and
        any best-practice warnings are appended.
        """
        error_text = str(e)
        suggestions = [
            "Check automation configuration format",
            "Ensure required fields: alias, triggers, actions",
            "Use entity_id format: automation.morning_routine or unique_id",
            "Use ha_search(domain_filter='automation') to find automations",
            "Use ha_get_skill_guide for automation examples",
        ]
        if isinstance(e, HomeAssistantAPIError):
            if "'service'" in error_text and "not allowed" in error_text:
                suggestions.insert(
                    0,
                    "Use 'action:' not 'service:' for service calls in action steps "
                    "(renamed in HA 2024.8).",
                )
            elif "unexpected keyword argument" in error_text.lower():
                suggestions.insert(
                    0,
                    "An action step contains a field that belongs at the automation root "
                    "(e.g. alias, trigger, condition). Each action step should only contain "
                    "action/target/data/delay/choose/if/repeat/parallel keys.",
                )
            elif "'variables'" in error_text and "dictionary" in error_text:
                suggestions.insert(
                    0,
                    "variables must be a dict mapping names to values, "
                    'e.g. {"variables": {"my_var": 42}}',
                )
        if bp_warnings:
            suggestions.append(
                "Config had best-practice issues that may be related: "
                + "; ".join(bp_warnings)
            )
        return suggestions

    async def _take_control_config(self, identifier: str | None) -> TakenControl:
        """Render a blueprint automation into the config that replaces it.

        Returns ``(config, blueprint_path, config_hash)``. The hash is of the
        config this read saw, so the caller can lock the write against it.
        Deliberately produces a config
        for the ordinary replacement path rather than writing it here: the
        rendering is not exempt from the validation, best-practice checks or
        skill-content attachment every other automation write goes through,
        and routing it through one write tail is what keeps those guarantees
        from drifting apart.
        """
        if not identifier:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "identifier is required for take_control_of_blueprint",
                    suggestions=[
                        "Provide the automation entity_id or unique_id to convert",
                        "Use ha_search(domain_filter='automation') to find automations",
                    ],
                    context={"action": "take_control", "identifier": identifier},
                )
            )

        # The caller need not supply a hash (unlike python_transform, whose
        # expression is written against a config the caller already read), so
        # this read's own hash is returned for the write to lock against.
        current_config, fetched_hash = await self._get_automation_config_internal(
            identifier
        )

        taken, blueprint_path = await take_control_config(
            self._render_blueprint, "automation", identifier, current_config
        )
        return TakenControl(taken, blueprint_path, fetched_hash)

    async def _guard_alias_replacement(
        self, identifier: str, config: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None] | None:
        """Require a prior read before replacing a differently named automation.

        Return the raw before-image and storage id for reuse by this write.
        This catches sequential ID reuse. The REST read and write are separate
        requests, so it does not provide atomic protection against other writers.
        """
        try:
            current = await self._read_automation_config(identifier)
        except HomeAssistantAPIError as exc:
            if exc.status_code == 404 and not identifier.startswith("automation."):
                return None  # Preserve intentional creation with a caller-chosen ID.
            # Entity-ID resolution reports a state 404 as not found. Never retry
            # that as creation: a recovered lookup could overwrite an unchecked target.
            raise
        if current.get("alias") != config.get("alias"):
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Automation {identifier!r} already exists as {current.get('alias')!r}. "
                    f"This write would replace it with {config.get('alias')!r}. Nothing was written.",
                    context={"identifier": identifier, "parameter": "config_hash"},
                    suggestions=[
                        "Omit entity_id (and identifier) and remove config['id'] to create a separate automation.",
                        (
                            "For an intentional rename or replacement, call ha_config_get_automation "
                            "for this identifier, inspect its config, and resubmit with its config_hash."
                        ),
                    ],
                )
            )
        raw_id = current.get("id")
        return (
            copy.deepcopy(current),
            str(raw_id) if raw_id is not None else None,
        )

    async def _get_automation_config_internal(
        self, identifier: str
    ) -> tuple[dict[str, Any], str]:
        """Fetch and normalize automation config without logging or category injection.

        Returns (normalized_config, config_hash) tuple.
        Used by readback and the historical ha_config_get_automation facade.

        Raises a structured ``RESOURCE_NOT_FOUND`` ToolError via
        ``_raise_automation_not_found`` when the REST client returns 404.
        Other ``HomeAssistantAPIError`` instances propagate unchanged to
        caller exception handlers (``exception_to_structured_error`` route).
        """
        _, normalized_config, config_hash_value = await self._read_automation_snapshot(
            identifier
        )
        return normalized_config, config_hash_value

    async def _read_automation_snapshot(
        self, identifier: str
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        """Keep the raw CAS image alongside its normalized caller-facing config."""
        try:
            config_result = await self._read_automation_config(identifier)
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                await self._raise_automation_not_found(identifier)
            raise
        raw_config = copy.deepcopy(config_result)
        normalized_config = _normalize_config_for_roundtrip(config_result)
        config_hash_value = compute_config_hash(normalized_config)
        return raw_config, normalized_config, config_hash_value

    async def _fetch_and_verify_hash(
        self, identifier: str, config_hash: str, action: str
    ) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
        """Fetch current automation config and verify config_hash for optimistic locking.

        Returns ``(current_normalized_config, resolved_unique_id, raw_config)``.
        The raw config stays local to the call for backup and CAS; a transform
        may safely mutate the separate normalized config. The fetch
        already resolved ``identifier`` to the storage key inside
        ``get_automation_config``; HA returns the stored body keyed by that
        unique_id, so ``config['id']`` IS the resolved unique_id (the same value
        the #1404 guard compares against). Threading it lets the caller pass
        ``_resolved=True`` to ``upsert_automation_config`` and skip the redundant
        second resolve (issue #1813 Phase 0). ``None`` when the stored body has
        no ``id`` (not expected for a real automation) — the caller then falls
        back to re-resolving.
        Raises ToolError if the hash does not match (conflict).
        """
        raw_config, current_config, current_hash = await self._read_automation_snapshot(
            identifier
        )
        if (
            current_hash != config_hash
            and compute_config_hash(raw_config) == config_hash
        ):
            current_config = copy.deepcopy(raw_config)
            current_hash = config_hash
        if current_hash != config_hash:
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Automation modified since last read (conflict)",
                    suggestions=[
                        "Call ha_config_get_automation() again",
                        "Use the fresh config_hash from that response",
                    ],
                    context={"action": action, "identifier": identifier},
                )
            )
        raw_id = current_config.get("id")
        # Stringify to match ``_resolve_automation_id``'s ``str()`` coercion so a
        # threaded id is byte-for-byte what the unthreaded path would produce.
        resolved_id = str(raw_id) if raw_id is not None else None
        return current_config, resolved_id, raw_config

    @staticmethod
    def _parse_and_validate_config(config: str | dict[str, Any]) -> dict[str, Any]:
        """Parse JSON config and validate it is a dict."""
        try:
            parsed_config = parse_json_param(config, "config")
        except ValueError as e:
            raise_failure(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_JSON,
                    message=f"Invalid config parameter: {e}",
                    suggestions=[
                        "Pass 'config' as a dict, not a JSON string, to avoid escaping issues.",
                        "Check for JSON syntax errors: unquoted keys, trailing commas, or invalid escape sequences.",
                    ],
                    context={"parameter": "config"},
                )
            )

        if parsed_config is None or not isinstance(parsed_config, dict):
            raise_failure(
                create_validation_error(
                    "Config parameter must be a JSON object",
                    parameter="config",
                    details=f"Received type: {type(parsed_config).__name__}",
                )
            )
        reject_malformed_list_fields(
            parsed_config, _ROOT_LIST_KEYS, {"parameter": "config"}
        )
        return cast(dict[str, Any], parsed_config)

    @staticmethod
    def _check_scene_create_misroute(
        config_dict: dict[str, Any], identifier: str | None
    ) -> None:
        """Raise if an empty-trigger config wraps scene.create (common model misroute)."""
        trigger_value = config_dict.get("triggers", config_dict.get("trigger"))
        trigger_empty = trigger_value is None or (
            isinstance(trigger_value, list) and not trigger_value
        )
        if not trigger_empty:
            return
        actions_list = coerce_to_list(
            config_dict.get("actions", config_dict.get("action"))
        )
        scene_create_indices = [
            i for i, a in enumerate(actions_list) if _action_contains_scene_create(a)
        ]
        if scene_create_indices:
            raise_failure(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message=(
                        "Empty trigger paired with a scene.create action — "
                        "this automation can never fire. For a state snapshot "
                        "of one or more entities, use ha_config_set_scene "
                        "directly instead of wrapping scene.create in an "
                        "automation."
                    ),
                    suggestions=[
                        "ha_config_set_scene(scene_id='...', config={'name': '...', 'entities': {'<identifier>': {...}}}) creates a scene without a trigger.",
                        "If the snapshot really should be the result of an event, add the trigger that should fire it and keep the automation.",
                        "For a state-derived value that recomputes when its inputs change, use ha_config_set_helper(helper_type='template') instead.",
                    ],
                    context={
                        "scene_create_action_indices": scene_create_indices,
                        "identifier": identifier,
                    },
                )
            )

    @staticmethod
    def _validate_condition_platform(config_dict: dict[str, Any]) -> None:
        """Raise if any condition uses 'platform' (trigger syntax) instead of 'condition'."""
        for idx, cond in enumerate(coerce_to_list(config_dict.get("conditions"))):
            if not isinstance(cond, dict):
                continue
            if "platform" in cond and "condition" not in cond:
                raise_failure(
                    create_error_response(
                        code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                        message=(
                            f"Condition at index {idx} uses 'platform' (trigger syntax). "
                            "Conditions use 'condition', not 'platform'."
                        ),
                        suggestions=[
                            (
                                f"Replace 'platform' with 'condition': "
                                f"{{'condition': '{cond['platform']}', ...}}"
                            ),
                            "Triggers use 'trigger'; conditions use 'condition'.",
                        ],
                        context={"condition_index": idx, "found_key": "platform"},
                    )
                )

    @staticmethod
    def _validate_required_fields(
        config_dict: dict[str, Any], identifier: str | None, source: str = "config"
    ) -> None:
        """Reject the misroutes and duplicate creation HA-MCP guards against.

        Core validates the automation itself; these are HA-MCP's own checks.
        On a blueprint automation, empty root lists are deleted from
        ``config_dict`` in place.
        """
        if "use_blueprint" in config_dict:
            # HA-MCP convenience: Core merges the instance config over the
            # rendered blueprint, so an empty list here would replace the
            # blueprint's own triggers/conditions/actions.
            for key in _ROOT_LIST_KEYS:
                if config_dict.get(key) == []:
                    del config_dict[key]

        reject_sequence_misroute(config_dict, identifier, source)
        reject_enabled_in_config(config_dict)

        AutomationConfigMethods._check_scene_create_misroute(config_dict, identifier)

        # Prevent duplicate creation when config contains an existing automation id
        if identifier is None and "id" in config_dict:
            existing_id = config_dict["id"]
            raise_failure(
                create_validation_error(
                    f"Config contains 'id' field ('{existing_id}') but no identifier was provided. "
                    "This would create a duplicate automation instead of updating the existing one.",
                    parameter="identifier",
                    details=f"To update, pass entity_id='{existing_id}' (identifier='{existing_id}' is also accepted). "
                    "To create a genuinely new automation, remove the 'id' field from the config.",
                )
            )

    @staticmethod
    def _check_input_guards(
        config_dict: dict[str, Any], entity_id: str | None, source: str = "config"
    ) -> None:
        AutomationConfigMethods._validate_required_fields(
            config_dict, entity_id, source
        )
