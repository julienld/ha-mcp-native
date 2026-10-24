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


@pytest.fixture
def native_observation():
    return {
        "manager": "NativeServerManager",
        "runtime": "native",
        "running": True,
        "on_core_loop": True,
        "legacy_listener": False,
        "legacy_thread": False,
        "admin_token_present": False,
        "revision": "candidate",
        "library_version": "candidate-version",
        "core_version": "2026.8.0",
        "architecture": "aarch64",
    }


@pytest.mark.parametrize("observed_core", [None, "2026.10.3"])
def test_a_native_server_on_the_wrong_core_cannot_qualify_the_minimum(
    monkeypatch, observed_core, native_observation
):
    observed = {**native_observation, "core_version": observed_core}
    monkeypatch.setenv("E2E_EXPECTED_CORE_VERSION", "2026.8.0")
    monkeypatch.setattr(
        "tests.src.e2e._conftest_native_runtime.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=Mock(), json=lambda: observed
        ),
    )
    with pytest.raises(AssertionError, match="Core version"):
        attest_native_runtime("http://fixture.test", {})


def test_an_x64_container_cannot_qualify_the_arm64_lane(
    monkeypatch, native_observation
):
    monkeypatch.setenv("E2E_EXPECTED_ARCH", "ARM64")
    observed = {**native_observation, "architecture": "x86_64"}
    monkeypatch.setattr(
        "tests.src.e2e._conftest_native_runtime.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=Mock(), json=lambda: observed
        ),
    )
    with pytest.raises(AssertionError, match="architecture"):
        attest_native_runtime("http://fixture.test", {})
