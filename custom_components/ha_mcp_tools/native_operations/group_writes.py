"""Small sequences of Core Group callbacks; Core keeps its membership algorithm."""

import inspect
from collections.abc import Callable
from functools import partial
from typing import Any

from ha_mcp.tool_api import UnsupportedOperation

from .group_records import assert_group_values, group_snapshot


def _sync(owner: Any, name: str, *arguments: Any) -> None:
    method = getattr(owner, name, None)
    if not callable(method) or inspect.iscoroutinefunction(method):
        raise UnsupportedOperation("groups.synchronous_callback." + name)
    method(*arguments)


def _membership(owner: Any, fields: dict[str, Any]) -> Any:
    if "entities" in fields:
        return fields["entities"]
    if "add_entities" in fields:
        return set(owner.tracking) | set(fields["add_entities"])
    if "remove_entities" in fields:
        return set(owner.tracking) - set(fields["remove_entities"])
    return None


def apply_fields(owner: Any, fields: dict[str, Any], guard: Callable[[], None]) -> None:
    before = group_snapshot(owner)
    assert before is not None
    proposed = dict(before)
    members = _membership(owner, fields)
    steps: list[tuple[str, Any, Callable[[], None]]] = []
    if members is not None:
        steps.append(
            (
                "entities",
                list(members),
                lambda: _sync(owner, "async_update_tracked_entity_ids", members),
            )
        )
    for key, method in (("name", "set_name"), ("icon", "set_icon")):
        if key in fields:
            value = fields[key]
            steps.append((key, value, partial(_sync, owner, method, value)))
    if "all" in fields:
        # Core's Group service uses this public callable field; there is no
        # mode setter. Its actual aggregation callback still computes state.
        steps.append(
            (
                "all",
                fields["all"],
                lambda: setattr(owner, "mode", all if fields["all"] else any),
            )
        )
    for key, value, change in steps:
        guard()
        assert_group_values(owner, proposed)
        change()
        proposed[key] = value
        guard()
        assert_group_values(owner, proposed)
    if steps:
        guard()
        _sync(owner, "async_update_group_state")
        _sync(owner, "async_write_ha_state")
        guard()
        assert_group_values(owner, proposed)
