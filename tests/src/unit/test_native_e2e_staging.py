"""Qualification must boot the selected runtime under a real fixture identity."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.src.e2e._conftest_native_runtime import (
    attest_native_runtime,
    configure_native_entry,
)


def _seed(config: Path, user: dict) -> Path:
    storage = config / ".storage"
    storage.mkdir()
    (storage / "auth").write_text(json.dumps({"data": {"users": [user]}}))
    path = storage / "core.config_entries"
    path.write_text(
        json.dumps(
            {
                "data": {
                    "entries": [
                        {
                            "entry_id": "server",
                            "data": {"admin_token": "old-fixture-token"},
                            "options": {"webhook_auth": "none"},
                        }
                    ]
                }
            }
        )
    )
    return path


@pytest.fixture
def owner() -> dict:
    return {
        "id": "owner",
        "is_owner": True,
        "is_active": True,
        "system_generated": False,
        "group_ids": ["system-admin"],
    }


def test_native_staging_uses_human_owner_without_retaining_a_token(tmp_path, owner):
    path = _seed(tmp_path, owner)
    configure_native_entry(tmp_path, "server")
    entry = json.loads(path.read_text())["data"]["entries"][0]
    assert entry["data"] == {"native_owner_user_id": "owner"}
    assert entry["options"]["runtime_mode"] == "native"
    assert entry["options"]["webhook_auth"] == "none"
    assert not entry["options"]["enable_llm_api"]
    assert not entry["options"]["enable_native_llm_import"]


@pytest.mark.parametrize(
    "change",
    [
        {"is_active": False},
        {"system_generated": True},
        {"group_ids": ["system-users"]},
        {"is_owner": False},
    ],
)
def test_missing_human_owner_refuses_before_rewriting_the_entry(
    tmp_path, owner, change
):
    path = _seed(tmp_path, {**owner, **change})
    before = path.read_bytes()
    with pytest.raises(ValueError, match="human fixture owner"):
        configure_native_entry(tmp_path, "server")
    assert path.read_bytes() == before


def test_a_responding_legacy_server_cannot_qualify_as_native(monkeypatch):
    response = SimpleNamespace(
        raise_for_status=Mock(),
        json=lambda: {
            "manager": "EmbeddedServerManager",
            "runtime": "legacy",
            "running": True,
        },
    )
    monkeypatch.setattr(
        "tests.src.e2e._conftest_native_runtime.requests.get",
        lambda *args, **kwargs: response,
    )
    with pytest.raises(AssertionError, match="did not attest"):
        attest_native_runtime("http://fixture.test", {"Authorization": "fixture"})
