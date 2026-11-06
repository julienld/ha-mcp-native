"""Readback expectations and the snapshot field Core's group.set cannot clear."""

from typing import Any


def expected_values(
    before: dict[str, Any] | None, fields: dict[str, Any]
) -> dict[str, Any]:
    expected = (
        dict(before)
        if before is not None
        else {
            "object_id": fields["object_id"],
            "name": fields["object_id"],
            "entities": [],
            "icon": None,
            "all": False,
            "created_by_service": True,
        }
    )
    for key in ("name", "icon", "all", "entities"):
        if key in fields:
            expected[key] = fields[key]
    if "add_entities" in fields:
        expected["entities"] = list(
            set(expected["entities"]) | set(fields["add_entities"])
        )
    if "remove_entities" in fields:
        expected["entities"] = list(
            set(expected["entities"]) - set(fields["remove_entities"])
        )
    return expected
