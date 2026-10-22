"""Shared script authoring, transforms, blueprint takeover and runtime control."""

import logging
from typing import Annotated, Any, Literal, NoReturn

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import ToolFailure
from ha_mcp.tool_api.parameters import BestPracticeKeyParam
from ha_mcp.tools.best_practice_checker import BestPracticeCheckResult
from ha_mcp.tools.best_practice_checker import (
    check_script_config as _check_best_practices,
)
from ha_mcp.tools.coercion import JSON_STRING_COERCION
from ha_mcp.utils.python_sandbox import get_security_documentation

from .blueprint_substitute import validate_write_modes
from .config_reads import _missing_config
from .config_write_context import merge_validation_meta
from .errors import raise_failure
from .script_config import ScriptConfigTools
from .validation import validate_identifier_not_empty

logger = logging.getLogger(__name__)

_SCRIPT_SKILL_FILES: tuple[str, ...] = (
    "references/automation-actions.md",
    "references/triggers-and-conditions.md",
    "references/template-guidelines.md",
)


class ScriptWriteTools(ScriptConfigTools):
    async def _raise_script_write_error(
        self, error: Exception, script_id: str, bp_warnings: BestPracticeCheckResult
    ) -> NoReturn:
        data = create_error_response(
            ErrorCode.INTERNAL_ERROR, str(error), context={"script_id": script_id}
        )
        await self._augment_error_dict(data, bp_warnings)
        raise_failure(data)

    async def _ensure_script_exists(self, entity_id: str, script_id: str) -> None:
        result = await self.backend.execute(
            "states.read", {"entity_ids": [entity_id]}, self.context
        )
        if entity_id not in result["states"]:
            current = await self.backend.execute(
                "config.script.read", {"identifier": script_id}, self.context
            )
            _missing_config("script", script_id, current.get("available_ids", []))

    async def _run_script_service(
        self,
        script_id: str,
        run: str | None,
        entity_id: str,
        service: str,
        data: dict[str, Any],
    ) -> None:
        await self._runtime_service("script", service, data)

    async def _upsert_script(
        self, config: dict[str, Any], script_id: str, resolved_id: str | None
    ) -> dict[str, Any]:
        current = await self.backend.execute(
            "config.script.read", {"identifier": script_id}, self.context
        )
        key = current["storage_key"]
        previous = current["config"] if current["found"] else None
        verified = getattr(self, "_last_verified_script", None)
        if verified is not None and (
            previous != verified[0] or (verified[1] is not None and key != verified[1])
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Script modified since last read (conflict)",
                status="not_started",
            )
        if resolved_id is not None and key != resolved_id:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The script identifier now names another stored script.",
                status="not_started",
            )
        backup = await self.backend.execute(
            "config.backup.capture",
            {
                "domain": "script",
                "source_tool": "ha_config_set_script",
                "backup_id": key if previous is not None else None,
                "current_config": previous,
            },
            self.context,
        )
        config.setdefault("alias", script_id)
        result: dict[str, Any] = await self.backend.execute(
            "config.script.write",
            {
                "storage_key": key,
                "expected_config": previous,
                "data": config,
                "identifier": script_id,
            },
            self.context,
        )
        result.update(
            {
                "success": True,
                "script_id": key,
                "operation": "created" if result.get("result") == "ok" else "updated",
            }
        )
        if backup.get("warnings"):
            result.setdefault("warnings", []).extend(backup["warnings"])
        return result

    async def ha_config_set_script(
        self,
        script_id: Annotated[
            str,
            Field(
                description="Script identifier — bare storage key ('morning_routine') or entity_id form ('script.morning_routine'); a leading 'script.' prefix is stripped before lookup."
            ),
        ],
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description="Script configuration dictionary. "
                "Mutually exclusive with python_transform.",
                default=None,
            ),
        ] = None,
        python_transform: Annotated[
            str | None,
            Field(
                description="Python expression to transform existing script config. "
                "Mutually exclusive with config. "
                "WARNING: Expressions with infinite loops will hang the server. "
                "Examples: "
                "Simple: python_transform=\"config['sequence'][0]['data']['message'] = 'Hello'\" "
                "Pattern: python_transform=\"for step in config['sequence']: "
                "if step.get('alias') == 'My Step': step['data']['value'] = 100\" "
                "\n\n" + get_security_documentation(),
            ),
        ] = None,
        config_hash: Annotated[
            str | None,
            Field(
                description="Config hash from ha_config_get_script for optimistic locking. Required"
                " for python_transform; optional for config updates (validates before "
                "full replacement if provided).",
            ),
        ] = None,
        take_control_of_blueprint: Annotated[
            bool,
            Field(
                description="Convert a blueprint-backed script into an editable "
                'standalone one -- the UI\'s "Take control". Renders the blueprint '
                "with its current inputs and saves the result over the same script, "
                "which keeps its script_id, alias and description and then has its "
                "own sequence and no 'use_blueprint'. Mutually exclusive with config "
                "and python_transform.",
                default=False,
            ),
        ] = False,
        category: Annotated[
            str | None,
            Field(
                description="Category ID to assign to this script. Use ha_config_get_category(scope='script') to list available categories, or ha_config_set_category() to create one.",
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool,
            Field(
                description="Wait for script to be queryable before returning. Set to False for "
                "bulk operations.",
                default=True,
            ),
        ] = True,
        run: Annotated[
            Literal["start", "stop"] | None,
            Field(
                description=(
                    "Run control, used alone with script_id (no config, "
                    "python_transform or take_control_of_blueprint): 'start' runs "
                    "the script (script.turn_on; returns once it has started, "
                    "without waiting for it to finish), 'stop' stops its running "
                    "executions (script.turn_off). Stopping does not disable the "
                    "script; Home Assistant has no script enable/disable."
                ),
                default=None,
            ),
        ] = None,
        variables: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "With run='start': values for the script's fields, passed to "
                    "the run as its variables."
                ),
                default=None,
            ),
        ] = None,
        MandatoryBPS: Annotated[
            bool,
            Field(default=True),
        ] = True,
        # BestPracticeKey (#1779): consumed by StrictBpsMiddleware, never read
        # here — see strict_bps.py for the declaration contract.
        BestPracticeKey: BestPracticeKeyParam = None,
    ) -> dict[str, Any]:
        """Create or update a Home Assistant script.

        MUST call ha_get_skill_guide OR refer to your locally installed skills first.

        Prefer native actions (`choose` / `if`, `wait_for_trigger`, `repeat`,
        `for:`) over templates in logic positions; templates belong only in
        `data.*`, notification text, `event_data` and `variables`. The
        best-practice checker reports violations under `best_practice_warnings`.
        `automation-actions.md`, `triggers-and-conditions.md` and
        `template-guidelines.md` ship under `skill_content` by default.

        Scripts use 'sequence', NOT 'trigger' or 'action'; for trigger-based
        execution use ha_config_set_automation.

        MODES (pick one):
        - python_transform: surgical edits to an existing script. Requires
          config_hash from ha_config_get_script(), e.g.
          python_transform="config['sequence'].append({'delay': {'seconds': 5}})"
        - config: new scripts or full restructures. Needs 'sequence' (regular) or
          'use_blueprint' {path, input} (blueprint-based).
        - take_control_of_blueprint: convert a blueprint-backed script into a
          standalone one. Takes no config of its own.
        - run (alone with script_id): 'start' runs the script (optional
          variables), 'stop' stops its running executions. Write config changes
          in a separate call first: Home Assistant signals no completion of the
          script reload a write triggers.

        EXAMPLES:
        - Create: ha_config_set_script(script_id="blink_light", config={"alias": "Light Blink", "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.living_room"}}, {"delay": {"seconds": 2}}, {"action": "light.turn_off", "target": {"entity_id": "light.living_room"}}]})
        - From a blueprint: ha_config_set_script(script_id="notification_script", config={"alias": "My Notification Script", "use_blueprint": {"path": "notification_script.yaml", "input": {"message": "Hello World"}}})
        - Take control: ha_config_set_script(script_id="notification_script", take_control_of_blueprint=True)

        TAKE CONTROL is one-way: later blueprint edits stop reaching the script;
        to change an input value, update 'use_blueprint.input' instead. It does
        NOT free the blueprint — Home Assistant keeps counting the converted
        script as a user, so deleting that blueprint stays refused until the
        script is removed. To preview the rendering without writing anything,
        use ha_manage_blueprints(action="substitute", domain="script", path=..., input=...).
        """
        bp_warnings: BestPracticeCheckResult = BestPracticeCheckResult()
        try:
            # Strip BEFORE validate so a bare ``"script."`` (empty after
            # strip) is rejected as ``VALIDATION_INVALID_PARAMETER`` rather
            # than slipping through validate (non-empty pre-strip) and
            # writing a phantom ``script.foo`` storage key — HA keys writes
            # by the literal ``script_id``, so passing ``"script.foo"``
            # unchanged makes the row invisible to a later
            # ``ha_config_get_script("foo")``. Behavioral parity with
            # ``ha_config_get_script`` so an agent that received an
            # entity_id (``script.foo``) from
            # ``ha_search(domain_filter='script')`` can update it
            # without a manual prefix-strip step.
            script_id = script_id.removeprefix("script.")
            # ``script_id`` is required (always non-None). Reject empty/
            # whitespace up-front so the caller gets a structured parameter
            # error instead of a misleading ``RESOURCE_NOT_FOUND`` from
            # the downstream upsert/fetch. Extension of the #1312
            # validate_identifier_not_empty pattern to the scripts family
            # per #1313.
            validate_identifier_not_empty(
                script_id,
                "script_id",
                suggestions=[
                    "Pass a script identifier (e.g. 'morning_routine')",
                    "Use ha_search(domain_filter='script') to list scripts",
                ],
                context={"action": "set"},
            )
            if run is not None or variables is not None:
                return await self._run_script_control(
                    script_id,
                    run,
                    variables,
                    has_write=config is not None
                    or python_transform is not None
                    or take_control_of_blueprint,
                    category=category,
                    MandatoryBPS=MandatoryBPS,
                )
            validate_write_modes(
                "script",
                "script_id",
                script_id,
                config,
                python_transform,
                take_control_of_blueprint,
            )

            detached_blueprint: str | None = None
            if take_control_of_blueprint:
                taken = await self._take_control_config(script_id)
                config = taken.config
                detached_blueprint = taken.blueprint_path
                # Take control is a read-modify-write the TOOL performs, so it
                # owns the consistency guarantee the caller could not supply:
                # without this, an edit landing between that read and this
                # write is silently overwritten. An explicit caller hash wins.
                config_hash = config_hash or taken.config_hash

            # Handle python_transform mode
            if python_transform is not None:
                transformed_config, resolved_id = await self._prepare_script_transform(
                    script_id, config_hash, python_transform
                )
                # Mirror the automations transform path (issue #2159): honor
                # the category param and a transform-set "category" key (the
                # REST API rejects unknown keys), validate before the write,
                # and apply post-upsert.
                transform_category = transformed_config.pop("category", None)
                effective_category = (
                    category if category is not None else transform_category
                )
                await self._validate_categories({"script": effective_category})
                bp_warnings = _check_best_practices(transformed_config)
                return await self._commit_script_transform(
                    script_id,
                    transformed_config,
                    resolved_id,
                    python_transform,
                    bp_warnings,
                    MandatoryBPS,
                    wait,
                    effective_category,
                )

            if config is None:
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Either config or python_transform must be provided",
                        suggestions=[
                            "config: Full script configuration for create/replace",
                            "python_transform: Python expression for surgical edits",
                        ],
                        context={"action": "set", "script_id": script_id},
                    )
                )

            config_dict, effective_category = self._validate_script_config(
                config,
                script_id,
                category,
            )

            # Optional hash check for full config updates. When it runs it
            # resolves ``script_id`` to the storage key — thread that through so
            # the upsert doesn't re-resolve (issue #1813 Phase 0). Stays None on
            # the no-hash path (raw script_id resolved once, inside upsert).
            resolved_key: str | None = None
            if config_hash:
                _, resolved_key = await self._fetch_and_verify_hash(
                    script_id, config_hash, "set"
                )

            # Pre-check for best-practice issues.
            bp_warnings = _check_best_practices(config_dict)

            # Issue #2159: the category is applied post-upsert via
            # ``apply_entity_category``, which HA accepts unchecked. Reject an
            # unknown one here so no script is created under it.
            await self._validate_categories({"script": effective_category})

            return await self._commit_script_config(
                config_dict,
                script_id,
                effective_category,
                resolved_key,
                wait,
                bp_warnings,
                MandatoryBPS,
                detached_blueprint,
            )

        except ToolFailure as te:
            raise await self._augment_failure(te, bp_warnings) from None
        except Exception as error:  # noqa: BLE001 - facade-specific classification
            await self._raise_script_write_error(error, script_id, bp_warnings)

    async def _run_script_control(
        self,
        script_id: str,
        run: str | None,
        variables: dict[str, Any] | None,
        *,
        has_write: bool,
        category: str | None,
        MandatoryBPS: bool,
    ) -> dict[str, Any]:
        """Start or stop a script without touching its config."""
        problem = None
        if run is None:
            problem = "variables only apply with run='start'"
        elif has_write:
            problem = (
                "run cannot be combined with config, python_transform or "
                "take_control_of_blueprint"
            )
        elif category is not None:
            problem = "category requires a config update"
        elif variables is not None and run != "start":
            problem = "variables only apply with run='start'"
        if problem:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    problem,
                    suggestions=[
                        "Write the script config in one call, then start or stop "
                        + "it in a second call with only script_id and run",
                        "Pass variables together with run='start'",
                    ],
                    context={"action": "run", "script_id": script_id, "run": run},
                )
            )
        # An existing script needs one registry lookup, not a registration poll.
        entity_id = await self._resolve_written_entity(script_id, "script", timeout=0)
        # HA's script services are a silent no-op on an unknown entity.
        await self._ensure_script_exists(entity_id, script_id)
        data: dict[str, Any] = {"entity_id": entity_id}
        if variables:
            data["variables"] = variables
        service = "turn_on" if run == "start" else "turn_off"
        await self._run_script_service(script_id, run, entity_id, service, data)
        response: dict[str, Any] = {
            "success": True,
            "action": run,
            "script_id": script_id,
            "entity_id": entity_id,
        }
        await self.attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=_SCRIPT_SKILL_FILES,
            referenced_files=None,
        )
        return response

    async def _finalize_script_write(
        self,
        result: dict[str, Any],
        script_id: str,
        resolved_key: str | None,
        wait: bool,
        category: str | None,
    ) -> None:
        """Resolve, wait, and apply a category for either script write mode.

        Match the upsert's storage key, which can differ from the caller's
        entity ID after a registry rename. Keep the caller's ID as fallback.
        """
        if not wait and not category:
            return
        storage_key = result.get("script_id") or resolved_key or script_id
        entity_id = await self._resolve_written_entity(
            storage_key,
            "script",
            fallback_entity_id=f"script.{script_id.removeprefix('script.')}",
        )
        if wait:
            try:
                registered = await self._wait_registered(entity_id)
                if not registered:
                    result.setdefault("warnings", []).append(
                        f"Script saved but {entity_id} not yet queryable. "
                        "It may take a moment to become available."
                    )
            except (ToolFailure, PermissionError, TimeoutError, OSError) as e:
                result.setdefault("warnings", []).append(
                    f"Script saved but verification failed: {e}"
                )
        if category:
            await self._apply_category(entity_id, category, "script", result, "script")

    async def _commit_script_transform(
        self,
        script_id: str,
        transformed_config: dict[str, Any],
        resolved_id: str | None,
        python_transform: str,
        bp_warnings: BestPracticeCheckResult,
        MandatoryBPS: bool,
        wait: bool,
        effective_category: str | None = None,
    ) -> dict[str, Any]:
        """Upsert, refresh the hash, finalize wait/category, and build the response."""
        # Save transformed config. ``_fetch_and_verify_hash`` already
        # resolved the storage key; pass it as the write target so the
        # upsert skips the redundant re-resolve (issue #1813 Phase 0).
        # ``script_id`` stays the caller id (the fetched config already
        # carries an alias here, so the default is a no-op, but keep the
        # contract consistent — #1935).
        result = await self._upsert_script(
            transformed_config, script_id, resolved_id=resolved_id
        )

        # Re-fetch to get authoritative hash (HA may normalize after save)
        _, new_config_hash, _ = await self._get_script_config_internal(script_id)

        await self._finalize_script_write(
            result, script_id, resolved_id, wait, effective_category
        )

        response: dict[str, Any] = {
            "success": True,
            "action": "python_transform",
            "script_id": script_id,
            "config_hash": new_config_hash,
            "python_expression": python_transform,
            "message": f"Script {script_id} updated via Python transform",
            # Merge upsert result, excluding "success" (we set it ourselves)
            **{k: v for k, v in result.items() if k != "success"},
        }
        if bp_warnings:
            response["best_practice_warnings"] = list(bp_warnings)
        await self.attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=_SCRIPT_SKILL_FILES,
            referenced_files=bp_warnings.referenced_files,
        )
        return response

    async def _commit_script_config(
        self,
        config_dict: dict[str, Any],
        script_id: str,
        effective_category: str | None,
        resolved_key: str | None,
        wait: bool,
        bp_warnings: BestPracticeCheckResult,
        MandatoryBPS: bool,
        detached_blueprint: str | None = None,
    ) -> dict[str, Any]:
        """Validate references, upsert, finalize wait/category, and build the response."""
        # Cross-check literal service and entity references against
        # the live registries. Soft warnings only — the write still
        # happens, even when references don't resolve (#940).
        validation_meta = await self._validate_references(config_dict)

        result = await self._upsert_script(config_dict, script_id, resolved_key)

        await self._finalize_script_write(
            result, script_id, resolved_key, wait, effective_category
        )

        if bp_warnings:
            result["best_practice_warnings"] = list(bp_warnings)

        merge_validation_meta(result, validation_meta)

        response = {
            "success": True,
            **result,
        }
        if detached_blueprint:
            response["took_control_of_blueprint"] = detached_blueprint
        # attach AFTER the outer dict is built so hint lands at
        # position 0 of the FINAL response (see BAT history in
        # config_write_helpers._SKILL_CONTENT_OPTOUT_HINT).
        await self.attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=_SCRIPT_SKILL_FILES,
            referenced_files=bp_warnings.referenced_files,
        )
        return response
