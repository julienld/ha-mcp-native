"""Policy tools preserve CAS and authority at the actual settings writer."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native
from .test_native_file_access import files as files
from .test_native_file_settings import panel as panel
from .test_native_filesystem import filesystem as filesystem


@pytest.fixture
def document(panel):
    settings, _owner = panel
    from custom_components.ha_mcp_tools.native_operations.policy_document import (
        NativePolicyDocument,
    )

    manager = settings.manager
    manager.settings_ui = settings
    manager.policy.flags["enable_security_policy_tool"] = True
    manager.approval_gate = SimpleNamespace(
        queue=SimpleNamespace(clear_remember_cache=MagicMock())
    )
    return NativePolicyDocument(manager, CallContext("owner")), settings


async def test_native_policy_missing_version_still_reports_its_actual_CAS(document):
    owner, _settings = document
    result = await owner.set({"rules": []}, None)
    assert result["success"] and result["data"]["version"] == 1
    assert not any(
        "without an optimistic-concurrency check" in warning
        for warning in result.get("warnings", [])
    )


async def test_native_policy_conflict_withholds_current_document_after_revocation(
    document, monkeypatch
):
    owner, settings = document
    await owner.set({"rules": []}, None)
    save = settings._save_policy

    def conflict(*args):
        value = save(*args)
        owner.manager.backend.authorize.side_effect = PermissionError("revoked")
        return value

    monkeypatch.setattr(settings, "_save_policy", conflict)
    with pytest.raises(ToolFailure) as raised:
        await owner.set({"rules": [], "version": 0}, 0)
    assert raised.value.code == "AUTH_FORBIDDEN"
    assert "current_policy" not in repr(raised.value.data)


async def test_native_policy_rechecks_before_starting_the_owned_persistence_task(
    document, monkeypatch
):
    owner, settings = document
    import custom_components.ha_mcp_tools.native_operations.policy_document as module

    owned = module.await_owned_io
    count = 0
    saved = MagicMock(wraps=settings._save_policy)
    monkeypatch.setattr(settings, "_save_policy", saved)

    async def before_worker(awaitable):
        nonlocal count
        count += 1
        if (
            count == 2
        ):  # Initial read has finished; the persistence task has not started.
            owner.manager.backend.authorize.side_effect = PermissionError("revoked")
        return await owned(awaitable)

    monkeypatch.setattr(module, "await_owned_io", before_worker)
    with pytest.raises(ToolFailure) as raised:
        await owner.set({"rules": []}, None)
    saved.assert_not_called()
    assert raised.value.code == "AUTH_FORBIDDEN"


async def test_policy_facade_preserves_whole_document_and_pin_guards(document):
    owner, settings = document
    from custom_components.ha_mcp_tools.native_security_policy_tools import (
        security_policy_definition,
    )
    from ha_mcp.runtime import Engine

    manager = owner.manager
    manager.engine = Engine(manager.backend, [security_policy_definition(manager)])
    actor = CallContext("owner")
    initial = await manager.engine.call(
        "ha_manage_security_policy",
        {"action": "set", "policy": {"rules": [], "rule_effect": "allow"}},
        actor,
    )
    assert not initial.is_error, initial.data
    for replacement in (
        {"rules": []},
        {"rules": [], "rule_effect": "allow", "event_decisions_enabled": True},
    ):
        result = await manager.engine.call(
            "ha_manage_security_policy", {"action": "set", "policy": replacement}, actor
        )
        assert result.is_error and result.status == "not_started"
    stored = await owner.get()
    assert stored["data"]["policy"]["rule_effect"] == "allow"
    assert stored["data"]["policy"]["version"] == 1


async def test_policy_get_is_readonly_but_set_is_denied_by_the_engine(document):
    owner, _settings = document
    from custom_components.ha_mcp_tools.native_security_policy_tools import (
        security_policy_definition,
    )
    from ha_mcp.runtime import Engine

    engine = Engine(
        owner.manager.backend,
        [security_policy_definition(owner.manager)],
        read_only=True,
    )
    actor = CallContext("owner", read_only=True)
    read = await engine.call("ha_manage_security_policy", {"action": "get"}, actor)
    assert not read.is_error and "policy" in read.data["data"]
    with pytest.raises(ToolFailure, match="disabled"):
        await engine.call(
            "ha_manage_security_policy",
            {"action": "set", "policy": {"rules": []}},
            actor,
        )


async def test_policy_feature_off_refuses_even_a_read(document):
    owner, _settings = document
    owner.manager.policy.flags["enable_security_policy_tool"] = False
    with pytest.raises(ToolFailure, match="disabled") as raised:
        await owner.get()
    assert raised.value.status == "not_started"
