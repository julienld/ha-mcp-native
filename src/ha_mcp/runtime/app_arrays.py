"""Ordered app API array changes; transport owns persistence and conflict checks."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response, create_validation_error

from .errors import raise_failure

_ARRAY_PATCH_OPS = {"patch", "delete", "add", "delete_where"}
_ARRAY_PATCH_MISSING: Any = object()


def _op_patch(
    working: list[Any],
    op_spec: dict[str, Any],
    index: int,
    id_field: str,
) -> dict[str, Any]:
    target_id = op_spec.get("id")
    if target_id is None:
        raise_failure(
            create_validation_error(
                f"array_patch patch op #{index} missing 'id'",
                parameter=f"array_patch.operations[{index}].id",
            )
        )
    patches = op_spec.get("patches")
    if not isinstance(patches, dict):
        raise_failure(
            create_validation_error(
                f"array_patch patch op #{index} 'patches' must be an object",
                parameter=f"array_patch.operations[{index}].patches",
            )
        )
    # target.update({}) is a silent no-op — the item would appear in
    # summary["patched"] with fields: [], giving the caller no signal that
    # nothing changed. Reject up-front so the mistake surfaces immediately.
    if not patches:
        raise_failure(
            create_validation_error(
                f"array_patch patch op #{index} 'patches' cannot be empty "
                "(no fields to update)",
                parameter=f"array_patch.operations[{index}].patches",
            )
        )
    target = next(
        (
            it
            for it in working
            if isinstance(it, dict) and it.get(id_field) == target_id
        ),
        None,
    )
    if target is None:
        raise_failure(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"No item with {id_field}={target_id!r} for patch op #{index}",
                context={"id_field": id_field, "id": target_id},
            )
        )
    target.update(patches)
    return {"id": target_id, "fields": list(patches.keys())}


def _op_delete(
    working: list[Any],
    op_spec: dict[str, Any],
    index: int,
    id_field: str,
) -> tuple[list[Any], dict[str, Any]]:
    target_id = op_spec.get("id")
    if target_id is None:
        raise_failure(
            create_validation_error(
                f"array_patch delete op #{index} missing 'id'",
                parameter=f"array_patch.operations[{index}].id",
            )
        )
    new_working = [
        it
        for it in working
        if not (isinstance(it, dict) and it.get(id_field) == target_id)
    ]
    if len(new_working) == len(working):
        raise_failure(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"No item with {id_field}={target_id!r} for delete op #{index}",
                context={"id_field": id_field, "id": target_id},
            )
        )
    return new_working, {"id": target_id}


def _op_add(
    working: list[Any],
    op_spec: dict[str, Any],
    index: int,
    id_field: str,
) -> dict[str, Any]:
    new_item = op_spec.get("item")
    if not isinstance(new_item, dict):
        raise_failure(
            create_validation_error(
                f"array_patch add op #{index} 'item' must be an object",
                parameter=f"array_patch.operations[{index}].item",
            )
        )
    if id_field not in new_item:
        raise_failure(
            create_validation_error(
                f"array_patch add op #{index} 'item' missing id field {id_field!r}",
                parameter=f"array_patch.operations[{index}].item",
            )
        )
    new_id = new_item[id_field]
    # None and blank strings are rejected because dict.get(id_field) == None by
    # default, so allowing them would let later patch/delete ops match unrelated
    # items. Non-string ids (e.g. integer 0) stay valid by design —
    # see test_add_with_integer_zero_id_is_accepted.
    if new_id is None or (isinstance(new_id, str) and not new_id.strip()):
        raise_failure(
            create_validation_error(
                f"array_patch add op #{index} item {id_field!r} cannot be "
                "None, empty, or whitespace-only",
                parameter=f"array_patch.operations[{index}].item.{id_field}",
            )
        )
    if any(isinstance(it, dict) and it.get(id_field) == new_id for it in working):
        raise_failure(
            create_error_response(
                ErrorCode.RESOURCE_ALREADY_EXISTS,
                f"Item with {id_field}={new_id!r} already exists (add op #{index})",
                context={"id_field": id_field, "id": new_id},
            )
        )
    working.append(new_item)
    return {"id": new_id}


def _op_delete_where(
    working: list[Any],
    op_spec: dict[str, Any],
    index: int,
) -> tuple[list[Any], dict[str, Any]]:
    field = op_spec.get("field")
    value = op_spec.get("value", _ARRAY_PATCH_MISSING)
    if not isinstance(field, str) or not field:
        raise_failure(
            create_validation_error(
                f"array_patch delete_where op #{index} missing or empty 'field'",
                parameter=f"array_patch.operations[{index}].field",
            )
        )
    if value is _ARRAY_PATCH_MISSING:
        raise_failure(
            create_validation_error(
                f"array_patch delete_where op #{index} missing 'value'",
                parameter=f"array_patch.operations[{index}].value",
            )
        )
    new_working = [
        it
        for it in working
        if not (isinstance(it, dict) and it.get(field, _ARRAY_PATCH_MISSING) == value)
    ]
    removed = len(working) - len(new_working)
    entry: dict[str, Any] = {"field": field, "value": value, "count": removed}
    # Distinguish "value not present" from "field name unknown to any item" —
    # the latter is almost always a typo and would otherwise silently give
    # count=0. Only warn when there are dict items to inspect; an empty or
    # all-non-dict array would trivially satisfy `not any(...)` and produce
    # a misleading typo suggestion.
    inspectable = [it for it in new_working if isinstance(it, dict)]
    if removed == 0 and inspectable and not any(field in it for it in inspectable):
        entry.setdefault("warnings", []).append(
            f"field {field!r} is not present on any item — "
            "check for a typo in the field name"
        )
    return new_working, entry


def _apply_array_ops(
    items: list[Any],
    operations: list[dict[str, Any]],
    id_field: str,
) -> tuple[list[Any], dict[str, Any]]:
    """Apply a sequence of array_patch operations to a list of resource dicts.

    Operations are applied in order against a working copy. Any validation
    failure (unknown op, missing reference, id collision, missing required
    field) raises ToolFailure before the caller posts anything back, giving
    fail-fast all-or-nothing semantics from the server's perspective.

    Args:
        items: Current array fetched from the addon (mutated copy is built here).
        operations: Ordered list of op dicts. Supported shapes:
            {"op": "patch", "id": <value>, "patches": {field: value, ...}}
            {"op": "delete", "id": <value>}
            {"op": "add", "item": {<id_field>: <value>, ...}}
            {"op": "delete_where", "field": <name>, "value": <value>}
        id_field: Field name on each item used as its identifier.

    Returns:
        Tuple of (new_array, summary). Summary lists what each op touched —
        IDs only, no full payloads — so the response stays compact even when
        the underlying array is large.
    """
    # Shallow copy of the outer list. The inner item dicts are NOT copied —
    # patch ops mutate them in place via `target.update(...)`. Callers must
    # not retain references to `items` and expect them unchanged; this is
    # safe here because the dispatcher only uses `items` to build the POST
    # body and then discards it.
    working = list(items)

    summary: dict[str, list[Any]] = {
        "patched": [],
        "deleted": [],
        "added": [],
        "deleted_where": [],
    }

    for index, op_spec in enumerate(operations):
        if not isinstance(op_spec, dict):
            raise_failure(
                create_validation_error(
                    f"array_patch operation #{index} is not an object",
                    parameter="array_patch.operations",
                )
            )

        op = op_spec.get("op")
        if op not in _ARRAY_PATCH_OPS:
            raise_failure(
                create_validation_error(
                    f"array_patch op '{op}' not recognised "
                    f"(expected one of: {sorted(_ARRAY_PATCH_OPS)})",
                    parameter=f"array_patch.operations[{index}].op",
                )
            )

        if op == "patch":
            summary["patched"].append(_op_patch(working, op_spec, index, id_field))
        elif op == "delete":
            working, entry = _op_delete(working, op_spec, index, id_field)
            summary["deleted"].append(entry)
        elif op == "add":
            summary["added"].append(_op_add(working, op_spec, index, id_field))
        else:  # delete_where
            working, entry = _op_delete_where(working, op_spec, index)
            summary["deleted_where"].append(entry)

    return working, summary
