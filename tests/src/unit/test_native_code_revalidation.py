"""Code mode cannot return saved scripts after authority changes during I/O."""

from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext

from .test_native_code import runtime


async def test_sandbox_compilation_leaves_the_loop_but_external_callbacks_stay_on_it():
    import asyncio
    import threading
    from types import SimpleNamespace

    from ha_mcp.runtime.code_runner import run_sandboxed_code

    owner_loop, owner_thread = asyncio.get_running_loop(), threading.get_ident()

    class Compiler:
        def __init__(self, code, **kwargs):
            assert threading.get_ident() != owner_thread, (
                "compilation blocks the HA loop"
            )

        async def run_async(self, **kwargs):
            assert asyncio.get_running_loop() is owner_loop
            return await kwargs["external_functions"]["call_tool"]("read", {})

    async def callback(name, args):
        assert asyncio.get_running_loop() is owner_loop
        return {"actual_loop": True}

    settings = SimpleNamespace(
        code_mode_max_duration=1, code_mode_max_memory=1024, code_mode_max_recursion=10
    )
    result = await run_sandboxed_code(
        "fixture", {"call_tool": callback}, settings, Compiler, lambda **kwargs: kwargs
    )
    assert result == {"actual_loop": True}


@pytest.mark.parametrize("mutation", [False, True])
async def test_native_code_revalidates_after_registry_io(
    tmp_path, monkeypatch, mutation
):
    manager, code = await runtime(tmp_path, monkeypatch, persistent=True)
    registry_call = code._registry_call
    revoked = [False]

    async def authorize(_actor):
        if revoked[0]:
            raise PermissionError("credential revoked")

    async def revoke_after_io(method, *args, **kwargs):
        result = await registry_call(method, *args, **kwargs)
        revoked[0] = True
        return result

    manager.backend.authorize = AsyncMock(side_effect=authorize)
    monkeypatch.setattr(code, "_registry_call", revoke_after_io)
    arguments = (
        {"code": "40 + 2", "justification": "Contract check", "save_as": "answer"}
        if mutation
        else {"list_saved": True}
    )
    result = await manager.engine.call(
        "ha_manage_custom_tool", arguments, CallContext("owner")
    )
    assert result.is_error
    assert result.data["error"]["code"] == "AUTH_FORBIDDEN"
    assert result.status == ("outcome_unknown" if mutation else "not_started")
    assert "saved_tools" not in repr(result.data)
    if mutation:
        assert code.registry.tools["answer"]["code"] == "40 + 2"
        assert result.data["saved_registry_changed"] is True
