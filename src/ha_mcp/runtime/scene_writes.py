"""Shared scene authoring, transforms, reload-confirmed and standalone activation."""

import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp.errors import (
    ErrorCode,
    create_entity_not_found_error,
    create_error_response,
)
from ha_mcp.tool_api import ToolFailure
from ha_mcp.tool_api.parameters import BestPracticeKeyParam
from ha_mcp.tools.coercion import JSON_STRING_COERCION
from ha_mcp.utils.python_sandbox import (
    PythonSandboxError,
    format_sandbox_error,
    get_security_documentation,
)

from .config_reload import config_reload_waiter
from .config_write_context import merge_validation_meta
from .errors import raise_classified, raise_failure
from .scene_config import SceneConfigTools
from .validation import validate_identifier_not_empty

logger = logging.getLogger(__name__)
_SCENE_SKILL_FILES: tuple[str, ...] = ("SKILL.md",)


def _activate_once_reloaded(
    result: dict[str, Any], reloaded: bool | None, activate: bool, scene_id: str
) -> bool:
    """Keep activate after a write only once the scene reload is confirmed.

    Activating before the reload would apply the previous snapshot or hit a
    scene entity the reload is about to replace.
    """
    if not activate or reloaded:
        return activate
    result["activated"] = False
    result.setdefault("warnings", []).append(
        "Scene was written, but not activated: the scene reload the write "
        "scheduled could not be confirmed, so the previous snapshot could have "
        f"been applied. Activate it with ha_config_set_scene(scene_id='{scene_id}', "
        "activate=True)."
    )
    return False


class SceneWriteTools(SceneConfigTools):
    async def _written_scene_key(
        self, result: dict[str, Any], resolved_id: str | None, scene_id: str
    ) -> str:
        key = result.get("scene_id") or resolved_id
        if not isinstance(key, str) or not key:
            # Existing clients always return their canonical write target. A
            # malformed adapter response must resolve again, never stringify
            # None or silently target a caller-facing renamed entity slug.
            resolution = await self._resolve_scene_config(scene_id)
            key = resolution["storage_key"]
        if not isinstance(key, str) or not key:
            raise ToolFailure(
                "INTERNAL_ERROR", "Scene write returned no canonical storage key."
            )
        return key

    async def _raise_scene_write_error(
        self, error: Exception, scene_id: str
    ) -> NoReturn:
        try:
            raise_classified(error, {"scene_id": scene_id})
        except ToolFailure as failure:
            raise await self._augment_failure(failure, None) from None

    async def _validate_category_id(self, category: str) -> None:
        await self._validate_categories({"scene": category})

    async def _scene_state_exists(self, key: str) -> bool:
        entity_id = f"scene.{key.removeprefix('scene.')}"
        result = await self.backend.execute(
            "states.read", {"entity_ids": [entity_id]}, self.context
        )
        return entity_id in result["states"]

    async def _ensure_scene_exists(self, entity_id: str) -> None:
        result = await self.backend.execute(
            "states.read", {"entity_ids": [entity_id]}, self.context
        )
        if entity_id not in result["states"]:
            raise_failure(create_entity_not_found_error(entity_id))

    def _config_reload_waiter(self, *, enabled: bool) -> Any:
        return config_reload_waiter(
            self.backend, self.context, "scene_reloaded", enabled=enabled
        )

    async def _scene_runtime_service(self, entity_id: str) -> None:
        await self._runtime_service("scene", "turn_on", {"entity_id": entity_id})

    async def _upsert_scene(
        self, config: dict[str, Any], scene_id: str, *, resolved_id: str | None
    ) -> dict[str, Any]:
        return await self._upsert_config(
            "scene",
            config,
            scene_id,
            resolved_id,
            getattr(self, "_last_verified_scene", None),
        )

    async def ha_config_set_scene(
        self,
        scene_id: Annotated[
            str, Field(description="Scene identifier (e.g., 'movie_night')")
        ],
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Scene configuration dictionary. Must include 'entities' "
                    "(a dict keyed by entity_id, NOT a list). Optional fields: "
                    "'name' (defaults to scene_id), 'icon', 'id'. "
                    "Mutually exclusive with python_transform."
                ),
                default=None,
            ),
        ] = None,
        python_transform: Annotated[
            str | None,
            Field(
                description=(
                    "Python expression to transform existing scene config. "
                    "Mutually exclusive with config. "
                    "WARNING: Expressions with infinite loops will hang the server. "
                    "Examples: "
                    "Add entity: python_transform=\"config['entities']['light.bed'] = {'state': 'on'}\" "
                    "Update brightness: python_transform=\"config['entities']['light.kitchen']['brightness'] = 50\" "
                    "Remove entity: python_transform=\"del config['entities']['light.kitchen']\" "
                    "\n\n" + get_security_documentation()
                ),
            ),
        ] = None,
        config_hash: Annotated[
            str | None,
            Field(
                description=(
                    "Config hash from ha_config_get_scene for optimistic locking. "
                    "REQUIRED for python_transform (validates scene unchanged). "
                    "Optional for config updates (validates before full replacement if provided)."
                ),
            ),
        ] = None,
        category: Annotated[
            str | None,
            Field(
                description=(
                    "Category ID to assign to this scene. Use ha_config_get_category(scope='scene') "
                    "to list available categories, or ha_config_set_category() to create one."
                ),
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool,
            Field(
                description=(
                    "Wait for scene to be queryable before returning. Set to False for bulk"
                    " operations."
                ),
                default=True,
            ),
        ] = True,
        activate: Annotated[
            bool,
            Field(
                description=(
                    "Activate the scene (scene.turn_on). Alone with scene_id it "
                    "activates any scene entity, including integration and YAML "
                    "scenes (pass their entity_id). With config or "
                    "python_transform it runs after Home Assistant has reloaded "
                    "scenes from the write."
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
        """
        Create or update a Home Assistant scene.

        MUST call ha_get_skill_guide OR refer to your locally installed skills first.

        Supports two modes: full config replacement (``config``) or
        Python transformation of an existing scene (``python_transform``).
        See the field descriptions for ``python_transform`` examples and
        the ``config`` shape contract. Either mode can also take
        ``activate``, which can be passed alone with ``scene_id`` to activate
        a scene without touching its config.

        WHEN TO USE:
        - ``python_transform``: surgical edits to an existing scene
          (add/remove/update a single entity entry).
        - ``config``: creating a new scene, or wholesale replacement.
        - ``activate`` (with scene_id, no config): activate any scene,
          e.g. ha_config_set_scene(scene_id="scene.movie_night", activate=True).

        WHEN NOT TO USE:
        - To list or look up existing scenes, use ha_config_get_scene.

        SCENE SHAPE: Automations use a list of actions; scenes capture a snapshot
        of states as a dict.

        EXAMPLE:

        ha_config_set_scene(scene_id="movie_night", config={
            "name": "Movie Night",
            "entities": {
                "light.living_room": {"state": "on", "brightness": 50},
            },
            "icon": "mdi:movie",
        })

        The top-level ``SKILL.md`` for home-assistant-best-practices ships in
        this response under ``skill_content`` by default — generic
        best-practice index covering entity-naming and
        safe-refactoring patterns that intersect with scene authoring. For
        detailed scene configuration help beyond that, use ha_get_skill_guide.
        """
        try:
            # Issue #1168 R6 blocker 16: empty ``scene_id`` pre-flight before
            # any config dispatch — keeps the error code/message aligned with
            # the actual problem rather than the misleading
            # ``RESOURCE_NOT_FOUND`` from a downstream lookup. Migrated to
            # the shared ``validate_identifier_not_empty`` helper (#1314) —
            # message and ``context["scene_id"]`` key preserved for callers.
            validate_identifier_not_empty(
                scene_id,
                "scene_id",
                message="scene_id must not be empty",
                suggestions=[
                    "Pass a non-empty scene identifier (e.g. 'movie_night')",
                    "For a fresh create, use a name-derived slug",
                ],
                context={"scene_id": scene_id},
            )
            # Validate mutual exclusivity of config and python_transform
            if config is not None and python_transform is not None:
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Cannot use both config and python_transform simultaneously",
                        suggestions=[
                            "Use only ONE of: config or python_transform",
                            "config: Full replacement",
                            "python_transform: Python-based edits (recommended for existing scenes)",
                        ],
                        context={"action": "set", "scene_id": scene_id},
                    )
                )

            if activate and config is None and python_transform is None:
                return await self._activate_scene_only(scene_id, category, MandatoryBPS)

            if python_transform is not None:
                return await self._run_scene_python_transform(
                    scene_id,
                    config_hash,
                    python_transform,
                    category,
                    wait,
                    MandatoryBPS,
                    activate=activate,
                )

            if config is None:
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Either config or python_transform must be provided",
                        suggestions=[
                            "config: Full scene configuration for create/replace",
                            "python_transform: Python expression for surgical edits",
                        ],
                        context={"action": "set", "scene_id": scene_id},
                    )
                )

            return await self._run_scene_config_replace(
                scene_id,
                config,
                config_hash,
                category,
                wait,
                MandatoryBPS,
                activate=activate,
            )

        except ToolFailure as failure:
            raise await self._augment_failure(failure, None) from None
        except Exception as error:  # noqa: BLE001 - facade-specific classification
            await self._raise_scene_write_error(error, scene_id)

    async def _activate_scene_only(
        self, scene_id: str, category: str | None, MandatoryBPS: bool
    ) -> dict[str, Any]:
        """Activate an existing scene without touching its config."""
        if category is not None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "category requires a config update",
                    suggestions=[
                        "Pass config or python_transform when assigning a category",
                        "Omit category to only activate the scene",
                    ],
                    context={"action": "activate", "category": category},
                )
            )
        # Integration and YAML scenes have no storage key, so an entity_id is
        # taken as-is; a bare key goes through the registry like other reads.
        entity_id = (
            scene_id
            if scene_id.startswith("scene.")
            else await self._resolve_scene_entity_id(scene_id)
        )
        # HA's scene.turn_on is a silent no-op on an unknown entity.
        await self._ensure_scene_exists(entity_id)
        response: dict[str, Any] = {
            "success": True,
            "action": "activate",
            "scene_id": scene_id,
            "entity_id": entity_id,
        }
        await self._activate_scene(response, entity_id, standalone=True)
        await self.attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=_SCENE_SKILL_FILES,
            referenced_files=None,
        )
        return response

    async def _activate_scene(
        self, response: dict[str, Any], entity_id: str, *, standalone: bool
    ) -> None:
        """Call scene.turn_on; raise when standalone, warn after a config write."""
        try:
            await self._scene_runtime_service(entity_id)
        except (ToolFailure, PermissionError, TimeoutError, OSError) as exc:
            if standalone:
                raise_classified(
                    exc, context={"action": "activate", "entity_id": entity_id}
                )
            logger.warning(
                "Scene %s not activated after config write: %s", entity_id, exc
            )
            response["activated"] = False
            response.setdefault("warnings", []).append(
                f"Scene config was written, but the scene could not be activated: {exc}"
            )
            return
        response["activated"] = True

    async def _run_scene_python_transform(
        self,
        scene_id: str,
        config_hash: str | None,
        python_transform: str,
        category: str | None,
        wait: bool,
        MandatoryBPS: bool,
        *,
        activate: bool = False,
    ) -> dict[str, Any]:
        """Execute ``ha_config_set_scene``'s python_transform mode.

        Extracted verbatim from the inline branch; returns the tool response.
        """
        if config_hash is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "config_hash is required for python_transform",
                    suggestions=[
                        "Call ha_config_get_scene() first",
                        "Use the config_hash from that response",
                    ],
                    context={
                        "action": "python_transform",
                        "scene_id": scene_id,
                    },
                )
            )

        actual_config, resolved_id = await self._fetch_and_verify_hash(
            scene_id, config_hash, "python_transform"
        )
        # Capture the scene's own ``id`` BEFORE the transform mutates the
        # config in place — it is the stable identity the id-change guard
        # compares against (in production it equals ``resolved_id``; it is
        # also correct when ``resolved_id`` is None because the envelope
        # omitted ``scene_id``).
        original_id = actual_config.get("id")

        try:
            transformed_config = await self._transform_config(
                python_transform, actual_config
            )
        except PythonSandboxError as e:
            message, suggestions = format_sandbox_error(e, python_transform)
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    message,
                    suggestions=suggestions,
                    context={
                        "action": "python_transform",
                        "scene_id": resolved_id,
                    },
                )
            )

        self._validate_transformed_scene(transformed_config, resolved_id, original_id)
        self._prune_scene_metadata(transformed_config, resolved_id)

        # Issue #1168 R6 blocker 15: pre-validate ``category`` BEFORE
        # the upsert commits. Validating after the write produced the
        # exact partial-state-mutation pattern this PR's auto-attach
        # contract was meant to eliminate — scene was rewritten,
        # caller saw VALIDATION_INVALID_PARAMETER on a phantom
        # category, and the storage state had already moved.
        if category:
            await self._validate_category_id(category)

        # ``resolved_id`` is the storage key (write target); ``scene_id``
        # stays the caller id so a missing ``name`` defaults caller-facing
        # rather than to the storage key (#1935).
        # Scene writes reload every scene; activation has to follow that reload.
        async with self._config_reload_waiter(enabled=activate) as wait_for_reload:
            result = await self._upsert_scene(
                transformed_config, scene_id, resolved_id=resolved_id
            )
            reloaded = await wait_for_reload()
        activate = _activate_once_reloaded(result, reloaded, activate, scene_id)
        # The upsert re-resolves when ``resolved_id`` was None (envelope
        # omitted the key); re-bind to the authoritative write target it
        # returned so the re-fetch, entity resolution, and response all
        # use the resolved storage key rather than a possible None.
        resolved_id = await self._written_scene_key(result, resolved_id, scene_id)

        # Re-fetch to get authoritative hash (HA may normalise after save).
        # ``resolved_id`` is already the storage key, so skip the re-resolve.
        _, new_config_hash, _ = await self._get_scene_config_internal(
            resolved_id, _resolved=True
        )

        # Resolve actual entity_id and apply wait + category — same
        # post-upsert finalisation the full-config branch runs. Without
        # these, ``wait`` and ``category`` are silently dropped on
        # python_transform calls.
        entity_id = (
            await self._resolve_written_entity(resolved_id, "scene")
            if wait or category or activate
            else f"scene.{resolved_id}"
        )
        if wait:
            try:
                registered = await self._wait_registered(entity_id)
                if not registered:
                    result.setdefault("warnings", []).append(
                        f"Scene updated but {entity_id} not yet queryable. "
                        "It may take a moment to become available."
                    )
            except (ToolFailure, PermissionError, TimeoutError, OSError) as e:
                result.setdefault("warnings", []).append(
                    f"Scene updated but verification failed: {e}"
                )
        if category and entity_id:
            # Pre-validation of ``category`` already ran before the
            # upsert (R6 blocker 15) — apply directly here.
            await self._apply_category(
                entity_id,
                category,
                "scene",
                result,
                "scene",
            )
        if activate:
            await self._activate_scene(result, entity_id, standalone=False)

        # Issue #1168 R3 blocker 6: build the response from
        # ``resolved_id`` directly (not the caller-input ``scene_id``)
        # so the outer field always matches the storage key carried
        # in ``result``. ``result["scene_id"]`` is also the storage
        # key from rest_client; the kwarg-after-spread order means
        # the explicit assignment wins on key-collision regardless.
        response: dict[str, Any] = {
            "success": True,
            **{k: v for k, v in result.items() if k != "success"},
            "action": "python_transform",
            "scene_id": resolved_id,
            "config_hash": new_config_hash,
            "python_expression": python_transform,
            "message": f"Scene {resolved_id} updated via Python transform",
        }
        await self.attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=_SCENE_SKILL_FILES,
            referenced_files=None,
        )
        return response

    async def _run_scene_config_replace(
        self,
        scene_id: str,
        config: dict[str, Any],
        config_hash: str | None,
        category: str | None,
        wait: bool,
        MandatoryBPS: bool,
        *,
        activate: bool = False,
    ) -> dict[str, Any]:
        """Execute ``ha_config_set_scene``'s full-config replacement mode.

        Extracted verbatim from the inline branch; returns the tool response.
        """
        config_dict, effective_category = self._validate_scene_config(
            config,
            scene_id,
            category,
        )

        # Issue #1168 R6 blocker 15: pre-validate ``category`` BEFORE
        # the hash check + upsert commits. Same partial-state hazard as
        # the python_transform branch — a phantom category must not
        # rewrite the scene before erroring.
        #
        # Issue #1168 R8 (post-merge follow-up): gate on
        # ``effective_category`` truthy, not on the user-facing
        # ``category`` param. ``_validate_scene_config`` promotes a
        # top-level ``config["category"]`` into ``effective_category``
        # when the user passed ``category=None``; the R7 fix gated on
        # ``category is not None`` to skip the WS round-trip when no
        # category was supplied, but that left the dict-promoted path
        # uncovered — a phantom category in ``config["category"]``
        # would skip validation and reach ``apply_entity_category``,
        # which attaches the phantom ID to the entity registry without
        # checking it exists. Truthy check covers both sources and
        # still skips the WS call when neither produces a category.
        if effective_category:
            await self._validate_category_id(effective_category)

        # Issue #1168 R3 blocker 7: when caller passes ``config_hash``,
        # honor the optimistic-locking semantics promised by the field
        # description. ``_fetch_and_verify_hash`` raises ToolError on
        # mismatch. We capture ``resolved_id`` from the verified fetch
        # so subsequent upsert/response builders use the storage key
        # consistently (issue #1168 R3 blocker 6).
        #
        # Path branching: the hash arm's inner fetch reads the config
        # first, so a non-storage scene (Hue/vendor, raw-YAML) surfaces
        # as CONFIG_NOT_FOUND and a genuinely-missing scene as the bare
        # 404 – you cannot lock against a config that isn't there. The
        # no-hash arm has no such read, so it resolves richly and, when
        # the entity already exists, pre-checks the config API below
        # before the upsert (issue #1971) so a plain ``set`` on a
        # non-storage scene cannot silently shadow-create a duplicate.
        if config_hash:
            _, resolved_id = await self._fetch_and_verify_hash(
                scene_id, config_hash, "set"
            )
        else:
            resolution = await self._resolve_scene_config(scene_id)
            resolved_id = resolution["storage_key"]
            # If the scene already exists this is an EDIT, and without a hash
            # there is no prior read: a POST to ``config/scene/config/<key>``
            # for a Hue/vendor or YAML scene would CREATE a duplicate managed
            # ``scenes.yaml`` entry instead of erroring. Pre-check the config
            # API first – its 404 raises SceneStorageConfigNotFoundError, which
            # the outer handler maps to CONFIG_NOT_FOUND.
            #
            # Existence takes both signals: an ``id``-less YAML scene has no
            # registry unique_id yet still holds a state-machine entity, and
            # shadow-creates just the same. Only a scene missing from both is a
            # real create, at the price of one extra ``/states/`` GET there.
            if resolution["registry_hit"] or await self._scene_state_exists(
                resolved_id
            ):
                await self._read_scene_envelope(scene_id, resolution=resolution)

        # Cross-check literal service and entity references against the
        # live registries. Soft warnings only.
        validation_meta = await self._validate_references(config_dict)

        # ``resolved_id`` is already the storage key (from the hash-verify
        # fetch or the resolve above) — pass it as the write target to skip
        # the redundant re-resolve. ``scene_id`` stays the caller id so a
        # missing ``name`` defaults caller-facing, not to the storage key
        # (#1935).
        # Scene writes reload every scene; activation has to follow that reload.
        async with self._config_reload_waiter(enabled=activate) as wait_for_reload:
            result = await self._upsert_scene(
                config_dict, scene_id, resolved_id=resolved_id
            )
            reloaded = await wait_for_reload()
        activate = _activate_once_reloaded(result, reloaded, activate, scene_id)
        # The upsert re-resolves when ``resolved_id`` was None (envelope
        # omitted the key); re-bind to the authoritative write target it
        # returned so entity resolution and the response use the resolved
        # storage key rather than a possible None.
        resolved_id = await self._written_scene_key(result, resolved_id, scene_id)

        # Resolve actual entity_id via registry — HA derives scene
        # entity_ids from the 'name' slug, not the scene_id storage key,
        # so f"scene.{scene_id}" is wrong whenever a name is supplied.
        entity_id = (
            await self._resolve_written_entity(resolved_id, "scene")
            if wait or effective_category or activate
            else f"scene.{resolved_id}"
        )

        # Wait for scene to be queryable
        if wait:
            try:
                registered = await self._wait_registered(entity_id)
                if not registered:
                    result.setdefault("warnings", []).append(
                        f"Scene saved but {entity_id} not yet queryable. "
                        "It may take a moment to become available."
                    )
            except (ToolFailure, PermissionError, TimeoutError, OSError) as e:
                result.setdefault("warnings", []).append(
                    f"Scene saved but verification failed: {e}"
                )

        # Apply category to entity registry if provided.
        if effective_category and entity_id:
            # Pre-validation of ``effective_category`` already ran before
            # the upsert (R6 blocker 15) — apply directly here.
            await self._apply_category(
                entity_id,
                effective_category,
                "scene",
                result,
                "scene",
            )
        if activate:
            await self._activate_scene(result, entity_id, standalone=False)

        merge_validation_meta(result, validation_meta)

        # Issue #1168 R3 blocker 6: build response from ``resolved_id``
        # so the outer ``scene_id`` always matches the storage key.
        # ``result["scene_id"]`` is also the storage key (from
        # rest_client); explicit assignment after the spread guards
        # against any future result-shape drift.
        response = {
            "success": True,
            **result,
            "scene_id": resolved_id,
        }
        # attach AFTER the outer dict is built so hint lands at
        # position 0 of the FINAL response (see
        # config_write_helpers._SKILL_CONTENT_OPTOUT_HINT).
        await self.attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=_SCENE_SKILL_FILES,
            referenced_files=None,
        )
        return response
