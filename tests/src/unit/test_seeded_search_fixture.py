"""Seeded search coverage must prepare legal registry state, never hide failures."""

import copy
from contextlib import asynccontextmanager

import pytest

from ..e2e.tools import test_search_entities as search


class SeedRegistryClient:
    def __init__(self, *, fail_after_dispatch=None):
        assignments = search.TestSearchEntitiesSeededAreasIssue1170.SEED_ASSIGNMENTS
        names = [None, "", "Original switch", None]
        self.entries = {
            eid: {"name": name, "area_id": None}
            for (eid, _), name in zip(assignments, names, strict=True)
        }
        self.entries["switch.ac"]["area_id"] = "previous_room"
        self.before = copy.deepcopy(self.entries)
        self.fail_after_dispatch = fail_after_dispatch
        self.writes = []
        self.search_reads = 0

    async def call_tool(self, tool, arguments):
        if tool == "ha_get_entity":
            return {
                "success": True,
                "entity_entry": copy.deepcopy(self.entries[arguments["entity_id"]]),
            }
        if tool == "ha_search":
            self.search_reads += 1
            return {
                "success": True,
                "entities": [
                    {"entity_id": eid}
                    for eid, entry in self.entries.items()
                    if entry["area_id"] == arguments["area_filter"]
                    and self.search_reads > 1
                ],
            }
        assert tool == "ha_set_entity"
        eid = arguments["entity_id"]
        candidate = copy.deepcopy(self.entries[eid])
        if "name" in arguments:
            candidate["name"] = arguments["name"] or None
        if "use_device_name" in arguments:
            candidate["name"] = "" if arguments["use_device_name"] else None
        if "area_id" in arguments:
            candidate["area_id"] = arguments["area_id"] or None
        # Core 2026.10 refuses an entity area without an entity's own name.
        if candidate["area_id"] and not candidate["name"]:
            raise ValueError("An entity without a name of its own cannot have an area")
        self.writes.append(copy.deepcopy(arguments))
        self.entries[eid] = candidate
        if eid == self.fail_after_dispatch and candidate["area_id"] == "bedroom":
            raise RuntimeError("Provider failed after accepting the update")
        return {"success": True}


def fixture(client):
    case = search.TestSearchEntitiesSeededAreasIssue1170()
    return asynccontextmanager(type(case).seeded_bedroom.__wrapped__)(case, client)


async def test_seeded_search_prepares_unnamed_entities_and_restores_exact_overrides():
    client = SeedRegistryClient()
    try:
        async with fixture(client) as prepared:
            assert set(prepared["entity_ids"]) == set(client.entries)
            assert all(entry["name"] for entry in client.entries.values())
            assert all(
                entry["area_id"] == "bedroom" for entry in client.entries.values()
            )
            assert len(client.writes) == 4, "Readiness polling repeated a mutation"
            assert client.search_reads >= 2
    except pytest.skip.Exception as error:
        pytest.fail(f"Valid seed entities were skipped instead of prepared: {error}")
    assert client.entries == client.before
    assert len(client.writes) == 8


async def test_seeded_search_propagates_setup_failure_and_restores_partial_dispatch():
    client = SeedRegistryClient(fail_after_dispatch="light.ceiling_lights")
    for eid, entry in client.entries.items():
        entry["name"] = f"Original {eid}"
    client.before = copy.deepcopy(client.entries)
    try:
        with pytest.raises(RuntimeError, match="after accepting"):
            async with fixture(client):
                pytest.fail("The failed preparation yielded a usable fixture")
    except pytest.skip.Exception as error:
        pytest.fail(f"A setup failure was converted to a skip: {error}")
    assert client.entries == client.before
    assert [call["entity_id"] for call in client.writes].count(
        "light.ceiling_lights"
    ) == 2
