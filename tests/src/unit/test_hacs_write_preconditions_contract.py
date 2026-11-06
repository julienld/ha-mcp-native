"""The live store oracles distinguish first-run gates from unrelated auth failures."""

from unittest.mock import AsyncMock

import pytest

from tests.src.e2e.workflows.hacs import test_hacs as live


@pytest.mark.parametrize(
    "method", ["test_add_invalid_repository", "test_download_nonexistent_repository"]
)
@pytest.mark.parametrize("gate", ["warning_not_accepted", "github_not_connected"])
async def test_live_oracle_accepts_only_known_marketplace_prerequisites(
    monkeypatch, method, gate
):
    response = {
        "success": False,
        "error": {
            "code": "AUTH_FORBIDDEN",
            "message": f"Marketplace {gate}: finish setup in the UI",
        },
    }
    command = AsyncMock(return_value=response)
    monkeypatch.setattr(live, "safe_call_tool", command)
    await getattr(live.TestHacsWriteOperations(), method)(object())
    command.assert_awaited_once()


@pytest.mark.parametrize(
    "method", ["test_add_invalid_repository", "test_download_nonexistent_repository"]
)
@pytest.mark.parametrize(
    "response",
    [
        {
            "success": False,
            "error": {
                "code": "AUTH_FORBIDDEN",
                "message": "Unrelated permission failure",
            },
        },
        {
            "success": True,
            "error": {
                "code": "AUTH_FORBIDDEN",
                "message": "Marketplace warning_not_accepted: contradictory success",
            },
        },
    ],
)
async def test_live_oracle_rejects_unrelated_auth_and_false_success(
    monkeypatch, method, response
):
    monkeypatch.setattr(live, "safe_call_tool", AsyncMock(return_value=response))
    with pytest.raises(AssertionError):
        await getattr(live.TestHacsWriteOperations(), method)(object())
