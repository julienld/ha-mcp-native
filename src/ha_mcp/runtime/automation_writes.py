"""Shared automation authoring, transforms, blueprint takeover and runtime actions."""

import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp.errors import (
    ErrorCode,
    HomeAssistantAPIError,
    create_error_response,
)
from ha_mcp.tool_api import ToolFailure
from ha_mcp.tool_api.parameters import BestPracticeKeyParam
from ha_mcp.tools.best_practice_checker import BestPracticeCheckResult
from ha_mcp.tools.best_practice_checker import (
    check_automation_config as _check_best_practices,
)
from ha_mcp.tools.coercion import JSON_STRING_COERCION
from ha_mcp.utils.python_sandbox import (
    PythonSandboxError,
    format_sandbox_error,
    get_security_documentation,
)

from .automation_config import (
    _detect_conflicting_root_keys,
    _strip_redundant_identifier_echo,
    _validate_automation_identifier,
)
from .automation_identity import automation_write_body
from .automation_runtime import (
    _RUNTIME_ERRORS,
    AutomationRuntimeTools,
    _run_actions_once_reloaded,
    _sync_post_write_automation_result,
)
from .blueprint_substitute import validate_write_modes
from .config_normalize import _normalize_automation_config
from .config_reload import config_reload_waiter
from .config_reload_status import note_reload_outcome
from .config_write_context import merge_validation_meta
from .errors import raise_classified, raise_failure
from .validation import reject_malformed_list_fields

logger = logging.getLogger(__name__)
_AUTOMATION_SKILL_FILES = (
    "references/triggers-and-conditions.md",
    "references/automation-actions.md",
    "references/template-guidelines.md",
)
NOT_VERIFIED_WARNING_PREFIX = (
    "Automation was submitted to Home Assistant but the entity was not found"
)


class AutomationWriteTools(AutomationRuntimeTools):
    def _config_reload_waiter(self, *, enabled: bool) -> Any:
        return config_reload_waiter(
            self.backend, self.context, "automation_reloaded", enabled=enabled
        )

    async def _raise_automation_write_error(
        self,
        error: Exception,
        identifier: str | None,
        bp_warnings: BestPracticeCheckResult,
    ) -> NoReturn:
        if (
            identifier
            and isinstance(error, HomeAssistantAPIError)
            and error.status_code == 404
        ):
            await self._raise_automation_not_found(identifier)
        try:
            raise_classified(
                error,
                {"identifier": identifier},
                suggestions=self._build_set_automation_suggestions(error, bp_warnings),
            )
        except ToolFailure as failure:
            raise await self._augment_failure(failure, bp_warnings) from None

    async def _upsert_automation(
        self, config: dict[str, Any], identifier: str | None, resolved_id: str | None
    ) -> dict[str, Any]:
        allocated = identifier is None
        candidate = identifier
        if candidate is None:
            candidate = await self.backend.execute(
                "config.automation.allocate_id", {}, self.context
            )
        if not isinstance(candidate, str) or not candidate.strip():
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Automation storage ID must not be empty.",
                status="not_started",
            )
        key, previous = await self._config_before_image(
            "automation",
            candidate,
            resolved_id,
            getattr(self, "_last_verified_automation", None),
            creating=allocated,
        )
        body = automation_write_body(config, identifier, key)
        backup = await self._capture_config_backup("automation", key, previous)
        result: dict[str, Any] = await self.backend.execute(
            "config.automation.write",
            {
                "storage_key": key,
                "expected_config": previous,
                "data": body,
                "identifier": candidate,
                "allocated": allocated,
            },
            self.context,
        )
        result.update(
            {"unique_id": key, "operation": "created" if allocated else "updated"}
        )
        result.pop("automation_id", None)
        result.setdefault("entity_id", None)
        if (
            allocated
            and not result.get("entity_id")
            and not result.get("entity_not_verified")
        ):
            try:
                result["entity_id"] = await self._wait_automation_entity(key)
            except _RUNTIME_ERRORS as error:
                result.setdefault("warnings", []).append(
                    f"Automation registration verification failed: {error}"
                )
            if not result["entity_id"]:
                result["entity_not_verified"] = True
        if backup.get("warnings"):
            result.setdefault("warnings", []).extend(backup["warnings"])
        return result

    async def ha_config_set_automation(
        self,
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description="Complete automation configuration. "
                "Purpose-specific triggers/conditions (HA 2026.7+ default: 'trigger': '<domain>.<name>' "
                "with 'target'/'options') are valid config. "
                "Mutually exclusive with python_transform.",
                default=None,
            ),
        ] = None,
        identifier: Annotated[
            str | None,
            Field(
                description="Target automation entity_id or HA config 'id' (unique_id). "
                "Omit for creation with a generated ID. Values such as 'new' are literal IDs, not placeholders.",
                default=None,
            ),
        ] = None,
        python_transform: Annotated[
            str | None,
            Field(
                description="Python expression to transform existing automation config. "
                "Mutually exclusive with config. "
                "WARNING: Expressions with infinite loops will hang the server. "
                "Examples: "
                "Simple: python_transform=\"config['actions'][0]['data']['brightness'] = 255\" "
                "Pattern: python_transform=\"for a in config['actions']: "
                "if a.get('alias') == 'My Step': a['data']['value'] = 100\" "
                "\n\n" + get_security_documentation(),
            ),
        ] = None,
        config_hash: Annotated[
            str | None,
            Field(
                description="Config hash from ha_config_get_automation for optimistic locking. "
                "Required for python_transform and when a config update changes an "
                "existing automation's alias; otherwise optional for config updates "
                "(validates before full replacement if provided).",
            ),
        ] = None,
        take_control_of_blueprint: Annotated[
            bool,
            Field(
                description="Convert a blueprint-backed automation into an editable "
                'standalone one -- the UI\'s "Take control". Renders the blueprint '
                "with its current inputs and saves the result over the same "
                "automation, which keeps its entity_id, alias and description and "
                "then has its own triggers/conditions/actions and no "
                "'use_blueprint'. Requires identifier; mutually exclusive with "
                "config and python_transform.",
                default=False,
            ),
        ] = False,
        category: Annotated[
            str | None,
            Field(
                description="Category ID to assign to this automation. Use ha_config_get_category(scope='automation') to list available categories, or ha_config_set_category() to create one.",
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool,
            Field(
                description="Wait for automation to be queryable before returning. Set to False for"
                " bulk operations.",
                default=True,
            ),
        ] = True,
        enabled: Annotated[
            bool | None,
            Field(
                description=(
                    "Turn the automation on (True) or off (False) after an optional config "
                    "update; None leaves it unchanged. Not written into the stored config: Home Assistant keeps "
                    "the state across restarts, but a config 'initial_state' overrides it "
                    "whenever the automation is reloaded or HA starts."
                ),
                default=None,
            ),
        ] = None,
        run_actions: Annotated[
            bool,
            Field(
                description=(
                    "Run the automation's actions now, skipping its triggers and "
                    "conditions (automation.trigger, the UI's Run actions). Applied "
                    "after `enabled` and after any config write. Can be used "
                    "standalone with identifier and no config."
                ),
                default=False,
            ),
        ] = False,
        MandatoryBPS: Annotated[
            bool,
            Field(default=True),
        ] = True,
        # BestPracticeKey (#1779): consumed by StrictBpsMiddleware, never read
        # here — see strict_bps.py for the declaration contract.
        BestPracticeKey: BestPracticeKeyParam = None,
    ) -> dict[str, Any]:
        """Create or update a Home Assistant automation.

        MUST call ha_get_skill_guide OR refer to your locally installed skills first.

        Prefer native triggers/conditions/actions over templates in logic
        positions; templates belong only in `data.*`, notification text,
        `event_data` and `variables`. The best-practice checker reports
        violations under `best_practice_warnings` — fix them before
        re-submitting. `triggers-and-conditions.md`, `automation-actions.md` and
        `template-guidelines.md` ship under `skill_content` by default. Test any unavoidable template
        with ha_eval_template first.

        Consider a dedicated tool first: a state snapshot with no trigger ->
        ha_config_set_scene; a value derived from other entities ->
        ha_config_set_helper(helper_type='template'); a counter / timer /
        schedule / boolean -> ha_config_set_helper.

        MODES (pick one; each can also take `enabled`, which alone with
        `identifier` turns the automation on or off without touching config):
        - python_transform: surgical edits to an existing automation. Requires
          identifier and config_hash from ha_config_get_automation(). Operates on
          the fetched config, which uses HA's plural root keys
          'triggers'/'actions'/'conditions', e.g.
          python_transform="config['triggers'].append({'trigger': 'state', 'entity_id': 'binary_sensor.motion', 'to': 'on'})"
        - config: new automations or full restructures. Regular automations need
          alias, triggers and actions; blueprint automations need alias and
          use_blueprint {path, input}.
        - take_control_of_blueprint: convert a blueprint-backed automation into a
          standalone one. Takes no config of its own.

        IDENTITY: omit identifier and config['id'] to create with a generated ID;
        an unused raw ID creates that specific ID. Reusing an identifier targets
        the same automation even if the alias changes; to rename or replace it,
        read it first and pass its config_hash — a changed alias without that
        hash is rejected before writing. The returned `automation_id` is the
        resolved entity_id, falling back to the input identifier or the
        generated unique_id.

        EXAMPLES:
        - Create: ha_config_set_automation(config={"alias": "Morning Lights", "triggers": [{"trigger": "time", "at": "07:00:00"}], "actions": [{"action": "light.turn_on", "target": {"area_id": "bedroom"}}]})
        - From a blueprint: ha_config_set_automation(config={"alias": "Motion Light Kitchen", "use_blueprint": {"path": "homeassistant/motion_light.yaml", "input": {"motion_entity": "binary_sensor.kitchen_motion", "light_target": {"entity_id": "light.kitchen"}}}})
        - Update: current = ha_config_get_automation(identifier="automation.x"); ha_config_set_automation(identifier="automation.x", config_hash=current["config_hash"], config={...})
        - Disable: ha_config_set_automation(identifier="automation.x", enabled=False)
        - Run now: ha_config_set_automation(identifier="automation.x", run_actions=True)
        - Take control: ha_config_set_automation(identifier="automation.x", take_control_of_blueprint=True)

        TAKE CONTROL is one-way: later blueprint edits stop reaching the
        automation; to change an input value, update 'use_blueprint.input'
        instead. It does NOT free the blueprint — Home Assistant keeps counting
        the converted automation as a user, so deleting that blueprint stays
        refused until the automation is removed (an automation reload does not
        clear it). The response names the blueprint in
        `took_control_of_blueprint`. To preview the rendering without writing
        anything, use ha_manage_blueprints(action="substitute", path=..., input=...).
        """
        bp_warnings: BestPracticeCheckResult = BestPracticeCheckResult()
        try:
            # ``identifier`` is optional (omit → create new with generated
            # unique_id; pass → update existing). When provided, reject
            # empty/whitespace up-front so the caller gets a structured
            # parameter error instead of a misleading ``RESOURCE_NOT_FOUND``
            # from the downstream lookup. The ``not identifier`` check
            # further down the python_transform branch still handles the
            # explicit ``identifier is None`` case for that mode.
            _validate_automation_identifier(identifier)
            validate_write_modes(
                "automation",
                "identifier",
                identifier,
                config,
                python_transform,
                take_control_of_blueprint,
            )

            detached_blueprint: str | None = None
            if take_control_of_blueprint:
                taken = await self._take_control_config(identifier)
                config = taken.config
                detached_blueprint = taken.blueprint_path
                # Take control is a read-modify-write the TOOL performs, so it
                # owns the consistency guarantee the caller could not supply:
                # without this, an edit landing between that read and this
                # write is silently overwritten. An explicit caller hash still
                # wins, so it can still lock against a config it read itself.
                config_hash = config_hash or taken.config_hash

            runtime_only_response = await self._maybe_set_runtime_only(
                identifier,
                config,
                python_transform,
                category,
                enabled,
                run_actions,
                wait,
            )
            if runtime_only_response is not None:
                await self.attach_skill_content(
                    runtime_only_response,
                    MandatoryBPS=MandatoryBPS,
                    canonical_files=_AUTOMATION_SKILL_FILES,
                    referenced_files=bp_warnings.referenced_files,
                )
                return runtime_only_response

            if python_transform is not None:
                response, bp_warnings = await self._run_python_transform(
                    identifier,
                    config_hash,
                    python_transform,
                    category,
                    MandatoryBPS,
                    enabled,
                    wait,
                    run_actions=run_actions,
                )
                return response

            if config is None:
                return raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Either config or python_transform must be provided",
                        suggestions=[
                            "config: Full automation configuration for create/replace",
                            "python_transform: Python expression for surgical edits",
                        ],
                        context={"action": "set", "identifier": identifier},
                    )
                )

            config_dict = self._parse_and_validate_config(config)

            # Extract category before sending to HA REST API (which rejects unknown keys).
            # Parameter takes precedence over config dict value.
            config_category = config_dict.pop("category", None)
            effective_category = category if category is not None else config_category

            # Detect conflicting singular+plural root keys BEFORE normalization
            # drops the singular alias (surface rather than silently discard).
            conflict_warnings = _detect_conflicting_root_keys(config_dict)

            # Normalize field names to HA's canonical plural root keys
            # (trigger -> triggers, action -> actions, condition -> conditions).
            config_dict = _normalize_automation_config(config_dict)

            # Purely local input checks run before the tool's own reads, so
            # a malformed config rejects without spending the hash check's
            # or the alias guard's GETs. (The auto-backup wrapper may take
            # its pre-write snapshot first on updates; that read is outside
            # this tool's control.)
            self._validate_required_fields(config_dict, identifier)

            # Both the hash check and alias guard read the resolved storage key.
            # Reuse it for the write to avoid a second entity-ID lookup (#1813).
            # Creation and responses without an id retain the existing fallback.
            resolved_id: str | None = None
            if identifier and config_hash:
                _, resolved_id = await self._fetch_and_verify_hash(
                    identifier, config_hash, "set"
                )
            elif identifier:
                resolved_id = await self._guard_alias_replacement(
                    identifier, config_dict
                )

            bp_warnings = _check_best_practices(config_dict)
            validation_meta = await self._validate_references(config_dict)

            # Issue #2159: the category is applied post-upsert via
            # ``apply_entity_category``, which HA accepts unchecked. Reject an
            # unknown one here so no automation is created under it.
            await self._validate_categories({"automation": effective_category})

            return await self._run_config_update(
                config_dict,
                identifier,
                effective_category,
                wait,
                bp_warnings,
                validation_meta,
                MandatoryBPS,
                conflict_warnings,
                resolved_id,
                detached_blueprint,
                enabled,
                run_actions=run_actions,
            )

        except ToolFailure as failure:
            raise await self._augment_failure(failure, bp_warnings) from None
        except Exception as error:  # noqa: BLE001 - facade-specific classification
            return await self._raise_automation_write_error(
                error, identifier, bp_warnings
            )

    async def _run_python_transform(
        self,
        identifier: str | None,
        config_hash: str | None,
        python_transform: str,
        category: str | None,
        MandatoryBPS: bool,
        enabled: bool | None,
        wait: bool,
        *,
        run_actions: bool = False,
    ) -> tuple[dict[str, Any], BestPracticeCheckResult]:
        """Execute python_transform mode and return (response, bp_warnings)."""
        if not identifier:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "identifier is required for python_transform",
                    suggestions=[
                        "Provide the automation entity_id or unique_id",
                        "Use ha_search(domain_filter='automation') to find automations",
                    ],
                    context={"action": "python_transform", "identifier": identifier},
                )
            )
        if config_hash is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "config_hash is required for python_transform",
                    suggestions=[
                        "Call ha_config_get_automation() first",
                        "Use the config_hash from that response",
                    ],
                    context={"action": "python_transform", "identifier": identifier},
                )
            )

        current_config, resolved_id = await self._fetch_and_verify_hash(
            identifier, config_hash, "python_transform"
        )

        try:
            transformed_config = await self._transform_config(
                python_transform, current_config
            )
        except PythonSandboxError as e:
            message, suggestions = format_sandbox_error(e, python_transform)
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    message,
                    suggestions=suggestions,
                    context={"action": "python_transform", "identifier": identifier},
                )
            )

        reject_malformed_list_fields(
            transformed_config,
            ("triggers", "trigger", "conditions", "condition", "actions", "action"),
            {"action": "python_transform", "identifier": identifier},
            source="python_transform",
        )
        # Pop category before sending to HA REST API (rejects unknown keys)
        transform_category = transformed_config.pop("category", None)
        effective_category = category if category is not None else transform_category

        # Detect conflicting singular+plural root keys (e.g. a transform that set
        # the singular 'trigger' on a fetched plural config) before normalization
        # drops the singular alias.
        conflict_warnings = _detect_conflicting_root_keys(transformed_config)

        transformed_config = _normalize_automation_config(transformed_config)
        self._validate_required_fields(
            transformed_config, identifier, source="python_transform"
        )
        bp_warnings = _check_best_practices(transformed_config)

        # Issue #2159: reject an unknown category before the write, so a
        # transform never lands under a category that does not exist.
        await self._validate_categories({"automation": effective_category})

        # ``_fetch_and_verify_hash`` already resolved ``identifier`` to the
        # storage key; thread it so the upsert skips the redundant re-resolve
        # (issue #1813 Phase 0). Fall back to the raw identifier if the fetched
        # body carried no ``id`` (not expected for a real automation).
        runtime_requested = enabled is not None or run_actions
        async with self._config_reload_waiter(
            enabled=runtime_requested
        ) as wait_for_reload:
            result = await self._upsert_automation(
                transformed_config, identifier, resolved_id
            )
            reloaded = await wait_for_reload()
            note_reload_outcome(
                result, reloaded, domain="automation", requested=enabled is not None
            )
        run_actions = _run_actions_once_reloaded(
            result, reloaded, run_actions, identifier or result.get("unique_id")
        )
        for warning in conflict_warnings:
            result.setdefault("warnings", []).append(warning)
        new_config_hash: str | None = None  # the write is saved; read-back may fail
        try:
            _, new_config_hash = await self._get_automation_config_internal(identifier)
        except _RUNTIME_ERRORS as e:
            result.setdefault("warnings", []).append(
                f"Saved; reading it back failed ({e}), re-read it for a config_hash"
            )

        entity_id = result.get("entity_id")
        if not entity_id and identifier and identifier.startswith("automation."):
            entity_id = identifier
        if effective_category and entity_id:
            await self._apply_category(
                entity_id,
                effective_category,
                "automation",
                result,
                "automation",
            )

        entity_id = await self._apply_runtime_state(
            result,
            entity_id,
            enabled,
            wait,
            identifier=identifier or result.get("unique_id"),
            run_actions=run_actions,
        )
        _sync_post_write_automation_result(result, entity_id)

        response: dict[str, Any] = {
            "success": True,
            "action": "python_transform",
            "automation_id": entity_id or identifier or result.get("unique_id"),
            "config_hash": new_config_hash,
            "python_expression": python_transform,
            "message": f"Automation {identifier} updated via Python transform",
            **_strip_redundant_identifier_echo(result, extra_excludes=("success",)),
        }
        if bp_warnings:
            response["best_practice_warnings"] = list(bp_warnings)
        await self.attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=_AUTOMATION_SKILL_FILES,
            referenced_files=bp_warnings.referenced_files,
        )
        return response, bp_warnings

    async def _run_config_update(
        self,
        config_dict: dict[str, Any],
        identifier: str | None,
        effective_category: str | None,
        wait: bool,
        bp_warnings: BestPracticeCheckResult,
        validation_meta: dict[str, Any],
        MandatoryBPS: bool,
        conflict_warnings: list[str] | None = None,
        resolved_id: str | None = None,
        detached_blueprint: str | None = None,
        enabled: bool | None = None,
        *,
        run_actions: bool = False,
    ) -> dict[str, Any]:
        """Execute config-replacement mode and return the tool response.

        ``detached_blueprint`` is set by take-control mode and names the
        blueprint the automation's config no longer references. It is
        reported on the response as ``took_control_of_blueprint`` and nothing
        more: Home Assistant goes on counting the automation as a user of that
        blueprint until it is removed, so there is no release to wait for.

        ``resolved_id`` (set when the hash check or alias guard fetched an id)
        is threaded to the upsert so it skips the redundant
        re-resolve; None falls back to resolving inside the REST client.
        """
        runtime_requested = enabled is not None or run_actions
        async with self._config_reload_waiter(
            enabled=runtime_requested
        ) as wait_for_reload:
            result = await self._upsert_automation(config_dict, identifier, resolved_id)
            reloaded = await wait_for_reload()
            note_reload_outcome(
                result, reloaded, domain="automation", requested=enabled is not None
            )
        run_actions = _run_actions_once_reloaded(
            result, reloaded, run_actions, identifier or result.get("unique_id")
        )

        for warning in conflict_warnings or []:
            result.setdefault("warnings", []).append(warning)

        post_write_identifier = identifier or result.get("unique_id")
        # A create already polled for the entity inside the upsert; with wait=True
        # the resolver below polls again. Either way, _apply_runtime_state must
        # not start a further poll of its own.
        entity_already_polled = wait or bool(result.get("entity_not_verified"))
        entity_id = await self._resolve_post_write_automation_entity(
            post_write_identifier,
            result.get("entity_id"),
            wait,
            result,
        )
        _sync_post_write_automation_result(result, entity_id)

        if result.get("entity_not_verified"):
            result.setdefault("warnings", []).append(
                f"{NOT_VERIFIED_WARNING_PREFIX} "
                "after polling. The automation may still have been created -- check Home "
                "Assistant logs and try reloading automations. Common causes: "
                "automations.yaml vs automation.yaml filename mismatch, invalid config "
                "that HA accepted but failed to load, or slow hardware."
            )
            result.pop("entity_not_verified", None)

        if not entity_id and identifier and identifier.startswith("automation."):
            entity_id = identifier
        if wait and entity_id:
            action_word = "created" if identifier is None else "updated"
            try:
                registered = await self._wait_registered(entity_id)
                if not registered:
                    result.setdefault("warnings", []).append(
                        f"Automation {action_word} but {entity_id} not yet queryable. "
                        "It may take a moment to become available."
                    )
            except _RUNTIME_ERRORS as e:
                result.setdefault("warnings", []).append(
                    f"Automation {action_word} but verification failed: {e}"
                )

        if effective_category and entity_id:
            await self._apply_category(
                entity_id,
                effective_category,
                "automation",
                result,
                "automation",
            )

        entity_id = await self._apply_runtime_state(
            result,
            entity_id,
            enabled,
            wait,
            identifier=None if entity_already_polled else post_write_identifier,
            run_actions=run_actions,
        )
        _sync_post_write_automation_result(result, entity_id)

        if bp_warnings:
            result["best_practice_warnings"] = list(bp_warnings)

        merge_validation_meta(result, validation_meta)

        automation_id = entity_id or identifier or result.get("unique_id")
        response = {
            "success": True,
            # automation_id omitted when all three fallbacks are falsy —
            # the create path is unguarded by validate_identifier_not_empty,
            # and surfacing automation_id=None would lie about resolvability.
            # Defensive: HA's upsert normally returns a usable id, so this
            # fallback rarely triggers.
            **({"automation_id": automation_id} if automation_id else {}),
            **_strip_redundant_identifier_echo(result),
        }
        if detached_blueprint:
            response["took_control_of_blueprint"] = detached_blueprint
        # attach AFTER the outer dict is built so attach_skill_content's
        # reorder puts skill_content_hint at position 0 of the FINAL
        # response — building the outer dict via spread otherwise pushes
        # the hint to position 2-3 behind success/automation_id, which
        # is the exact position BAT showed small models can't find.
        await self.attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=_AUTOMATION_SKILL_FILES,
            referenced_files=bp_warnings.referenced_files,
        )
        return response
