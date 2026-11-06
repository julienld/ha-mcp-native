"""Finite read, category and skill operations used by config write families."""

import copy
from typing import Any, Literal

from ha_mcp.tool_api import BackendTools, ToolFailure
from ha_mcp.utils.config_hash import json_values_equal

from .actions import ServiceCallTools
from .blueprint_substitute import rendered_blueprint
from .config_reference_rules import (
    _TARGET_ID_KEYS,
    build_entity_set,
    build_service_index,
    check_refs,
    extract_refs,
)
from .registry_references import validate_registry_ids
from .skill_content import apply_skill_content

_WRITE_TOOL_BP_HINT_SUGGESTION = (
    "For Home Assistant best-practice guidance, call "
    "ha_get_skill_guide() to read SKILL.md, then the reference file its "
    "table points to, before retrying."
)


class ConfigWriteContext(BackendTools):
    async def _transform_config(self, expression: str, config: dict[str, Any]) -> Any:
        return await self.backend.execute(
            "config.transform",
            {"expression": expression, "config": config},
            self.context,
        )

    async def _upsert_config(
        self,
        domain: str,
        config: dict[str, Any],
        identifier: str,
        resolved_id: str | None,
        verified: tuple[dict[str, Any], str | None] | None,
    ) -> dict[str, Any]:
        """One fresh before-image and typed compare protect each config writer."""
        key, previous = await self._config_before_image(
            domain, identifier, resolved_id, verified
        )
        backup = await self._capture_config_backup(domain, key, previous)
        # Defaults remain caller-facing after registry renames, matching the
        # existing clients. Core's validators alone decide accepted fields.
        if domain == "script":
            config.setdefault("alias", identifier)
        elif domain == "scene":
            config.setdefault("name", identifier.removeprefix("scene."))
        result: dict[str, Any] = await self.backend.execute(
            f"config.{domain}.write",
            {
                "storage_key": key,
                "expected_config": previous,
                "data": config,
                "identifier": identifier,
            },
            self.context,
        )
        result.update({"success": True, f"{domain}_id": key})
        if domain == "script":
            result["operation"] = (
                "created" if result.get("result") == "ok" else "updated"
            )
        elif domain == "scene":
            # Core returns the same acknowledgment on create and update; the
            # scene client deliberately omits this misleading operation echo.
            result.pop("operation", None)
        if backup.get("warnings"):
            result.setdefault("warnings", []).extend(backup["warnings"])
        return result

    async def _config_before_image(
        self,
        domain: str,
        identifier: str,
        resolved_id: str | None,
        verified: tuple[dict[str, Any], str | None] | None,
        *,
        creating: bool = False,
    ) -> tuple[str, Any]:
        current = await self.backend.execute(
            f"config.{domain}.read", {"identifier": identifier}, self.context
        )
        key = current["storage_key"]
        if not isinstance(key, str) or not key.strip():
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "The resolved config storage key must not be empty.",
                status="not_started",
            )
        previous = current["config"] if current["found"] else None
        if domain == "scene" and not current["found"] and current.get("not_in_storage"):
            from .config_read_errors import raise_scene_config_missing

            raise_scene_config_missing(identifier, current)
        if verified is not None and (
            not json_values_equal(previous, verified[0])
            or (verified[1] is not None and key != verified[1])
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                f"{domain.capitalize()} modified since last read (conflict)",
                status="not_started",
            )
        if resolved_id is not None and key != resolved_id:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                f"The {domain} identifier now names another stored {domain}.",
                status="not_started",
            )
        if creating and previous is not None:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The allocated config ID already exists; nothing was written.",
                status="not_started",
            )
        return key, previous

    async def _capture_config_backup(
        self, domain: str, key: str, previous: Any
    ) -> dict[str, Any]:
        backup: dict[str, Any] = await self.backend.execute(
            "config.backup.capture",
            {
                "domain": domain,
                "source_tool": f"ha_config_set_{domain}",
                "backup_id": key if previous is not None else None,
                "current_config": previous,
            },
            self.context,
        )
        return backup

    async def _validate_categories(self, categories: dict[str, str | None]) -> None:
        await validate_registry_ids(self.backend, self.context, None, None, categories)

    async def attach_skill_content(
        self,
        response: dict[str, Any],
        MandatoryBPS: bool,
        canonical_files: tuple[str, ...],
        referenced_files: set[str] | None,
    ) -> None:
        wanted = set(canonical_files) if MandatoryBPS else set()
        wanted.update(referenced_files or ())
        resolved = await self.backend.execute(
            "skills.files", {"references": sorted(wanted)}, self.context
        )
        apply_skill_content(
            response,
            resolved["content"],
            enabled=resolved["enabled"],
            requested=bool(wanted),
            vendor_missing=resolved["vendor_missing"],
        )

    async def _augment_error_dict(
        self, error: dict[str, Any], bp_warnings: Any
    ) -> None:
        attach_error_guidance(error)
        referenced = getattr(bp_warnings, "referenced_files", None)
        if referenced:
            await self.attach_skill_content(error, False, (), referenced)

    async def _augment_failure(
        self, error: ToolFailure, bp_warnings: Any
    ) -> ToolFailure:
        data = copy.deepcopy(error.data)
        await self._augment_error_dict(data, bp_warnings)
        return ToolFailure(error.code, str(error), data=data, status=error.status)

    async def _render_blueprint(
        self, domain: str, path: str, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self.backend.execute(
            "blueprints.render",
            {"domain": domain, "path": path, "input": inputs},
            self.context,
        )
        return rendered_blueprint(domain, path, result)

    async def _validate_references(self, config: dict[str, Any]) -> dict[str, Any]:
        walked = extract_refs(config)
        output: dict[str, Any] = {
            "warnings": [],
            "unvalidated_templates": walked["unvalidated_templates"],
            "blueprint_skipped": walked["blueprint_skipped"],
        }
        if walked["blueprint_skipped"] or not walked["refs"]:
            return output
        try:
            data = await self.backend.execute(
                "config.references.snapshot", {"include_targets": True}, self.context
            )
        except ToolFailure as error:
            if error.code in {
                "AUTHENTICATION_FAILED",
                "AUTH_INVALID_TOKEN",
                "READ_ONLY",
                "PERMISSION_DENIED",
                "FORBIDDEN",
            }:
                raise
            output.update(
                references_unchecked=True,
                warnings=[f"Reference catalogs unavailable: {error}"],
            )
            return output
        targets = {
            kind: set(data[key])
            for kind, key in _TARGET_ID_KEYS.items()
            if isinstance(data.get(key), list)
        }
        sentinels = frozenset(data.get("match_sentinels") or [])
        output["warnings"] = check_refs(
            walked["refs"],
            build_service_index(data["services"]),
            build_entity_set([{"entity_id": eid} for eid in data["entity_ids"]]),
            targets,
            sentinels,
        )
        unchecked = sorted(
            {
                ref["kind"]
                for ref in walked["refs"]
                if ref["kind"] not in {"service", "entity"}
                and ref["kind"] not in targets
            }
        )
        if unchecked:
            output["unchecked_target_kinds"] = unchecked
        return output

    async def _resolve_written_entity(
        self, storage_key: str, domain: Literal["scene", "script"], **options: Any
    ) -> str:
        result: str = await self.backend.execute(
            "config.entity_id.after_write",
            {"storage_key": storage_key, "domain": domain, **options},
            self.context,
        )
        return result

    async def _wait_registered(self, entity_id: str) -> bool:
        result: bool = await self.backend.execute(
            "entities.registration.wait", {"entity_id": entity_id}, self.context
        )
        return result

    async def _apply_category(
        self,
        entity_id: str,
        category: str,
        scope: str,
        result: dict[str, Any],
        entity_type: str,
    ) -> None:
        try:
            applied = await self.backend.execute(
                "entities.category.write",
                {
                    "entity_id": entity_id,
                    "category": category,
                    "scope": scope,
                    "entity_type": entity_type,
                },
                self.context,
            )
        except (ToolFailure, PermissionError, TimeoutError, OSError) as error:
            # The config is already persisted. A denied/failed follow-up must
            # not describe the whole write as a rejection and invite a replay.
            result.setdefault("warnings", []).append(
                f"{entity_type.capitalize()} saved but failed to set category: {error}"
            )
            return
        result.update(
            {key: value for key, value in applied.items() if key != "warnings"}
        )
        if applied.get("warnings"):
            result.setdefault("warnings", []).extend(applied["warnings"])

    async def _runtime_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        response = await ServiceCallTools(self.backend, self.context).ha_call_service(
            domain=domain,
            service=service,
            entity_id=data.get("entity_id"),
            data=data,
            wait=False,
        )
        if response.get("success") is not True:
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                "The runtime service was not acknowledged.",
                data=response,
            )


def attach_error_guidance(error_dict: dict[str, Any]) -> None:
    """Idempotent suggestion policy shared with legacy skill error decoration."""
    error = error_dict.get("error")
    if not isinstance(error, dict):
        return
    suggestions = error.setdefault("suggestions", [])
    primary = error.get("suggestion")
    if not suggestions and isinstance(primary, str) and primary:
        suggestions.append(primary)
    if _WRITE_TOOL_BP_HINT_SUGGESTION not in suggestions:
        suggestions.append(_WRITE_TOOL_BP_HINT_SUGGESTION)
    if suggestions:
        error["suggestion"] = suggestions[0]


def merge_validation_meta(
    result: dict[str, Any], validation_meta: dict[str, Any]
) -> None:
    """Attach reference-validator output to a set-tool success ``result``.

    Produces a single nested ``validation`` field when there's anything
    worth reporting - warnings, skipped templates, a blueprint
    short-circuit, target kinds left unchecked, or a failed registry fetch.
    Keeps the happy-path response unchanged.

    Shared between ``ha_config_set_automation`` and
    ``ha_config_set_script``; see
    :mod:`ha_mcp.tools.reference_validator` for the validator itself
    and #940 for background.
    """
    entry: dict[str, Any] = {}
    if validation_meta.get("warnings"):
        entry["warnings"] = validation_meta["warnings"]
    if validation_meta.get("unvalidated_templates"):
        entry["unvalidated_templates"] = validation_meta["unvalidated_templates"]
    if validation_meta.get("blueprint_skipped"):
        entry["blueprint_skipped"] = True
    for key in ("unchecked_target_kinds", "references_unchecked"):
        if validation_meta.get(key):
            entry[key] = validation_meta[key]
    if entry:
        result["validation"] = entry
