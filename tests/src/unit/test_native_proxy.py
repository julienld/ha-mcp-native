"""Qualify the small standard MCP proxy against a real HTTP endpoint."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


async def test_native_approval_continuation_carries_the_form_request(monkeypatch):
    from ._embedded_stubs import install

    install()

    from custom_components.ha_mcp_tools.native_approvals import (
        MCP_APPROVAL_CONTEXT,
        project_continuation,
    )
    from ha_mcp.policy import mrtr
    from ha_mcp.tool_api import CallContext

    from .policy.test_mrtr import context
    from .test_native_approvals import runtime

    monkeypatch.setattr(mrtr, "ROUND_WAIT_SECONDS", 0.01)
    _manager, engine, handler, _announced = runtime()
    token = MCP_APPROVAL_CONTEXT.set(context(name="write", arguments={}))
    try:
        result = await engine.call("write", {}, CallContext("alice", "client"))
        assert result.status == "pending", result.data
        projected = project_continuation(result)
        assert projected is not None
        assert "ha_mcp_approval" in projected.input_required.input_requests
        handler.assert_not_awaited()
    finally:
        MCP_APPROVAL_CONTEXT.reset(token)
        await engine.stop()


@pytest.mark.parametrize("case", ["contracts", "revoked", "drop"])
def test_native_proxy_real_http(case: str, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; from tests.src.unit._native_proxy_probe import run; asyncio.run(run(sys.argv[1]))",
            case,
        ],
        cwd=root,
        env={**os.environ, "HA_MCP_CONFIG_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_proxy_forwards_bound_approval_continuations(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from tests.src.unit._native_approval_probe import run; asyncio.run(run(through_proxy=True))",
        ],
        cwd=root,
        env={**os.environ, "HA_MCP_CONFIG_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
