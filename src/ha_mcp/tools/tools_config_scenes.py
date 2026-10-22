"""
Configuration management tools for Home Assistant scenes.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant scene configurations. Mirrors the scripts/automations
patterns; the key shape difference is that scene ``entities`` is a dict
keyed by entity_id (not a list).
"""

import asyncio
import logging
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors, legacy_errors_sync
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
    SceneStorageConfigNotFoundError,
)
from ..errors import (
    create_entity_not_found_error,
)
from ..runtime.config_reads import SceneReadTools
from ..runtime.config_removals import SceneRemovalTools
from ..runtime.errors import raise_classified
from ..runtime.scene_config import SceneConfigTools
from ..runtime.scene_writes import SceneWriteTools
from ..runtime.scene_writes import _activate_once_reloaded as _activate_once_reloaded
from ..tool_api import ToolFailure
from ..utils.python_sandbox import (
    safe_execute,
)
from .auto_backup import with_auto_backup
from .component_config_reads import fetch_entity_lookup_via_component
from .config_helpers.registry import validate_registry_ids
from .config_write_helpers import (
    apply_entity_category,
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
    config_reload_waiter,
    fetch_entity_category,
)
from .entity_registration import resolve_entity_id_after_write
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .reference_validator import validate_config_references
from .scene_discovery import discover_scenes
from .tool_hints import read_only_hints, write_hints
from .ws_waiters import verify_entity_removed, wait_for_entity_registered

# No scene-specific reference file exists in home-assistant-best-practices;
# SKILL.md is the top-level generic best-practice doc covering entity-naming,
# safe-refactoring, and helper-vs-template trade-offs the agent benefits
# from when authoring scenes. Scenes have NO actions/conditions/triggers
# (only an ``entities`` state-snapshot dict) so the automation/script
# reference files don't apply.
_SCENE_SKILL_FILES: tuple[str, ...] = ("SKILL.md",)

logger = logging.getLogger(__name__)


def _raise_scene_not_storage_error(scene_id: str, platform: str | None) -> NoReturn:
    from ..runtime.config_read_errors import _raise_scene_not_storage_error as fail

    try:
        fail(scene_id, platform)
    except ToolFailure as error:
        raise_tool_error(error.data)


def _skip_scene_activation_backup(kwargs: dict[str, Any]) -> bool:
    """Skip config snapshots for a standalone activation (no config change)."""
    return (
        bool(kwargs.get("activate"))
        and kwargs.get("config") is None
        and kwargs.get("python_transform") is None
    )


class ConfigSceneTools(SceneWriteTools):
    """Scene configuration management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _fetch_category(
        self, entity_id: str, scope: str, warnings: list[str]
    ) -> str | None:
        return await fetch_entity_category(self._client, entity_id, scope, warnings)

    # Short retry retained for config reads and removals; post-write polling
    # has its own deadline in entity_registration.
    _RESOLVE_RETRY_DELAY = 0.2

    @staticmethod
    def _first_scene_entity_id(matches: list[dict[str, Any]]) -> str | None:
        """First ``scene.``-domain ``entity_id`` in a component match list, else None."""
        for match in matches:
            entity_id = match.get("entity_id") or ""
            if entity_id.startswith("scene."):
                return entity_id
        return None

    async def _resolve_scene_entity_id(
        self, scene_id: str, *, allow_component: bool = False
    ) -> str:
        """Resolve the entity ID for config reads and removals, with one retry.

        HA derives scene entity IDs from the name; the storage key remains
        the registry unique_id. Match that key instead of assuming the slug.
        Accept either a bare key or a key prefixed with ``scene.``.

        Only removals enable ``allow_component``. Config reads use the legacy
        registry path to preserve the GET-path invariant documented in
        ``component_config_reads``. An empty component result is rechecked
        once; component unavailability falls through to the legacy path.

        Post-write waits and categories use ``resolve_entity_id_after_write``:
        their registration window requires deadline-based polling (#2426).
        """
        scene_id = scene_id.removeprefix("scene.")
        if allow_component:
            resolved = await self._resolve_scene_via_component(scene_id)
            if resolved is not None:
                return resolved
        return await self._resolve_scene_via_registry(scene_id)

    async def _resolve_scene_via_component(self, scene_id: str) -> str | None:
        """Resolve a scene ``entity_id`` through the in-process component.

        Returns the resolved ``entity_id`` (or the naive ``scene.{scene_id}``
        guess when the component authoritatively reports the scene still absent
        after a settle). Returns ``None`` when the component is unavailable or
        non-authoritative (capability miss, or the recheck went
        unavailable/errored mid-retry), so the caller falls through to the
        legacy registry list+retry path. ``scene_id`` must already have its
        ``scene.`` prefix stripped.
        """
        matches = await fetch_entity_lookup_via_component(
            self._client, scene_id, domain="scene"
        )
        if matches is None:
            return None
        # Component answered authoritatively. A hit returns at once — the
        # in-process registry read needs no settle. An EMPTY result right
        # after an upsert can be HA's async entity-registration lag
        # (#1168), not a true absence, so recheck ONCE after the same
        # short delay the legacy path uses before the naive fallback.
        entity_id = self._first_scene_entity_id(matches)
        if entity_id is not None:
            return entity_id
        await asyncio.sleep(self._RESOLVE_RETRY_DELAY)
        recheck = await fetch_entity_lookup_via_component(
            self._client, scene_id, domain="scene"
        )
        if recheck is None:
            # recheck is None: the component went unavailable/errored/downgraded
            # mid-retry, so the first empty read is NOT authoritative. Fall
            # through to the legacy list+retry path (its own retry absorbs the
            # same registration lag) instead of guessing.
            return None
        # Authoritative recheck: a hit is returned; a genuine empty
        # (still absent after the settle) falls to the naive guess.
        entity_id = self._first_scene_entity_id(recheck)
        if entity_id is not None:
            return entity_id
        return f"scene.{scene_id}"

    async def _resolve_scene_via_registry(self, scene_id: str) -> str:
        """Resolve a scene ``entity_id`` via the entity-registry list.

        Legacy path used by ``_resolve_scene_entity_id`` when the component is
        unavailable or non-authoritative: lists the registry with one
        registration-lag retry, falling back to the naive ``scene.{scene_id}``.
        ``scene_id`` must already have its ``scene.`` prefix stripped.
        """
        retried = False
        for attempt in range(2):
            try:
                result = await self._client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                )
                if result.get("success") is not False:
                    for entry in result.get("result") or []:
                        entity_id = entry.get("entity_id") or ""
                        if entry.get("unique_id") == scene_id and entity_id.startswith(
                            "scene."
                        ):
                            return entity_id
            except (TimeoutError, HomeAssistantAPIError, HomeAssistantConnectionError):
                # Programming bugs (AttributeError, KeyError, …) propagate; only
                # genuine HA-API failures fall through to the naive form so the
                # caller still gets a best-effort entity_id rather than a 500.
                logger.warning(
                    f"Entity registry resolve failed for scene_id={scene_id}, "
                    f"falling back to scene.{scene_id}",
                    exc_info=True,
                )
                # API failure is sticky — retrying won't change a 500 / auth
                # error. Bail to the naive form immediately.
                break
            if attempt == 0:
                # Registry list succeeded but no row matched. On a freshly-
                # upserted scene the registry index lags the storage write —
                # one short retry catches the common case before falling
                # back to the naive entity_id.
                await asyncio.sleep(self._RESOLVE_RETRY_DELAY)
                retried = True
        # Issue #1168 R5 blocker 13 (refined per R6 blocker 19): only log on
        # the genuine exhausted-retry exit (both list calls succeeded, neither
        # matched). The first-pass-success path also falls through here on the
        # legitimate "registry caught up before retry" case, but that path
        # never sets ``retried``; gating on it stops the phantom-noise.
        if retried:
            logger.debug(
                f"_resolve_scene_entity_id: registry retry exhausted for "
                f"scene_id={scene_id!r}, falling back to scene.{scene_id}"
            )
        return f"scene.{scene_id}"

    async def _validate_category_id(self, category: str) -> None:
        """Confirm ``category`` exists in the ``scene`` category registry.

        Issue #1168 R5 blocker 9: ``apply_entity_category`` forwards the
        supplied ID blindly to the entity-registry update; HA accepts a
        non-existent category ID without complaint, leaving the registry
        with a phantom reference invisible in
        ``ha_config_get_category(scope='scene')`` results. Pre-validating
        the ID against the live category registry is the only place the
        phantom can be caught without changing HA itself. Issue #2159 moved
        the check onto the shared cross-registry validator so every write
        tool rejects dangling references identically.
        """
        await validate_registry_ids(
            self._client, None, None, {"scene": category}, fail_closed=True
        )

    ha_config_get_scene = tool(
        name="ha_config_get_scene",
        tags={"Scenes"},
        annotations=read_only_hints("Get or Find Scenes", open_world=False),
    )(log_tool_usage(legacy_errors(SceneReadTools.ha_config_get_scene)))

    async def _fetch_scene_config_envelope(self, scene_id: str) -> dict[str, Any]:
        config: dict[str, Any] = await self._client.get_scene_config(scene_id)
        return config

    async def _discover_scenes(
        self, *, query: str | None, search_in_config: bool, limit: int, offset: int
    ) -> dict[str, Any]:
        return await discover_scenes(
            self._client,
            query=query,
            search_in_config=search_in_config,
            limit=limit,
            offset=offset,
        )

    def _map_scene_error(self, error: Exception, scene_id: str | None) -> NoReturn:
        if isinstance(error, ToolError):
            raise error
        if isinstance(error, SceneStorageConfigNotFoundError):
            _raise_scene_not_storage_error(error.scene_id, error.platform)
        exception_to_structured_error(
            error,
            context={
                "scene_id": scene_id,
                **(
                    {"entity_id": f"scene.{scene_id.removeprefix('scene.')}"}
                    if scene_id is not None
                    else {}
                ),
            },
            suggestions=[
                "Call ha_config_get_scene() without scene_id to discover scenes",
                "Check Home Assistant connection",
                "Use ha_get_skill_guide for help",
            ],
        )

    async def _read_scene_envelope(
        self, scene_id: str, *, _resolved: bool = False, resolution: Any = None
    ) -> dict[str, Any]:
        if resolution is not None:
            if isinstance(resolution, dict):
                from ..client.rest_client import SceneResolution

                resolution = SceneResolution(**resolution)
            return await self._client.get_scene_config(scene_id, resolution=resolution)
        return await self._client.get_scene_config(scene_id, _resolved=_resolved)

    _legacy_get_scene = SceneReadTools._read_scene
    _read_scene = SceneReadTools._read_scene

    _get_scene_config_internal = legacy_errors(
        SceneConfigTools._get_scene_config_internal, classify=False
    )

    _fetch_and_verify_hash = legacy_errors(
        SceneConfigTools._fetch_and_verify_hash, classify=False
    )

    _validate_scene_config = staticmethod(
        legacy_errors_sync(SceneConfigTools._validate_scene_config)
    )

    ha_config_set_scene = tool(
        name="ha_config_set_scene",
        tags={"Scenes"},
        annotations=write_hints(
            "Create or Update Scene",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(
        with_auto_backup(
            domain="scene", id_param="scene_id", skip_fn=_skip_scene_activation_backup
        )(log_tool_usage(legacy_errors(SceneWriteTools.ha_config_set_scene)))
    )

    _validate_transformed_scene = staticmethod(
        legacy_errors_sync(SceneConfigTools._validate_transformed_scene)
    )

    _prune_scene_metadata = staticmethod(
        legacy_errors_sync(SceneConfigTools._prune_scene_metadata)
    )

    async def _transform_config(self, expression: str, config: dict[str, Any]) -> Any:
        return safe_execute(expression, config)

    async def attach_skill_content(
        self,
        response: dict[str, Any],
        MandatoryBPS: bool,
        canonical_files: tuple[str, ...],
        referenced_files: set[str] | None,
    ) -> None:
        attach_skill_content(response, MandatoryBPS, canonical_files, referenced_files)

    async def _augment_error_dict(
        self, error: dict[str, Any], bp_warnings: Any
    ) -> None:
        augment_error_dict_with_skill_content(error, bp_warnings)

    async def _validate_references(self, config: dict[str, Any]) -> dict[str, Any]:
        return await validate_config_references(self._client, config)

    async def _resolve_written_entity(
        self, storage_key: str, domain: str, **options: Any
    ) -> str:
        return await resolve_entity_id_after_write(
            self._client, storage_key, domain, **options
        )

    async def _wait_registered(self, entity_id: str) -> bool:
        try:
            return await wait_for_entity_registered(self._client, entity_id)
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as error:
            raise ToolFailure("VERIFICATION_FAILED", str(error)) from error

    async def _apply_category(
        self,
        entity_id: str,
        category: str,
        scope: str,
        result: dict[str, Any],
        entity_type: str,
    ) -> None:
        await apply_entity_category(
            self._client, entity_id, category, scope, result, entity_type
        )

    async def _scene_state_exists(self, key: str) -> bool:
        return await self._client._scene_state_exists(key)

    async def _ensure_scene_exists(self, entity_id: str) -> None:
        try:
            await self._client.get_entity_state(entity_id)
        except HomeAssistantAPIError as error:
            if error.status_code == 404:
                raise_tool_error(create_entity_not_found_error(entity_id))
            raise

    def _config_reload_waiter(self, *, enabled: bool) -> Any:
        return config_reload_waiter(self._client, "scene_reloaded", enabled=enabled)

    async def _scene_runtime_service(self, entity_id: str) -> None:
        try:
            await self._client.call_service(
                "scene", "turn_on", {"entity_id": entity_id}
            )
        except (
            HomeAssistantAPIError,
            HomeAssistantAuthError,
            HomeAssistantConnectionError,
        ) as error:
            raise_classified(
                error, context={"action": "activate", "entity_id": entity_id}
            )

    async def _upsert_scene(
        self, config: dict[str, Any], scene_id: str, *, resolved_id: str | None
    ) -> dict[str, Any]:
        return await self._client.upsert_scene_config(
            config, scene_id, resolved_id=resolved_id
        )

    async def _raise_scene_write_error(self, e: Exception, scene_id: str) -> NoReturn:
        if isinstance(e, ToolError):
            raise augment_tool_error_with_skill_content(e, bp_warnings=None) from None
        if isinstance(e, SceneStorageConfigNotFoundError):
            _raise_scene_not_storage_error(e.scene_id, e.platform)
        error = exception_to_structured_error(
            e,
            context={"scene_id": scene_id},
            suggestions=[
                "Ensure config includes an 'entities' field (a dict keyed by entity_id)",
                "Scene shape: {'entities': {'light.kitchen': {'state': 'on'}}}",
                "Use ha_search(domain_filter='scene') to find scenes",
                "Use ha_get_skill_guide for help",
            ],
            raise_error=False,
        )
        augment_error_dict_with_skill_content(error, bp_warnings=None)
        raise_tool_error(error)

    ha_config_remove_scene = tool(
        name="ha_config_remove_scene",
        tags={"Scenes"},
        annotations=write_hints(
            "Remove Scene", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(domain="scene", id_param="scene_id")(
            log_tool_usage(legacy_errors(SceneRemovalTools.ha_config_remove_scene))
        )
    )

    async def _resolve_scene_config(self, scene_id: str) -> Any:
        value = await self._client._resolve_scene(scene_id)
        return {
            "storage_key": value.storage_key,
            "registry_hit": value.registry_hit,
            "platform": value.platform,
        }

    async def _delete_scene(self, scene_id: str, *, resolution: Any) -> dict[str, Any]:
        if isinstance(resolution, dict):
            from ..client.rest_client import SceneResolution

            resolution = SceneResolution(**resolution)
        return await self._client.delete_scene_config(scene_id, resolution=resolution)

    async def _verify_removed(self, entity_id: str, result: dict[str, Any]) -> None:
        await verify_entity_removed(self._client, entity_id, result)

    async def _map_remove_error(self, e: Exception, scene_id: str) -> NoReturn:
        if isinstance(e, ToolError):
            raise e
        if isinstance(e, SceneStorageConfigNotFoundError):
            _raise_scene_not_storage_error(e.scene_id, e.platform)
        exception_to_structured_error(
            e,
            context={"scene_id": scene_id},
            suggestions=[
                "Verify scene_id exists using ha_search(domain_filter='scene')",
                "Check if scene is being used by automations or scripts",
                "Use ha_get_skill_guide for help",
            ],
        )


def register_config_scene_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant scene configuration tools."""
    register_tool_methods(mcp, ConfigSceneTools(client))
