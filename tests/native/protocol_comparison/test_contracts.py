"""Contract probes with synthetic wire replies and fixture effects; no live HA."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from .contracts import actor_contract, read_only_contract
from .wire import Observation, WireError, parse_reply


def record(marker: str, actor: str) -> dict[str, Any]:
    # Deliberately different object-key order from the contract's expectation.
    return {
        "nested": {"empty": {}, "values": [0, False, ""]},
        "actor": actor,
        "marker": marker,
    }


def reply(result: dict[str, Any]) -> Observation:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
    return Observation(
        200, "application/json", 0, len(body), parse_reply("application/json", body, 1)
    )


def corrupt_values(value: dict[str, Any]) -> None:
    value["nested"]["values"] = [False, 0, ""]


@pytest.fixture
def actor_target() -> SimpleNamespace:
    first, second = record("actor-a", "user-a"), record("actor-b", "user-b")
    snapshots = [
        {"state": "ready", "records": []},
        {"state": "actor-a", "records": [deepcopy(first)]},
        {"state": "actor-a", "records": [deepcopy(first)]},
        {"state": "actor-b", "records": [deepcopy(first), deepcopy(second)]},
    ]
    actors = [
        {
            "expected_user_id": value["actor"],
            "client": SimpleNamespace(
                rpc=AsyncMock(
                    return_value=reply(
                        {"structuredContent": {"service_response": value}}
                    )
                )
            ),
        }
        for value in (first, second)
    ]
    return SimpleNamespace(
        actors=actors, snapshots=snapshots, snapshot=AsyncMock(side_effect=snapshots)
    )


async def test_actor_contract_accepts_exact_values_for_both_actors(
    actor_target: SimpleNamespace,
) -> None:
    result = await actor_contract(actor_target)
    assert result["core_effects_verified"] and result["distinct_actors_observed"]


@pytest.mark.parametrize(
    "surface,category",
    [
        ("response", "service_response_or_actor_mismatch"),
        ("effect", "core_effect_or_actor_mismatch"),
    ],
)
async def test_actor_contract_rejects_boolean_integer_substitution(
    actor_target: SimpleNamespace, surface: str, category: str
) -> None:
    if surface == "response":
        response = actor_target.actors[0]["client"].rpc.return_value
        corrupt_values(response.result()["structuredContent"]["service_response"])
    else:
        corrupt_values(actor_target.snapshots[1]["records"][0])
    with pytest.raises(WireError, match=category):
        await actor_contract(actor_target)


@pytest.fixture
def read_only_target() -> SimpleNamespace:
    previous, restored = record("actor-b", "user-b"), record("actor-a", "user-a")
    snapshots = [
        {"state": "actor-b", "records": [deepcopy(previous)]},
        {"state": "actor-b", "records": [deepcopy(previous)]},
        {"state": "actor-a", "records": [deepcopy(previous), deepcopy(restored)]},
    ]
    responses = [
        reply({"tools": [{"name": "ha_call_service"}]}),
        reply({"isError": True, "content": [{"type": "text", "text": "read-only"}]}),
        reply({"structuredContent": {"data": {"state": "actor-b"}}}),
        reply({"structuredContent": {"service_response": restored}}),
    ]
    return SimpleNamespace(
        actors=[
            {
                "expected_user_id": "user-a",
                "client": SimpleNamespace(rpc=AsyncMock(side_effect=responses)),
            }
        ],
        snapshots=snapshots,
        responses=responses,
        snapshot=AsyncMock(side_effect=snapshots),
        policy=AsyncMock(),
        wait_ready=AsyncMock(),
    )


async def test_read_only_contract_accepts_unchanged_records_and_restored_writes(
    read_only_target: SimpleNamespace,
) -> None:
    result = await read_only_contract(read_only_target)
    assert result["no_service_effect"] and result["writes_work_after_restoration"]


@pytest.mark.parametrize(
    "surface,category",
    [
        ("denied_effect", "read_only_call_changed_fixture"),
        ("restored_response", "read_only_restoration_not_effective"),
        ("restored_effect", "read_only_restoration_not_effective"),
    ],
)
async def test_read_only_contract_rejects_boolean_integer_substitution(
    read_only_target: SimpleNamespace, surface: str, category: str
) -> None:
    if surface == "denied_effect":
        corrupt_values(read_only_target.snapshots[1]["records"][0])
    elif surface == "restored_response":
        response = read_only_target.responses[-1]
        corrupt_values(response.result()["structuredContent"]["service_response"])
    else:
        corrupt_values(read_only_target.snapshots[2]["records"][-1])
    with pytest.raises(WireError, match=category):
        await read_only_contract(read_only_target)
    read_only_target.policy.assert_any_await(False)
