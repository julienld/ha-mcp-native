"""Shared automation/script reads over fresh persisted-config backend ports."""

from __future__ import annotations

import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import BackendTools, ToolFailure
from ha_mcp.utils.config_hash import compute_config_hash

from .config_normalize import _normalize_config_for_roundtrip
from .errors import raise_failure
from .validation import validate_identifier_not_empty

logger = logging.getLogger(__name__)


def _missing_config(domain: str, identifier: str, available: list[str]) -> NoReturn:
    raise_failure(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"{domain.capitalize()} not found: {identifier}",
            context={f"{domain}_id": identifier, f"available_{domain}_ids": available},
            suggestions=[
                f"Use ha_search(domain_filter='{domain}') to find existing {domain}s"
            ],
        )
    )


class ConfigReadTools(BackendTools):
    async def _fetch_category(
        self, entity_id: str, scope: str, warnings: list[str]
    ) -> str | None:
        result = await self.backend.execute(
            "entities.category.read",
            {"entity_id": entity_id, "scope": scope},
            self.context,
        )
        warnings.extend(result["warnings"])
        category: str | None = result["category"]
        return category

    def _map_read_error(self, error: Exception, identifier: str) -> NoReturn:
        raise_failure(create_error_response(ErrorCode.INTERNAL_ERROR, str(error)))


class AutomationReadTools(ConfigReadTools):
    async def _get_automation_config_internal(
        self, identifier: str
    ) -> tuple[dict[str, Any], str]:
        result = await self.backend.execute(
            "config.automation.read", {"identifier": identifier}, self.context
        )
        if not result["found"]:
            _missing_config("automation", identifier, result["available_ids"])
        config = _normalize_config_for_roundtrip(result["config"])
        return config, compute_config_hash(config)

    async def _resolve_automation_entity_id(
        self, identifier: str, *, allow_component: bool = False
    ) -> str | None:
        entity_id: str | None = await self.backend.execute(
            "config.entity_id",
            {"domain": "automation", "identifier": identifier},
            self.context,
        )
        return entity_id

    async def ha_config_get_automation(
        self,
        identifier: Annotated[
            str,
            Field(
                description="Automation entity_id (e.g., 'automation.morning_routine') or unique_id"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Get Home Assistant automation configuration.

        Returns the complete configuration including triggers, conditions, actions, and mode settings.

        The returned `config_hash` stays the same across consecutive reads of an unchanged config.

        The returned `automation_id` is the resolved entity_id (canonical
        form, e.g. `automation.morning_routine`) when the registry lookup
        succeeds, falling back to the input `identifier` otherwise.

        EXAMPLES:
        - Get automation: ha_config_get_automation("automation.morning_routine")
        - Get by unique_id: ha_config_get_automation("my_unique_automation_id")

        For comprehensive automation documentation, use ha_get_skill_guide.
        """
        try:
            # Empty/whitespace identifier would propagate to the internal
            # config lookup and surface as a misleading
            # ``RESOURCE_NOT_FOUND``. Structured ``VALIDATION_INVALID_PARAMETER``
            # naming the parameter is cleaner — extension of the #1312
            # validate_identifier_not_empty pattern to the automations
            # family per #1313.
            validate_identifier_not_empty(
                identifier,
                "identifier",
                suggestions=[
                    "Pass an automation entity_id (e.g. 'automation.morning_routine')",
                    "Or pass the unique_id of an existing automation",
                    "Use ha_search(domain_filter='automation') to list automations",
                ],
            )

            # Read persisted content through its authoritative owner. Entity
            # raw_config remains stale until a reload finishes and is never used.
            return await self._read_automation(identifier)
        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001
            self._map_read_error(e, identifier)

    async def _read_automation(self, identifier: str) -> dict[str, Any]:
        """Build the existing response from a fresh persisted configuration read."""
        normalized_config, config_hash = await self._get_automation_config_internal(
            identifier
        )

        # Resolve entity_id and fetch category from entity registry
        # (injected after hash so transient registry failures don't affect the hash)
        entity_id = await self._resolve_automation_entity_id(identifier)
        cat_warnings: list[str] = []
        if entity_id:
            cat_id = await self._fetch_category(entity_id, "automation", cat_warnings)
            if cat_id:
                normalized_config["category"] = cat_id

        response: dict[str, Any] = {
            "success": True,
            "action": "get",
            "automation_id": entity_id or identifier,
            "config": normalized_config,
            "config_hash": config_hash,
        }
        if cat_warnings:
            response.setdefault("warnings", []).extend(cat_warnings)
        return response


class ScriptReadTools(ConfigReadTools):
    async def _fetch_script_config_envelope(self, script_id: str) -> dict[str, Any]:
        result = await self.backend.execute(
            "config.script.read", {"identifier": script_id}, self.context
        )
        if not result["found"]:
            _missing_config("script", script_id, result["available_ids"])
        return {
            "success": True,
            "script_id": result["storage_key"],
            "config": result["config"],
        }

    async def ha_config_get_script(
        self,
        script_id: Annotated[
            str,
            Field(
                description="Script identifier — bare storage key ('morning_routine') or entity_id form ('script.morning_routine'); a leading 'script.' prefix is stripped before lookup."
            ),
        ],
    ) -> dict[str, Any]:
        """
        Get Home Assistant script configuration.

        Returns the complete configuration for a script, including sequence, mode, fields, and other settings.

        The returned `config_hash` stays the same across consecutive reads of an unchanged config.

        The returned `script_id` is the canonical bare storage key resolved by the REST client (matching what `ha_config_set_script` / `ha_config_remove_script` expect), falling back to the input identifier on the rare path where the REST envelope omits it. Prefix handling matches `ha_config_get_automation` in behavior (mechanism differs: automations resolve via state lookup; scripts strip the prefix).

        EXAMPLES:
        - Get script (bare form): ha_config_get_script("morning_routine")
        - Get script (entity_id form): ha_config_get_script("script.morning_routine")

        For detailed script configuration help, use ha_get_skill_guide.
        """
        try:
            # Strip BEFORE validate so a bare ``"script."`` (empty after
            # strip) is rejected as ``VALIDATION_INVALID_PARAMETER`` rather
            # than slipping through validate (non-empty pre-strip) and
            # 404-ing at ``get_script_config("")``. Accept entity_id form
            # (``script.foo``) and bare storage key (``foo``) — behavioral
            # parity with ``ha_config_get_automation`` (mechanism differs:
            # automations resolve via state lookup; scripts strip the
            # prefix). ``_raise_script_not_found`` suggests
            # ``ha_search(domain_filter='script')`` which returns
            # entity_ids; without this strip, feeding that output back into
            # the GET tool fails and reseeds the wrong-tool spiral that
            # #1297 closes.
            script_id = script_id.removeprefix("script.")
            # Empty/whitespace script_id would propagate to
            # ``get_script_config`` and surface as a misleading
            # ``RESOURCE_NOT_FOUND``. Extension of the #1312
            # validate_identifier_not_empty pattern to the scripts
            # family per #1313.
            validate_identifier_not_empty(
                script_id,
                "script_id",
                suggestions=[
                    "Pass a script identifier (e.g. 'morning_routine')",
                    "Use ha_search(domain_filter='script') to list scripts",
                ],
            )

            # Read persisted content through its authoritative owner. Entity
            # raw_config remains stale until a reload finishes and is never used.
            return await self._read_script(script_id)
        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001
            self._map_read_error(e, script_id)

    async def _read_script(self, script_id: str) -> dict[str, Any]:
        """Build the existing response from a fresh persisted configuration read."""
        config_result = await self._fetch_script_config_envelope(script_id)
        # Extract actual script config body and compute hash before category injection
        actual_config = config_result.get("config", config_result)
        config_hash_value = compute_config_hash(actual_config)

        # Fetch category from entity registry (best-effort)
        # (injected after hash so transient registry failures don't affect the hash)
        entity_id = f"script.{script_id}"
        cat_warnings: list[str] = []
        cat_id = await self._fetch_category(entity_id, "script", cat_warnings)
        if cat_id:
            actual_config["category"] = cat_id

        # Issue #1334: return the canonical storage key from the
        # rest_client envelope so callers can thread the result into
        # subsequent ha_config_*_script calls without re-resolving.
        # Falls back to the input when the rest_client response omits
        # the key — a contract violation that we surface via warning
        # rather than mask silently.
        canonical_id = config_result.get("script_id")
        if canonical_id is None:
            logger.warning(
                "get_script_config envelope missing 'script_id' for "
                "input %r; falling back to caller input. This indicates "
                "a rest_client contract violation.",
                script_id,
            )
            canonical_id = script_id

        response: dict[str, Any] = {
            "success": True,
            "action": "get",
            "script_id": canonical_id,
            # The script BODY, not the REST envelope. Returning the envelope
            # here put ``{success, script_id, config}`` under ``config``, so
            # ``response["config"]["sequence"]`` did not exist and every caller
            # had to unwrap twice -- the e2e carried a helper to do exactly
            # that. ``ha_config_get_automation`` returns the body, this
            # docstring promises the body, and the hash is already computed
            # over the body.
            "config": actual_config,
            "config_hash": config_hash_value,
        }
        # Top level, not inside ``config``: warnings are a top-level list[str]
        # by contract.
        if cat_warnings:
            response.setdefault("warnings", []).extend(cat_warnings)
        return response


class SceneReadTools(ConfigReadTools):
    async def _fetch_scene_config_envelope(self, scene_id: str) -> dict[str, Any]:
        from .config_read_errors import raise_scene_config_missing

        result = await self.backend.execute(
            "config.scene.read", {"identifier": scene_id}, self.context
        )
        if result["found"]:
            return {
                "success": True,
                "scene_id": result["storage_key"],
                "config": result["config"],
            }
        return raise_scene_config_missing(scene_id, result)

    async def _resolve_scene_entity_id(
        self, scene_id: str, *, allow_component: bool = False
    ) -> str:
        entity_id: str = await self.backend.execute(
            "config.entity_id",
            {"domain": "scene", "identifier": scene_id},
            self.context,
        )
        return entity_id

    async def _discover_scenes(
        self, *, query: str | None, search_in_config: bool, limit: int, offset: int
    ) -> dict[str, Any]:
        from .scene_discovery import discover_scenes

        return await discover_scenes(
            self.backend,
            self.context,
            query=query,
            search_in_config=search_in_config,
            limit=limit,
            offset=offset,
        )

    def _map_scene_error(self, error: Exception, scene_id: str | None) -> NoReturn:
        raise_failure(
            create_error_response(
                ErrorCode.INTERNAL_ERROR, str(error), context={"scene_id": scene_id}
            )
        )

    async def ha_config_get_scene(
        self,
        scene_id: Annotated[
            str | None,
            Field(
                description="Scene storage ID from listing (e.g. 'movie_night') "
                "or the scene's entity_id; an entity_id is resolved to the "
                "storage ID."
            ),
        ] = None,
        query: Annotated[
            str | None, Field(description="Filter scene names or IDs")
        ] = None,
        search_in_config: Annotated[
            bool,
            Field(
                description="Also search full stored scene attribute values within a bounded scan"
            ),
        ] = False,
        limit: Annotated[
            int, Field(description="Maximum scenes per page", ge=1, le=100)
        ] = 20,
        offset: Annotated[int, Field(description="Pagination offset", ge=0)] = 0,
    ) -> dict[str, Any]:
        """Get a scene's complete configuration, or list and search scenes without scene_id.

        Use ha_search for cross-domain discovery and dependency searches. For ordinary
        scene discovery, use this tool and pass a returned scene_id back to retrieve
        the complete entities dict and config_hash for editing (pass that hash to
        ha_config_set_scene for python_transform updates).

        Listing returns compact metadata. Integration-managed scenes have no editable
        storage config or scene_id. Partial content-search results explicitly
        report unread configs and are not exhaustive.

        EXAMPLES:
        - Get scene: ha_config_get_scene("movie_night")
        - Find scenes: ha_config_get_scene(query="movie")
        - Find attribute values: ha_config_get_scene(query="rainbow", search_in_config=True)

        For detailed scene configuration help, use ha_get_skill_guide.
        """
        try:
            if scene_id is None:
                return await self._discover_scenes(
                    query=query,
                    search_in_config=search_in_config,
                    limit=limit,
                    offset=offset,
                )
            # Issue #1168 R6 blocker 16: empty ``scene_id`` previously
            # surfaced as ``RESOURCE_NOT_FOUND`` with a misleading
            # `entities`-related suggestion. Pre-flight here so the caller
            # gets the actual problem. Migrated to the shared
            # ``validate_identifier_not_empty`` helper (#1314) — message
            # and ``context["scene_id"]`` key preserved for callers.
            validate_identifier_not_empty(
                scene_id,
                "scene_id",
                message="scene_id must not be empty",
                suggestions=[
                    "Pass a non-empty scene identifier (e.g. 'movie_night')",
                    "Call ha_config_get_scene() without scene_id to discover scenes",
                ],
                context={"scene_id": scene_id},
            )
            # Persisted content is read from Core's canonical config owner,
            # never from runtime SceneConfig.states or a reloaded raw_config.
            return await self._read_scene(scene_id)
        except ToolFailure:
            raise
        except Exception as error:  # noqa: BLE001 - facade-specific error classification
            self._map_scene_error(error, scene_id)

    async def _read_scene(self, scene_id: str) -> dict[str, Any]:
        """Build the scene response from its fresh body and canonical storage ID."""
        # Issue #1168 R3 blockers 3 + 6: unwrap the rest-client envelope
        # so the response carries the scene body directly (no nested
        # `success`/`scene_id`/`config` chain), and use the storage key
        # consistently for `scene_id` regardless of whether the caller
        # passed the entity_id slug or the storage key.
        envelope = await self._fetch_scene_config_envelope(scene_id)
        actual_config = envelope.get("config", envelope)
        resolved_id = envelope.get("scene_id", scene_id)
        config_hash_value = compute_config_hash(actual_config)

        # Resolve real entity_id via registry — see _resolve_scene_entity_id
        # for the reasoning. Category fetch on the wrong entity_id is a
        # silent no-op, masking real category assignments.
        entity_id = await self._resolve_scene_entity_id(resolved_id)
        cat_warnings: list[str] = []
        cat_id = await self._fetch_category(entity_id, "scene", cat_warnings)

        response: dict[str, Any] = {
            "success": True,
            "action": "get",
            "scene_id": resolved_id,
            "config": actual_config,
            "config_hash": config_hash_value,
        }
        if cat_id:
            response["category"] = cat_id
        if cat_warnings:
            response.setdefault("warnings", []).extend(cat_warnings)
        return response
