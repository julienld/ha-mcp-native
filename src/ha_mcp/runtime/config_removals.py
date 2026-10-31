"""Config deletions share validation, canonical targets and confirmation policy."""

import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import BackendTools, ToolFailure

from .config_read_errors import raise_scene_config_missing
from .config_reads import (
    AutomationReadTools,
    SceneReadTools,
    ScriptReadTools,
    _missing_config,
)
from .errors import raise_failure
from .validation import validate_identifier_not_empty

logger = logging.getLogger(__name__)


class ConfigRemovalTools(BackendTools):
    async def _map_remove_error(self, error: Exception, identifier: str) -> NoReturn:
        raise_failure(
            create_error_response(
                ErrorCode.INTERNAL_ERROR, str(error), context={"identifier": identifier}
            )
        )

    async def _delete_stored_config(
        self, domain: str, identifier: str, *, resolution: Any = None
    ) -> dict[str, Any]:
        arguments = {"identifier": identifier}
        if resolution is not None:
            arguments["resolution"] = resolution
        current = await self.backend.execute(
            f"config.{domain}.read", arguments, self.context
        )
        if not current["found"]:
            if domain == "scene":
                raise_scene_config_missing(identifier, current)
            _missing_config(domain, identifier, current.get("available_ids", []))
        storage_key = (
            current.get("storage_key") or current["config"].get("id") or identifier
        )
        entity_id = (
            await self.backend.execute(
                "config.entity_id",
                {"domain": domain, "identifier": storage_key},
                self.context,
            )
            if domain == "script"
            else None
        )
        backup = await self.backend.execute(
            "config.backup.capture",
            {
                "domain": domain,
                "source_tool": f"ha_config_remove_{domain}",
                "backup_id": storage_key,
                "current_config": current["config"],
            },
            self.context,
        )
        result: dict[str, Any] = await self.backend.execute(
            f"config.{domain}.delete",
            {
                "storage_key": storage_key,
                "identifier": identifier,
                "resolution": resolution,
                "expected_config": current["config"],
            },
            self.context,
        )
        if domain == "automation":
            result.pop("automation_id", None)
            result["unique_id"] = storage_key
        if entity_id is not None:
            result["_verification_entity_id"] = entity_id
        if backup and backup.get("warnings"):
            result.setdefault("warnings", []).extend(backup["warnings"])
        return result

    async def _verify_removed(self, entity_id: str, result: dict[str, Any]) -> None:
        try:
            removed = await self.backend.execute(
                "entities.removal.wait", {"entity_id": entity_id}, self.context
            )
        except Exception as error:  # noqa: BLE001 - cancellation propagates; deletion acknowledged
            result.setdefault("warnings", []).append(
                f"Deletion confirmed but removal verification failed: {error}"
            )
            return
        if not removed:
            result.setdefault("warnings", []).append(
                f"Deletion confirmed but {entity_id} is still present after the wait window."
            )


class AutomationRemovalTools(AutomationReadTools, ConfigRemovalTools):
    async def _delete_automation(self, identifier: str) -> dict[str, Any]:
        return await self._delete_stored_config("automation", identifier)

    async def ha_config_remove_automation(
        self,
        identifier: Annotated[
            str,
            Field(
                description="Automation entity_id (e.g., 'automation.old_automation') or unique_id to delete"
            ),
        ],
        wait: Annotated[
            bool,
            Field(
                description="Wait for automation to be fully removed before returning.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Delete a Home Assistant automation permanently.

        The returned `automation_id` is the resolved entity_id (canonical
        form, e.g. `automation.morning_routine`) when the registry lookup
        succeeded before the delete, falling back to the input
        `identifier` otherwise.

        EXAMPLES:
        - Delete automation: ha_config_remove_automation("automation.old_automation")
        - Delete by unique_id: ha_config_remove_automation("my_unique_id")
        """
        try:
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                identifier,
                "identifier",
                suggestions=[
                    "Use ha_search(domain_filter='automation') to find existing automations"
                ],
                context={"operation": "remove_automation"},
            )
            # Resolve entity_id for wait verification (identifier may be a unique_id)
            entity_id_for_wait = await self._resolve_automation_entity_id(
                identifier, allow_component=True
            )
            if not entity_id_for_wait:
                logger.warning(
                    f"Could not resolve unique_id '{identifier}' to entity_id -- wait verification will be skipped"
                )

            result = await self._delete_automation(identifier)

            # Wait for entity to be removed
            if wait and entity_id_for_wait:
                await self._verify_removed(entity_id_for_wait, result)

            return {
                "success": True,
                "action": "delete",
                "automation_id": (
                    entity_id_for_wait or identifier or result.get("unique_id")
                ),
                **{key: value for key, value in result.items() if key != "identifier"},
            }
        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001 - facade-specific error classification
            await self._map_remove_error(e, identifier)


class ScriptRemovalTools(ScriptReadTools, ConfigRemovalTools):
    async def _delete_script(self, script_id: str) -> dict[str, Any]:
        return await self._delete_stored_config("script", script_id)

    async def ha_config_remove_script(
        self,
        script_id: Annotated[
            str,
            Field(
                description="Script identifier to delete — bare storage key ('old_script') or entity_id form ('script.old_script'); a leading 'script.' prefix is stripped before lookup."
            ),
        ],
        wait: Annotated[
            bool,
            Field(
                description="Wait for script to be fully removed before returning.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Delete a Home Assistant script.

        EXAMPLE: ha_config_remove_script("old_script")

        Only scripts created via the Home Assistant UI can be deleted. Scripts
        defined in YAML configuration files (scripts.yaml or configuration.yaml)
        cannot be deleted through the API and return a 405 Method Not Allowed
        error; edit the configuration file directly instead.

        WARNING: Deleting a script that is used by automations may cause those automations to fail.
        """
        try:
            # Strip BEFORE validate so a bare ``"script."`` (empty after
            # strip) is rejected as ``VALIDATION_INVALID_PARAMETER`` rather
            # than slipping through validate (non-empty pre-strip) and
            # producing a ``script.script.foo`` entity_id for the
            # ``verify_entity_removed`` check below — that mis-formed
            # entity_id 404s, which counts as removed, so the real entity
            # would never be checked. Behavioral parity with
            # ``ha_config_get_script``.
            script_id = script_id.removeprefix("script.")
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                script_id,
                "script_id",
                suggestions=[
                    "Use ha_search(domain_filter='script') to find existing script_ids"
                ],
                context={"operation": "remove_script"},
            )
            result = await self._delete_script(script_id)

            # Wait for script to be removed
            entity_id = result.pop("_verification_entity_id", f"script.{script_id}")
            if wait:
                await self._verify_removed(entity_id, result)

            return {"success": True, "action": "delete", **result}
        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001 - facade-specific error classification
            await self._map_remove_error(e, script_id)


class SceneRemovalTools(SceneReadTools, ConfigRemovalTools):
    async def _resolve_scene_config(self, scene_id: str) -> Any:
        return await self.backend.execute(
            "config.scene.resolve", {"identifier": scene_id}, self.context
        )

    async def _delete_scene(self, scene_id: str, *, resolution: Any) -> dict[str, Any]:
        return await self._delete_stored_config(
            "scene", scene_id, resolution=resolution
        )

    async def ha_config_remove_scene(
        self,
        scene_id: Annotated[
            str, Field(description="Scene identifier to delete (e.g., 'old_scene')")
        ],
        wait: Annotated[
            bool,
            Field(
                description="Wait for scene to be fully removed before returning.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Delete a Home Assistant scene.

        EXAMPLE: ha_config_remove_scene("old_scene")

        Only scenes created via the Home Assistant UI can be deleted. Scenes
        defined in YAML configuration files (scenes.yaml or configuration.yaml)
        cannot be deleted through the API and return a 405 Method Not Allowed
        error; edit the configuration file directly instead.

        WARNING: Deleting a scene that is referenced by automations or scripts
        (via ``scene.turn_on``) may cause those to fail.
        """
        try:
            # Issue #1168 R6 blocker 16: empty ``scene_id`` pre-flight before
            # the resolver — keeps the error code/message aligned with the
            # actual problem. Migrated to the shared
            # ``validate_identifier_not_empty`` helper (#1314) — message
            # and ``context["scene_id"]`` key preserved for callers.
            validate_identifier_not_empty(
                scene_id,
                "scene_id",
                message="scene_id must not be empty",
                suggestions=[
                    "Pass a non-empty scene identifier (e.g. 'old_scene')",
                    "Use ha_search(domain_filter='scene') to find existing scene_ids",
                ],
                context={"scene_id": scene_id},
            )
            # Issue #1168 R3 blocker 6: resolve once up-front so every later
            # callsite (entity_id resolver, delete call, response) uses the
            # storage key consistently — outer ``scene_id`` matches the
            # inner body regardless of whether the caller passed the
            # entity_id slug or the storage key. The rich resolution also
            # carries ``registry_hit``/``platform`` so a config-API 404 on a
            # non-storage scene (Hue/vendor, raw-YAML) classifies as
            # CONFIG_NOT_FOUND rather than the misleading generic 404 (#1971).
            resolution = await self._resolve_scene_config(scene_id)
            resolved_id = (
                resolution["storage_key"]
                if isinstance(resolution, dict)
                else resolution.storage_key
            )

            # Resolve actual entity_id BEFORE delete — once the registry
            # entry is gone, the unique_id lookup can no longer find it.
            # Falls back to f"scene.{resolved_id}" if the registry has no
            # matching unique_id (e.g. scene_id-as-entity-id slug case).
            entity_id = await self._resolve_scene_entity_id(
                resolved_id, allow_component=True
            )

            # Thread the resolution through so delete_scene_config reuses the
            # storage key (no redundant re-resolve) and can classify a 404.
            result = await self._delete_scene(scene_id, resolution=resolution)

            if wait:
                await self._verify_removed(entity_id, result)

            return {
                "success": True,
                "action": "delete",
                **result,
                "scene_id": resolved_id,
            }
        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001 - facade-specific error classification
            await self._map_remove_error(e, scene_id)
