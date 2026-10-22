"""Storage-helper orchestration over explicit collection operations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import BackendTools, UnsupportedOperation
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .errors import raise_failure
from .helper_flow import create_single_form_helper
from .helper_params import _prepare_typed_params
from .helper_payload import check_core_gaps, merged_update, with_create_defaults
from .helper_schema import (
    _CORE_HELPER_SCHEMAS,
    _HELPER_SKILL_FILES,
    SIMPLE_HELPER_TYPES,
    _helper_response,
    _simple_helper_error_context,
)
from .registry_references import (
    _raise_if_unknown_area,
    _raise_if_unknown_category,
    _raise_if_unknown_labels,
)
from .skill_content import apply_skill_content
from .validation import validate_identifier_not_empty


class HelperStorageTools(BackendTools):
    _prepare_typed_params = staticmethod(_prepare_typed_params)

    async def _require_helper_type(self, helper_type: str) -> None:
        if helper_type in SIMPLE_HELPER_TYPES or helper_type == "utility_meter":
            return
        raise UnsupportedOperation(f"helpers.flow.{helper_type}")

    async def _validate_action(
        self, action: str | None, helper_id: str | None, helper_type: str
    ) -> str:
        if action == "create" and helper_id is not None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"action='create' was passed together with helper_id={helper_id!r}. These are contradictory: create makes a new helper, while helper_id targets an existing one.",
                    context=_simple_helper_error_context(
                        helper_type, action=action, helper_id=helper_id
                    ),
                )
            )
        if action == "update" and helper_id is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "action='update' requires helper_id to identify which helper to modify.",
                    context=_simple_helper_error_context(helper_type, action=action),
                )
            )
        if helper_id is not None:
            validate_identifier_not_empty(
                helper_id, "helper_id", context={"helper_type": helper_type}
            )
        return action or ("update" if helper_id else "create")

    @asynccontextmanager
    async def _schema_context(
        self, helper_type: str, action: str | None
    ) -> AsyncIterator[None]:
        if helper_type not in SIMPLE_HELPER_TYPES:
            yield
            return
        schemas = await self.backend.execute(
            "helpers.storage.schemas", {}, self.context
        )
        token = _CORE_HELPER_SCHEMAS.set((schemas.get("types", {}), action or "create"))
        try:
            yield
        finally:
            _CORE_HELPER_SCHEMAS.reset(token)

    async def _check_name_collision(self, helper_type: str, name: str | None) -> None:
        if not name or not name.strip():
            return
        listed = await self.backend.execute(
            "helpers.storage.list"
            if helper_type in SIMPLE_HELPER_TYPES
            else "helpers.flow.entries",
            {"helper_type": helper_type},
            self.context,
        )
        target = _name_slug(name)
        if not target:
            return
        for item in listed["items"]:
            if item.get("id") == target or _name_slug(item.get("name", "")) == target:
                existing = item.get("id") or target
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"A {helper_type} helper named {name!r} already exists (id: {existing!r}). Pass helper_id={existing!r} to update it, or use a different name to create a new helper.",
                        context=_simple_helper_error_context(
                            helper_type, name=name, existing_helper_id=existing
                        ),
                    )
                )

    async def _validate_registry_ids(
        self,
        area_id: str | None,
        labels: list[str] | None,
        categories: dict[str, str | None],
        *,
        fail_closed: bool,
    ) -> None:
        requested = (
            (["area"] if area_id else [])
            + (["label"] if labels else [])
            + (["category"] if any(categories.values()) else [])
        )
        if not requested:
            return
        rows = await self.backend.execute(
            "helpers.registry.snapshot",
            {"registries": requested, "category_scopes": list(categories)},
            self.context,
        )
        if area_id:
            _raise_if_unknown_area(rows["areas"], area_id)
        if labels:
            _raise_if_unknown_labels(rows["labels"], labels)
        for scope, category in categories.items():
            if category:
                _raise_if_unknown_category(rows["categories"][scope], category, scope)

    async def _attach_helper_skill(
        self, response: dict[str, Any], mandatory: bool
    ) -> None:
        resolved = await self.backend.execute(
            "skills.files",
            {"references": list(_HELPER_SKILL_FILES) if mandatory else []},
            self.context,
        )
        apply_skill_content(
            response,
            resolved["content"],
            enabled=resolved["enabled"],
            requested=mandatory,
            vendor_missing=resolved["vendor_missing"],
        )

    async def _create_simple(
        self,
        helper_type: str,
        name: str | None,
        icon: str | None,
        area_id: str | None,
        labels: list[str] | None,
        category: str | None,
        wait: bool,
        MandatoryBPS: bool,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        if not name or not name.strip():
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f'name is required for create action. Include "name" as a top-level argument, e.g. {{"helper_type": "{helper_type}", "name": "My Helper"}}.',
                    context=_simple_helper_error_context(helper_type),
                )
            )
        item_icon = (
            None
            if helper_type == "zone"
            else _collection_icon(helper_type, "create", icon)
        )
        body = {"name": name, **with_create_defaults(helper_type, fields)}
        if item_icon:
            body["icon"] = item_icon
        check_core_gaps(helper_type, body)
        registry = _registry_fields(
            icon if item_icon is None else None, area_id, labels, category
        )
        async with registry_update_lock(
            f"helper_{helper_type}", f"name:{_name_slug(name)}"
        ):
            await self._check_name_collision(helper_type, name)
            result = await self.backend.execute(
                "helpers.storage.write",
                {
                    "helper_type": helper_type,
                    "action": "create",
                    "data": body,
                    "registry": registry,
                    "wait_for_entity": wait,
                },
                self.context,
            )
        response = _write_response(
            result, "create", helper_type, f"Successfully created {helper_type}: {name}"
        )
        await self._attach_helper_skill(response, MandatoryBPS)
        return response

    async def _update_simple(
        self,
        helper_type: str,
        helper_id: str,
        entity_id: str,
        name: str | None,
        icon: str | None,
        area_id: str | None,
        labels: list[str] | None,
        category: str | None,
        wait: bool,
        MandatoryBPS: bool,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        async with registry_update_lock(f"helper_{helper_type}", helper_id):
            item = await self._stored_item(helper_type, helper_id)
            entity_id = item.get("entity_id") or helper_id
            body = merged_update(
                helper_type,
                item["item"],
                name,
                _collection_icon(helper_type, "update", icon),
                fields,
                entity_id,
            )
            check_core_gaps(helper_type, body, changed=fields)
            backup = await self.backend.execute(
                "helpers.backup.capture",
                {
                    "helper_type": helper_type,
                    "action": "update",
                    "backup_id": helper_id,
                    "item_id": item["item_id"],
                    "entity_id": item.get("entity_id"),
                    "current_item": item["item"],
                },
                self.context,
            )
            result = await self.backend.execute(
                "helpers.storage.write",
                {
                    "helper_type": helper_type,
                    "action": "update",
                    "item_id": item["item_id"],
                    "data": body,
                    "registry": _registry_fields(icon, area_id, labels, category),
                    "expected_item": item["item"],
                    "wait_for_entity": wait,
                },
                self.context,
            )
        response = _write_response(
            result,
            "update",
            helper_type,
            f"Successfully updated {helper_type}: {item.get('entity_id') or helper_id}",
        )
        if backup and backup.get("warnings"):
            response.setdefault("warnings", []).extend(backup["warnings"])
        await self._attach_helper_skill(response, MandatoryBPS)
        return response

    async def _stored_item(self, helper_type: str, helper_id: str) -> dict[str, Any]:
        result = await self.backend.execute(
            "helpers.storage.read",
            {"helper_type": helper_type, "item_id": helper_id},
            self.context,
        )
        if result.get("success") is not True and helper_id.startswith(
            f"{helper_type}."
        ):
            result = await self.backend.execute(
                "helpers.storage.read",
                {"helper_type": helper_type, "entity_id": helper_id},
                self.context,
            )
        if result.get("success") is not True:
            raise_failure(
                create_error_response(
                    ErrorCode.CONFIG_NOT_FOUND,
                    f"{helper_type} config not found for id: {helper_id}",
                    context=_simple_helper_error_context(
                        helper_type, helper_id=helper_id
                    ),
                )
            )
        stored: dict[str, Any] = result
        return stored

    async def _flow_helper(
        self,
        helper_type: str,
        name: str | None,
        helper_id: str | None,
        config: dict[str, Any] | None,
        area_id: str | None,
        labels: Any,
        category: str | None,
        wait: bool,
        *,
        icon: str | None,
        action: str | None,
    ) -> dict[str, Any]:
        if action != "create":
            raise UnsupportedOperation(f"helpers.flow.{helper_type}.update")
        from ha_mcp.tools.coercion import parse_string_list_param

        parsed_labels = parse_string_list_param(labels, "labels")
        await self._validate_registry_ids(
            area_id, parsed_labels, {"helpers": category}, fail_closed=True
        )
        response = await create_single_form_helper(
            self.backend, self.context, helper_type, name, config, wait=wait
        )
        registry = _registry_fields(icon, area_id, parsed_labels, category)
        if registry and response["entity_ids"]:
            result = await self.backend.execute(
                "helpers.entities.update",
                {"entity_ids": response["entity_ids"], "registry": registry},
                self.context,
            )
            response["applied"] = result["applied"]
            for key in registry:
                applied = [row[key] for row in result["applied"] if key in row]
                if applied:
                    response[key] = applied[0]
            if result.get("warnings"):
                response.setdefault("warnings", []).extend(result["warnings"])
        return response

    async def _set_config_subentry(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise UnsupportedOperation("helpers.subentry.orchestration")

    def _map_helper_error(
        self, error: Exception, action: str | None, helper_type: str
    ) -> NoReturn:
        raise error


def _name_slug(name: str) -> str:
    lowered = name.lower().replace(" ", "_")
    return "".join(
        character for character in lowered if character.isalnum() or character == "_"
    )


def _collection_icon(helper_type: str, action: str, icon: str | None) -> str | None:
    if icon is None or icon == "" or helper_type == "zone":
        return icon
    current = _CORE_HELPER_SCHEMAS.get()
    fields = (current[0].get(helper_type, {}).get(action) or []) if current else []
    return icon if any(field.get("name") == "icon" for field in fields) else None


def _registry_fields(
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
) -> dict[str, Any]:
    return {
        key: value
        for key, value in (
            ("icon", icon),
            ("area_id", area_id),
            ("labels", labels),
            ("category", category),
        )
        if value is not None
    }


def _write_response(
    result: dict[str, Any], action: str, helper_type: str, message: str
) -> dict[str, Any]:
    if result.get("success") is not True:
        error = result.get("error") or {}
        code = (
            ErrorCode.CONFIG_NOT_FOUND
            if error.get("code") == "not_found"
            else ErrorCode.VALIDATION_INVALID_PARAMETER
        )
        raise_failure(
            create_error_response(
                code,
                f"Failed to {action} {helper_type}: {error.get('message', 'unknown error')}",
                context=_simple_helper_error_context(helper_type),
            )
        )
    return _helper_response(
        action,
        helper_type,
        data={**(result.get("item") or {}), **(result.get("registry_applied") or {})},
        entity_id=result.get("entity_id"),
        message=message,
        warnings=list(result.get("warnings") or []),
    )
