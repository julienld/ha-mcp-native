"""Scene config validation, identity checks and exact before-images."""

import copy
import logging
from typing import Any, cast

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.coercion import parse_json_param
from ha_mcp.utils.config_hash import compute_config_hash

from .config_read_errors import raise_scene_config_missing
from .config_removals import SceneRemovalTools
from .config_write_context import ConfigWriteContext
from .errors import raise_failure

logger = logging.getLogger(__name__)


class SceneConfigTools(SceneRemovalTools, ConfigWriteContext):
    async def _read_scene_envelope(
        self, scene_id: str, *, _resolved: bool = False, resolution: Any = None
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"identifier": scene_id}
        if _resolved:
            arguments["resolution"] = {
                "storage_key": scene_id.removeprefix("scene."),
                "registry_hit": False,
                "platform": None,
            }
        elif resolution is not None:
            arguments["resolution"] = resolution
        result = await self.backend.execute(
            "config.scene.read", arguments, self.context
        )
        if not result["found"]:
            raise_scene_config_missing(scene_id, result)
        return {
            "config": result["config"],
            "scene_id": result["storage_key"],
            "success": True,
        }

    async def _get_scene_config_internal(
        self, scene_id: str, *, _resolved: bool = False
    ) -> tuple[dict[str, Any], str, str | None]:
        """Fetch scene config without logging or category injection.

        Returns ``(actual_config, config_hash, resolved_id)`` where
        ``actual_config`` is the inner scene body (not the REST wrapper) and
        ``resolved_id`` is the storage key the rest-client resolved the input
        to (issue #1168 R3 blocker 6). Used by ``_fetch_and_verify_hash``.

        ``resolved_id`` is ``None`` when the envelope omits ``scene_id`` — the
        write target must then be re-resolved from the caller's input rather
        than defaulting to the (unresolved) caller slug, which for a renamed
        scene would target the wrong storage key. This is a structural
        invariant, not a live path: ``get_scene_config`` always sets the key.

        ``_resolved=True`` forwards to ``get_scene_config`` that ``scene_id`` is
        already a resolved storage key, so the redundant registry lookup is
        skipped (e.g. the python_transform re-fetch, which already resolved).
        """
        envelope = await self._read_scene_envelope(scene_id, _resolved=_resolved)
        actual_config = envelope.get("config", envelope)
        resolved_id = envelope.get("scene_id")
        config_hash_value = compute_config_hash(actual_config)
        return actual_config, config_hash_value, resolved_id

    async def _fetch_and_verify_hash(
        self, scene_id: str, config_hash: str, action: str
    ) -> tuple[dict[str, Any], str | None]:
        """Fetch current scene config and verify config_hash for optimistic locking.

        Returns ``(actual_config, resolved_id)`` — the inner scene body and
        the storage key the rest-client resolved the input to. ``resolved_id``
        is ``None`` when the envelope omitted the key, so the upsert re-resolves
        rather than writing to the caller slug; the caller then re-binds it from
        the upsert result. Raises ``ToolError`` if the hash does not match
        (conflict). Issue #1168 R3 blocker 6: callers thread ``resolved_id`` into
        responses so the outer ``scene_id`` matches the inner body's ``id``
        regardless of whether the caller passed the entity_id slug or the
        storage key.
        """
        (
            actual_config,
            current_hash,
            resolved_id,
        ) = await self._get_scene_config_internal(scene_id)
        if current_hash != config_hash:
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Scene modified since last read (conflict)",
                    suggestions=[
                        "Retry with the fresh config_hash returned in this error's context",
                        "Or call ha_config_get_scene() again to fetch a new hash",
                    ],
                    context={
                        "action": action,
                        "scene_id": resolved_id,
                        "current_config_hash": current_hash,
                    },
                )
            )
        self._last_verified_scene = (copy.deepcopy(actual_config), resolved_id)
        return actual_config, resolved_id

    @staticmethod
    def _validate_scene_config(
        config: str | dict[str, Any],
        scene_id: str,
        category: str | None,
    ) -> tuple[dict[str, Any], str | None]:
        """Parse and validate scene config, returning ``(config_dict, effective_category)``.

        Parses JSON string config, validates it is a dict, checks for the
        required ``entities`` field, and extracts category.
        """
        try:
            parsed_config = parse_json_param(config, "config")
        except ValueError as e:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_JSON,
                    f"Invalid config parameter: {e}",
                    context={
                        "scene_id": scene_id,
                        "provided_config_type": type(config).__name__,
                    },
                )
            )

        if parsed_config is None or not isinstance(parsed_config, dict):
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Config parameter must be a JSON object",
                    context={
                        "scene_id": scene_id,
                        "provided_type": type(parsed_config).__name__,
                    },
                )
            )

        config_dict = cast(dict[str, Any], parsed_config)

        # Extract category before sending to HA REST API (rejects unknown keys).
        # Parameter takes precedence over config dict value.
        config_category = config_dict.pop("category", None)
        effective_category = category if category is not None else config_category

        # Required field check. ``entities`` must be a dict keyed by entity_id.
        if "entities" not in config_dict:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "config must include an 'entities' field (a dict keyed by entity_id)",
                    context={"scene_id": scene_id, "required_fields": ["entities"]},
                )
            )

        if not isinstance(config_dict["entities"], dict):
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Scene 'entities' must be a dict keyed by entity_id, not a list",
                    suggestions=[
                        "Scene shape: {'entities': {'light.kitchen': {'state': 'on'}}}",
                        "Automations use a list of actions; scenes do not",
                    ],
                    context={
                        "scene_id": scene_id,
                        "provided_type": type(config_dict["entities"]).__name__,
                    },
                )
            )

        return config_dict, effective_category

    @staticmethod
    def _validate_transformed_scene(
        transformed_config: Any,
        resolved_id: str | None,
        original_id: Any,
    ) -> None:
        """Validate a python_transform result still matches the scene shape.

        Rejects a ``None`` rebind, a missing/non-dict ``entities`` field, and an
        in-place ``id`` change (rename). Raises a structured ToolError on any
        violation; returns normally when the transformed config is valid.
        Extracted verbatim from ``ha_config_set_scene``'s python_transform branch.
        """
        # Issue #1168 R5 blocker 8: a transform that reassigns
        # config to ``None`` (or returns ``None`` from a list-comp
        # mistake) used to crash the next ``in`` check with a
        # ``TypeError`` that surfaced as ``INTERNAL_ERROR``. Catch
        # the rebind here so the user gets the same VALIDATION
        # signal the dict-shape checks below produce.
        if transformed_config is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    "Transform must not reassign config to None",
                    suggestions=[
                        "The transform must produce a dict matching the scene config shape",
                        "Mutate ``config`` in-place rather than reassigning it",
                    ],
                    context={
                        "action": "python_transform",
                        "scene_id": resolved_id,
                    },
                )
            )

        # Validate transformed config still has the required shape.
        if "entities" not in transformed_config:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    "Transformed config must still include 'entities'",
                    suggestions=[
                        "The transform may have removed the required field",
                        "Ensure the config still has an 'entities' key",
                    ],
                    context={
                        "action": "python_transform",
                        "scene_id": resolved_id,
                    },
                )
            )
        if not isinstance(transformed_config["entities"], dict):
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    "Transformed 'entities' must remain a dict keyed by entity_id",
                    context={
                        "action": "python_transform",
                        "scene_id": resolved_id,
                        "resulting_type": type(transformed_config["entities"]).__name__,
                    },
                )
            )

        # Issue #1168 R5 blocker 10: a transform that mutates
        # ``config['id']`` produces a duplicate scene at the new
        # storage key AND orphans the original (state goes
        # ``unavailable``, registry row left behind). HA itself
        # accepts the mismatched-id upsert silently. Reject the
        # rename here — renaming a scene means delete-old +
        # create-new through the explicit tools, not an in-place
        # ``id`` rebind. R6 blocker 18: only reject an explicit
        # mismatched id; a transform that ``del config['id']`` is
        # legitimate (HA treats ``id`` as optional) and should pass
        # through. R7 blocker 24: ``transformed_config["id"] = None``
        # is the in-place equivalent of ``del`` — normalise both
        # by checking against ``(None, original_id)``, so only an
        # explicit non-None mismatch triggers the reject. Compare against
        # the scene's own captured ``id`` (not ``resolved_id``, which may
        # be None on the structural-invariant path) — in production the
        # two are equal.
        if "id" in transformed_config and transformed_config["id"] not in (
            None,
            original_id,
        ):
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    "Transform must not change ``config['id']``",
                    suggestions=[
                        f"Original scene_id is {original_id!r} — keep it unchanged",
                        "To rename a scene, use ha_config_remove_scene + ha_config_set_scene",
                    ],
                    context={
                        "action": "python_transform",
                        "scene_id": original_id,
                        "attempted_id": transformed_config.get("id"),
                    },
                )
            )

    @staticmethod
    def _prune_scene_metadata(
        transformed_config: dict[str, Any], resolved_id: str | None
    ) -> None:
        """Drop orphan ``metadata`` keys whose entity was removed by the transform.

        Extracted verbatim from ``ha_config_set_scene``'s python_transform branch.
        """
        # Issue #1168 R3 blocker 5: prune orphan ``metadata`` keys
        # whose entity was deleted by the transform (e.g. a list
        # comprehension that filters ``entities`` doesn't touch
        # ``metadata`` and HA keeps the orphan entries on disk).
        # Full-replace via ``config=`` clears metadata cleanly; the
        # transform path needs to mirror that contract.
        metadata = transformed_config.get("metadata")
        if isinstance(metadata, dict):
            valid_keys = set(transformed_config["entities"].keys())
            pruned_keys = sorted(k for k in metadata if k not in valid_keys)
            if pruned_keys:
                # Issue #1168 R5 blocker 12: a buggy transform that
                # accidentally drops entities used to silently
                # rewrite metadata. Surface the prune so a stray
                # ``del entities[k]`` doesn't lose the friendly_name
                # without trace.
                logger.info(
                    f"python_transform pruned {len(pruned_keys)} orphan "
                    f"metadata key(s) from scene {resolved_id}: {pruned_keys}"
                )
            transformed_config["metadata"] = {
                k: v for k, v in metadata.items() if k in valid_keys
            }
